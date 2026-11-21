"""Day 5 API tests. Gemini is ALWAYS mocked: no API key, no network, no cost.

FastAPI's TestClient sends real HTTP requests to the app in-process. `app.dependency_overrides` swaps the
real Gemini client, retry policy and usage log for fakes, exactly where the app would normally create them.

Run:  python -m pytest tests/test_api.py -v
"""

from __future__ import annotations

import csv
import json
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api import main, segments
from src.api.schemas import GenerateRequest, GenerateResponse, SegmentsResponse
from src.llm.retry import RetryPolicy
from src.llm.usage_tracker import PriceTable, UsageLogger
from test_segment_emails import FakeClient, reply, server_error, valid_payload  # Day 4 fakes, reused

FAST = RetryPolicy(max_attempts=3, initial_delay=0.0, max_delay=0.0)
TEST_MODEL = "gemini-test-model"


@pytest.fixture
def usage_log(tmp_path):
    prices = PriceTable(models={TEST_MODEL: [{"effective_from": None, "effective_to": None,
                                              "input_per_1m": 1.0, "output_per_1m": 4.0}]})
    return UsageLogger(path=tmp_path / "llm_usage.csv", prices=prices, run_id="test")


@pytest.fixture
def api(usage_log, monkeypatch):
    """A TestClient whose Gemini client is set per test via `api.gemini(script)`."""
    monkeypatch.setenv("GEMINI_MODEL", TEST_MODEL)
    holder = {}
    main.app.dependency_overrides.update({
        main.get_client_factory: lambda: (lambda: holder["client"]),
        main.get_retry_policy: lambda: FAST,
        main.get_usage_logger: lambda: usage_log,
    })
    client = TestClient(main.app)

    def gemini(script):
        holder["client"] = FakeClient(script)
        return holder["client"]

    client.gemini = gemini
    yield client
    main.app.dependency_overrides.clear()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Copies of the committed data files that a test may break, without touching the real ones."""
    for attr in ("PROFILES_PATH", "NAMES_PATH", "EMAILS_PATH"):
        src = getattr(segments, attr)
        dst = tmp_path / src.name
        shutil.copy(src, dst)
        monkeypatch.setattr(segments, attr, dst)
    return tmp_path


# --------------------------------------------------------------------------- health & docs


def test_health_ok_and_never_leaks_the_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-test-key-123")
    r = TestClient(main.app).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["segments_available"] is True
    assert body["gemini_api_key_configured"] is True
    assert "secret-test-key-123" not in r.text


def test_health_reports_missing_key_and_data(monkeypatch, data_dir):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (data_dir / "cluster_profiles.json").unlink()
    body = TestClient(main.app).get("/health").json()
    assert body["gemini_api_key_configured"] is False and body["segments_available"] is False


def test_swagger_docs_and_openapi_schema():
    c = TestClient(main.app)
    assert c.get("/docs").status_code == 200
    spec = c.get("/openapi.json").json()
    assert {"/segments", "/generate", "/health"} <= set(spec["paths"])
    assert "404" in spec["paths"]["/generate"]["post"]["responses"]


# --------------------------------------------------------------------------- GET /segments


def test_segments_returns_the_real_model_output():
    r = TestClient(main.app).get("/segments")
    assert r.status_code == 200
    data = SegmentsResponse.model_validate(r.json())   # response matches its schema
    assert data.n_clusters == 4 and data.total_customers == 5000
    assert sum(s.customer_count for s in data.segments) == 5000
    assert sum(s.percentage for s in data.segments) == pytest.approx(100, abs=0.2)
    by_id = {s.cluster_id: s for s in data.segments}
    assert by_id[1].segment_name == "Big-Basket Buyers"
    assert by_id[1].characteristics.avg_total_spend == pytest.approx(4755.66)  # Day 3 notebook / README
    assert by_id[1].top_category == "Electronics"
    assert [p.category for p in by_id[0].preferred_categories] == ["Books", "Beauty", "Food"]
    assert by_id[3].top_category is None and by_id[3].preferred_categories == []  # broad mix
    assert by_id[0].ai_segment_name  # Day 4 name is attached when the drafts file exists


def test_segments_expose_no_customer_level_data():
    text = TestClient(main.app).get("/segments").text
    for leak in ("Customer_ID", "CUST_", "Age", "Gender", "City"):
        assert leak not in text


def test_segments_missing_data_returns_503(data_dir):
    (data_dir / "cluster_profiles.json").unlink()
    r = TestClient(main.app).get("/segments")
    assert r.status_code == 503 and "not found" in r.json()["detail"]


def test_segments_corrupt_data_returns_503(data_dir):
    (data_dir / "cluster_profiles.json").write_text("{not json", encoding="utf-8")
    r = TestClient(main.app).get("/segments")
    assert r.status_code == 503 and "not a valid" in r.json()["detail"]


def test_stale_names_fall_back_to_generic_and_missing_emails_are_optional(data_dir):
    names = json.loads((data_dir / "segment_names.json").read_text(encoding="utf-8"))
    names["clusters"][2]["customers"] = 999   # pretend the notebook was re-run and the cluster changed
    (data_dir / "segment_names.json").write_text(json.dumps(names), encoding="utf-8")
    (data_dir / "segment_emails.json").unlink()
    r = TestClient(main.app).get("/segments")
    assert r.status_code == 200
    by_id = {s["cluster_id"]: s for s in r.json()["segments"]}
    assert by_id[2]["segment_name"] == "Cluster 2" and by_id[0]["segment_name"] == "Low-Value Occasional Buyers"
    assert all(s["ai_segment_name"] is None for s in by_id.values())


# --------------------------------------------------------------------------- POST /generate


def test_generate_returns_a_validated_email(api, usage_log):
    fake = api.gemini([reply(valid_payload(2))])
    r = api.post("/generate", json={"cluster_id": 2})
    assert r.status_code == 200
    body = GenerateResponse.model_validate(r.json())
    assert body.cluster_id == 2 and body.status == "draft - not sent"
    assert body.segment_name == "Test Segment 2"
    assert body.email.subject and body.email.body.startswith("Hi there")
    assert body.customers == 1024 and body.pct_of_all_customers == 20.5   # from the real profile, not the LLM
    assert body.generation.model == TEST_MODEL and body.generation.attempts == 1
    assert body.generation.input_tokens == 1000 and body.generation.output_tokens == 500
    assert body.generation.estimated_cost_usd == pytest.approx(1000 / 1e6 * 1.0 + 500 / 1e6 * 4.0)

    call = fake.models.calls[0]   # Day 4 request: JSON mode + schema, the real cluster's statistics
    assert call["model"] == TEST_MODEL
    assert call["config"].response_mime_type == "application/json"
    assert "cluster_id 2" in call["contents"] and '"customers": 1024' in call["contents"]
    with usage_log.path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["status"] == "success" and rows[0]["cluster_id"] == "2"


def test_generate_retries_an_invalid_reply_then_succeeds(api):
    fake = api.gemini([reply("not json"), reply(valid_payload(0))])
    r = api.post("/generate", json={"cluster_id": 0})
    assert r.status_code == 200 and r.json()["generation"]["attempts"] == 2
    assert len(fake.models.calls) == 2


@pytest.mark.parametrize("cluster_id", [4, 99, 10**9])
def test_unknown_cluster_is_404_and_gemini_is_not_called(api, cluster_id):
    fake = api.gemini([])
    r = api.post("/generate", json={"cluster_id": cluster_id})
    assert r.status_code == 404
    assert "Available cluster_ids: [0, 1, 2, 3]" in r.json()["detail"]
    assert fake.models.calls == []


@pytest.mark.parametrize("payload", [
    {}, {"cluster_id": -1}, {"cluster_id": "abc"}, {"cluster_id": "0"}, {"cluster_id": 1.5},
    {"cluster_id": True}, {"cluster_id": None}, {"cluster_id": 0, "send": True}, [0],
])
def test_invalid_requests_are_rejected_with_422(api, payload):
    fake = api.gemini([])
    r = api.post("/generate", json=payload)
    assert r.status_code == 422
    assert fake.models.calls == []


def test_non_json_body_is_rejected(api):
    api.gemini([])
    r = api.post("/generate", content="cluster_id=0", headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_get_on_generate_is_not_allowed(api):
    assert api.get("/generate").status_code == 405


def test_persistent_server_errors_return_502(api, usage_log):
    fake = api.gemini([server_error(503)] * 3)
    r = api.post("/generate", json={"cluster_id": 1})
    assert r.status_code == 502 and "after 3 attempt(s)" in r.json()["detail"]
    assert len(fake.models.calls) == 3   # Day 4 retry policy was applied
    with usage_log.path.open(encoding="utf-8") as f:
        assert next(csv.DictReader(f))["status"] == "failed"


def test_timeouts_return_504(api):
    api.gemini([httpx.ReadTimeout("slow")] * 3)
    r = api.post("/generate", json={"cluster_id": 1})
    assert r.status_code == 504 and "timed out" in r.json()["detail"]


def test_rate_limit_returns_503(api):
    api.gemini([server_error(429)] * 3)
    assert api.post("/generate", json={"cluster_id": 1}).status_code == 503


def test_invalid_replies_return_502_with_reason(api):
    bad = valid_payload(3, email_body=valid_payload()["email_body"] + " Enjoy 20% off today!")
    api.gemini([reply(bad)] * 3)
    r = api.post("/generate", json={"cluster_id": 3})
    assert r.status_code == 502 and "percentage" in r.json()["detail"]


def test_bad_api_key_fails_fast_without_retries(api):
    from google.genai import errors
    fake = api.gemini([errors.ClientError(403, {"error": {"code": 403, "message": "denied", "status": "DENIED"}})])
    r = api.post("/generate", json={"cluster_id": 0})
    assert r.status_code == 502 and "GEMINI_API_KEY" in r.json()["detail"]
    assert len(fake.models.calls) == 1


def test_missing_api_key_returns_503_but_bad_id_still_404(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    c = TestClient(main.app)   # real client factory: no override
    r = c.post("/generate", json={"cluster_id": 0})
    assert r.status_code == 503 and "GEMINI_API_KEY" in r.json()["detail"]
    assert c.post("/generate", json={"cluster_id": 42}).status_code == 404


def test_generate_with_missing_data_returns_503(api, data_dir):
    api.gemini([])
    (data_dir / "cluster_profiles.json").unlink()
    assert api.post("/generate", json={"cluster_id": 0}).status_code == 503


# --------------------------------------------------------------------------- Pydantic models


def test_request_model_validation():
    assert GenerateRequest.model_validate({"cluster_id": 3}).cluster_id == 3
    for bad in ({"cluster_id": -1}, {"cluster_id": "1"}, {}, {"cluster_id": 1, "extra": 1}):
        with pytest.raises(ValidationError):
            GenerateRequest.model_validate(bad)


def test_response_model_rejects_incomplete_or_sent_emails():
    good = TestClient(main.app).get("/segments").json()
    SegmentsResponse.model_validate(good)
    broken = json.loads(json.dumps(good))
    broken["segments"][0]["percentage"] = 140
    with pytest.raises(ValidationError):
        SegmentsResponse.model_validate(broken)
    with pytest.raises(ValidationError):
        GenerateResponse.model_validate({"cluster_id": 0, "segment_name": "x", "generation": {"model": "m", "attempts": 1}})


def test_env_settings_are_parsed_safely(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_S", "abc")
    monkeypatch.setenv("GEMINI_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("GEMINI_MODEL", "  ")
    s = main.get_settings()
    assert s.timeout_s == 90.0 and s.max_attempts == 2 and s.model == main.DEFAULT_MODEL
