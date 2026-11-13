"""Day 4 tests. Every Gemini call is MOCKED: no API key, no network, no cost.

Run:  python -m pytest tests/test_segment_emails.py -v
"""

from __future__ import annotations

import copy
import csv
import json
from datetime import date
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest
from google.genai import errors

from src.data import segment_profiles as sp
from src.llm import generator as gen
from src.llm.retry import InvalidResponseError, RetryError, RetryPolicy, call_with_retry
from src.llm.schemas import ProfileSet, SegmentEmailCampaign, SegmentEmailResponse
from src.llm.usage_tracker import PriceTable, TokenUsage, UsageLogger, estimate_cost

MODEL = "gemini-test-model"
FAST = RetryPolicy(max_attempts=3, initial_delay=0.01, max_delay=0.01)
NO_SLEEP = lambda _s: None  # noqa: E731

# --------------------------------------------------------------------------- fixtures / fakes


@pytest.fixture
def toy_data():
    """6 customers in 2 clusters with hand-checkable numbers."""
    segments = pd.DataFrame({
        "Customer_ID": [f"C{i}" for i in range(6)],
        "cluster": [0, 0, 0, 1, 1, 1],
        "frequency": [1, 2, 1, 3, 3, 2],
        "recency_days": [200, 100, 300, 10, 20, 95],
        "avg_order_value": [50.0, 60.0, 40.0, 500.0, 400.0, 600.0],
        "avg_quantity": [1.0, 2.0, 1.0, 3.0, 2.0, 1.0],
        "discount_rate": [0.0, 0.1, 0.0, 0.05, 0.0, 0.0],
        "total_spend": [50.0, 120.0, 40.0, 1500.0, 1200.0, 1200.0],
        "avg_rating": [4.0, 3.0, 5.0, 4.0, 4.0, 4.0],
    })
    cats = {"C0": ["Books"], "C1": ["Books", "Food"], "C2": ["Books"],
            "C3": ["Electronics"] * 3, "C4": ["Electronics", "Electronics", "Food"], "C5": ["Electronics", "Books"]}
    orders = pd.DataFrame([(cid, c, pd.Timestamp("2024-01-01")) for cid, cs in cats.items() for c in cs],
                          columns=["Customer_ID", "Product_Category", "Date"])
    return segments, orders


@pytest.fixture
def profiles(toy_data) -> ProfileSet:
    return sp.build_profiles(*toy_data)


def valid_payload(cluster_id: int = 0, **overrides) -> dict:
    body = ("Hi there, we have been thinking about the stories and everyday essentials you have picked up with us. "
            "Our Books shelves are full of fresh reads across every genre, and it only takes a minute to find "
            "your next favourite. Take a look whenever you have a quiet moment.\n\nThe {{store_name}} team")
    data = {
        "cluster_id": cluster_id,
        "segment_name": f"Test Segment {cluster_id}",
        "typical_customer_description": "A light, occasional shopper who buys small baskets, mostly books.",
        "purchasing_behavior": "One or two small orders, long gaps between purchases.",
        "product_interests": ["Books", "Food"],
        "marketing_strategy": "Gentle re-engagement around their favourite categories.",
        "email_subject": "A few good reads picked for you",
        "email_greeting": "Hi {{first_name}},",
        "email_body": body,
        "call_to_action": "Explore Books",
        "marketing_angle": "Rediscovery",
        "personalization_reason": "Books lift is 1.32 and recency is high, so a low-pressure nudge fits.",
    }
    data.update(overrides)
    return data


def usage(prompt=1000, out=300, thoughts=200):
    return SimpleNamespace(prompt_token_count=prompt, candidates_token_count=out, thoughts_token_count=thoughts,
                           total_token_count=prompt + out + thoughts)


def reply(payload: dict | str | None, meta=None):
    text = payload if (payload is None or isinstance(payload, str)) else json.dumps(payload)
    return SimpleNamespace(text=text, usage_metadata=meta if meta is not None else usage())


class FakeModels:
    """Stands in for client.models. Each call pops the next item: a response, or an exception to raise.
    A callable item is called with the request kwargs (to answer with the right cluster_id)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if callable(item) and not isinstance(item, SimpleNamespace):
            item = item(kwargs)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClient:
    def __init__(self, script):
        self.models = FakeModels(script)


def answer_for_requested_cluster(kwargs):
    cid = int(kwargs["contents"].split("cluster_id ")[1].split()[0])
    return reply(valid_payload(cid))


@pytest.fixture
def logger(tmp_path):
    prices = PriceTable(models={MODEL: [{"effective_from": None, "effective_to": None,
                                         "input_per_1m": 1.0, "output_per_1m": 4.0}]})
    return UsageLogger(path=tmp_path / "llm_usage.csv", prices=prices, run_id="test")


def server_error(code=503):
    return errors.ServerError(code, {"error": {"code": code, "message": "overloaded", "status": "UNAVAILABLE"}})


# --------------------------------------------------------------------------- 1. cluster profiles load correctly


def test_toy_profiles_are_correct(profiles):
    assert [c.cluster_id for c in profiles.clusters] == [0, 1]
    c0, c1 = profiles.get(0), profiles.get(1)
    assert c0.customers == 3 and c1.customers == 3
    assert c0.avg_total_spend == pytest.approx((50 + 120 + 40) / 3, abs=0.01)
    assert c0.pct_single_order_customers == pytest.approx(66.7)
    assert c1.pct_ordered_in_last_90_days == pytest.approx(66.7)       # 10 and 20 days, not 95
    assert c0.category_share_of_orders_pct == {"Books": 75.0, "Food": 25.0}
    # cluster 1: 6 of 8 orders are Electronics (75%) vs 6 of 12 overall (50%) -> lift 1.5
    assert c1.category_lift_vs_all_customers["Electronics"] == 1.5
    assert profiles.total_orders == 12


def test_profiles_contain_no_personal_data(profiles):
    dumped = profiles.model_dump_json()
    assert "C0" not in dumped and "Customer_ID" not in dumped


@pytest.mark.parametrize("break_it, message", [
    (lambda s, o: (s.drop(columns="discount_rate"), o), "missing columns"),
    (lambda s, o: (s.assign(cluster=[0, 0, 0, 2, 2, 2]), o), "no gaps"),
    (lambda s, o: (s.assign(frequency=[1, 1, 1, 3, 3, 2]), o), "order counts"),
])
def test_bad_input_is_rejected(toy_data, tmp_path, break_it, message):
    seg, orders = break_it(*toy_data)
    path = tmp_path / "seg.csv"
    seg.to_csv(path, index=False)
    with pytest.raises((sp.ProfileDataError, ValueError), match=message):
        sp.build_profiles(sp.load_segments(path), orders)


@pytest.mark.skipif(not (sp.SEGMENTS_PATH.exists() and sp.ORDERS_PATH.exists()), reason="Day 3 data not present")
def test_real_day3_profiles_load():
    p = sp.load_profiles()
    assert len(p.clusters) == 4 and p.total_customers == 5000
    assert sum(c.customers for c in p.clusters) == 5000
    assert p.get(1).avg_total_spend == pytest.approx(4755.66)   # matches the Day 3 notebook / README


# --------------------------------------------------------------------------- 2. every cluster gets its own email


def test_every_cluster_gets_its_own_email(profiles, logger):
    client = FakeClient([answer_for_requested_cluster] * 2)
    results = gen.generate_all(client, profiles, model=MODEL, logger=logger, policy=FAST, sleep=NO_SLEEP)
    assert [r.cluster_id for r in results] == [0, 1] and all(r.ok for r in results)
    assert [r.record.cluster_id for r in results] == [0, 1]
    # each prompt carried only its own cluster's statistics
    assert "cluster_id 0" in client.models.calls[0]["contents"]
    assert "cluster_id 1" in client.models.calls[1]["contents"]
    campaign = gen.build_campaign(results, MODEL)
    assert len(campaign.segments) == 2 and campaign.failed_segments == []


def test_request_uses_json_mode_and_schema(profiles):
    client = FakeClient([reply(valid_payload(0))])
    gen.generate_segment_email(client, profiles.get(0), profiles, model=MODEL, policy=FAST, sleep=NO_SLEEP)
    config = client.models.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is SegmentEmailResponse
    assert client.models.calls[0]["model"] == MODEL


# --------------------------------------------------------------------------- 3-5. schema, invalid JSON, missing fields


def test_valid_response_matches_schema():
    r = gen.parse_response(json.dumps(valid_payload(3)), expected_cluster_id=3)
    assert isinstance(r, SegmentEmailResponse) and r.cluster_id == 3


@pytest.mark.parametrize("text", ["", "   ", None, "{not json", '{"cluster_id": 0', "[1, 2]", "Sure! Here it is"])
def test_invalid_json_is_rejected(text):
    with pytest.raises(InvalidResponseError):
        gen.parse_response(text, 0)


@pytest.mark.parametrize("field", ["email_subject", "call_to_action", "segment_name", "personalization_reason"])
def test_missing_required_field_is_rejected(field):
    payload = valid_payload(0)
    del payload[field]
    with pytest.raises(InvalidResponseError, match=field):
        gen.parse_response(json.dumps(payload), 0)


def test_wrong_cluster_id_is_rejected():
    with pytest.raises(InvalidResponseError, match="mismatch"):
        gen.parse_response(json.dumps(valid_payload(2)), 0)


@pytest.mark.parametrize("field, text", [
    ("email_subject", "Get 20% off your next order"),
    ("call_to_action", "Use coupon SAVE10"),
    ("email_body", valid_payload()["email_body"] + " Enjoy free shipping on everything this week."),
    ("email_body", valid_payload()["email_body"] + " This exclusive offer is only for you, last chance!"),
    ("email_body", valid_payload()["email_body"] + " As one of our high-value segment members..."),
    ("email_greeting", "Hi [Customer Name],"),
])
def test_invented_offers_and_leaks_are_rejected(field, text):
    with pytest.raises(InvalidResponseError, match="unsupported"):
        gen.parse_response(json.dumps(valid_payload(0, **{field: text})), 0)


def test_invalid_response_is_retried_then_succeeds(profiles):
    bad = valid_payload(0)
    del bad["email_body"]
    client = FakeClient([reply("{oops"), reply(bad), reply(valid_payload(0))])
    res = gen.generate_segment_email(client, profiles.get(0), profiles, model=MODEL, policy=FAST, sleep=NO_SLEEP)
    assert res.ok and res.attempts == 3
    assert res.usage.input_tokens == 3000   # the two rejected replies were still billed, so they are counted


# --------------------------------------------------------------------------- 6-7. retries and timeouts


def test_server_errors_trigger_retries_with_exponential_backoff():
    waits = []
    script = [server_error(503), server_error(500), "ok"]

    def fn():
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    policy = RetryPolicy(max_attempts=4, initial_delay=2, multiplier=2, max_delay=30, jitter=0)
    value, attempts = call_with_retry(fn, policy, sleep=waits.append)
    assert value == "ok" and attempts == 3
    assert waits == [2, 4]                    # 2s, then doubled


def test_backoff_is_capped_and_attempts_are_limited():
    policy = RetryPolicy(max_attempts=5, initial_delay=2, multiplier=2, max_delay=5, jitter=0)
    assert [policy.backoff(n) for n in range(1, 5)] == [2, 4, 5, 5]
    calls = []

    def always_fails():
        calls.append(1)
        raise server_error(503)

    with pytest.raises(RetryError) as info:
        call_with_retry(always_fails, policy, sleep=NO_SLEEP)
    assert info.value.attempts == 5 and len(calls) == 5   # never more than max_attempts


def test_rate_limit_respects_server_retry_delay():
    waits = []
    rate_limited = errors.ClientError(429, {"error": {"code": 429, "message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}})
    script = [rate_limited, "ok"]

    def fn():
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    call_with_retry(fn, RetryPolicy(initial_delay=1, jitter=0), sleep=waits.append)
    assert waits == [7.0]


def test_non_retryable_error_fails_immediately():
    calls = []

    def bad_key():
        calls.append(1)
        raise errors.ClientError(401, {"error": {"code": 401, "message": "API key not valid"}})

    with pytest.raises(RetryError, match="http_401"):
        call_with_retry(bad_key, FAST, sleep=NO_SLEEP)
    assert len(calls) == 1


def test_timeout_is_retried(profiles):
    client = FakeClient([httpx.ReadTimeout("timed out"), reply(valid_payload(0))])
    res = gen.generate_segment_email(client, profiles.get(0), profiles, model=MODEL, policy=FAST, sleep=NO_SLEEP)
    assert res.ok and res.attempts == 2


def test_persistent_timeouts_fail_cleanly_and_other_clusters_continue(profiles, logger):
    timeouts = [httpx.ConnectTimeout("timed out")] * FAST.max_attempts
    client = FakeClient(timeouts + [answer_for_requested_cluster])
    results = gen.generate_all(client, profiles, model=MODEL, logger=logger, policy=FAST, sleep=NO_SLEEP)
    assert [r.ok for r in results] == [False, True]
    assert results[0].attempts == FAST.max_attempts and "timeout" in results[0].error
    campaign = gen.build_campaign(results, MODEL)
    assert [s.cluster_id for s in campaign.segments] == [1]
    assert campaign.failed_segments[0].cluster_id == 0


def test_client_has_timeout_and_no_sdk_retries():
    client = gen.make_client(timeout_s=12.5, api_key="test-key-not-real")
    opts = client._api_client._http_options
    assert opts.timeout == 12500          # the SDK expects milliseconds
    assert opts.retry_options is None     # our retry layer is the only one


# --------------------------------------------------------------------------- 8. token usage + cost logging


def test_usage_and_cost_are_logged(profiles, logger):
    client = FakeClient([reply(valid_payload(0), usage(prompt=1200, out=400, thoughts=100))])
    res = gen.generate_segment_email(client, profiles.get(0), profiles, model=MODEL, policy=FAST, sleep=NO_SLEEP)
    row = logger.log(cluster_id=0, model=MODEL, status="success", attempts=res.attempts, usage=res.usage)
    assert (row["input_tokens"], row["output_tokens"], row["thinking_tokens_in_output"], row["total_tokens"]) \
        == (1200, 500, 100, 1700)
    # 1200/1M * $1 + 500/1M * $4
    assert float(row["est_input_cost_usd"]) == pytest.approx(0.0012)
    assert float(row["est_output_cost_usd"]) == pytest.approx(0.0020)
    assert float(row["est_total_cost_usd"]) == pytest.approx(0.0032)
    assert row["retries"] == 0 and "ESTIMATED" in row["note"]

    with logger.path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["input_tokens"] == "1200" and rows[0]["model"] == MODEL


def test_missing_usage_metadata_is_not_invented(logger):
    u = TokenUsage()
    u.add(None)
    u.add(SimpleNamespace(prompt_token_count=None, candidates_token_count=None))
    row = logger.log(cluster_id=0, model=MODEL, status="success", attempts=1, usage=u)
    assert row["input_tokens"] == "" and row["est_total_cost_usd"] == ""
    assert "unknown" in row["note"]


def test_unknown_model_price_leaves_cost_empty(logger):
    u = TokenUsage()
    u.add(usage())
    row = logger.log(cluster_id=0, model="some-unpriced-model", status="success", attempts=1, usage=u)
    assert row["input_tokens"] == 1000 and row["est_total_cost_usd"] == ""


def test_dated_pricing_from_config():
    table = PriceTable.load()
    assert table.price_for("gemini-3.8-flash", date(2026, 11, 13)).input_per_1m == 0.75
    assert table.price_for("gemini-3.8-flash", date(2027, 1, 1)).output_per_1m == 7.50
    assert table.price_for("not-a-model") is None
    assert estimate_cost(TokenUsage(), table.price_for("gemini-3.8-flash")) is None


# --------------------------------------------------------------------------- 9. JSON output is valid


def test_saved_json_is_valid_and_round_trips(profiles, logger, tmp_path):
    client = FakeClient([answer_for_requested_cluster] * 2)
    results = gen.generate_all(client, profiles, model=MODEL, logger=logger, policy=FAST, sleep=NO_SLEEP)
    path = gen.save_campaign(gen.build_campaign(results, MODEL), tmp_path / "segment_emails.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {"campaign", "model", "segments"} <= data.keys()
    assert set(data["segments"][0]["email"]) == {"subject", "greeting", "body", "call_to_action"}
    SegmentEmailCampaign.model_validate(data)
    assert not path.with_suffix(".json.tmp").exists()


def test_duplicate_cluster_emails_are_rejected(profiles, logger):
    client = FakeClient([answer_for_requested_cluster] * 2)
    campaign = gen.build_campaign(gen.generate_all(client, profiles, model=MODEL, logger=logger, policy=FAST,
                                                   sleep=NO_SLEEP), MODEL)
    data = campaign.model_dump()
    data["segments"].append(copy.deepcopy(data["segments"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        SegmentEmailCampaign.model_validate(data)


@pytest.mark.skipif(not gen.EMAILS_PATH.exists(), reason="segment_emails.json not generated yet")
def test_generated_file_on_disk_is_valid():
    campaign = SegmentEmailCampaign.model_validate_json(gen.EMAILS_PATH.read_text(encoding="utf-8"))
    for s in campaign.segments:
        gen.parse_response(json.dumps({
            "cluster_id": s.cluster_id, "segment_name": s.segment_name,
            "typical_customer_description": s.typical_customer_description,
            "purchasing_behavior": s.purchasing_behavior, "product_interests": s.product_interests,
            "marketing_strategy": s.marketing_strategy, "email_subject": s.email.subject,
            "email_greeting": s.email.greeting, "email_body": s.email.body,
            "call_to_action": s.email.call_to_action, "marketing_angle": s.marketing_angle,
            "personalization_reason": s.personalization_reason}), s.cluster_id)


# --------------------------------------------------------------------------- 10. Day 1 still works


def test_day1_headlines_still_work():
    import headlines

    payload = {"product": "Wireless Earbuds", "headlines": [
        {"text": "Silence the noise, keep the music", "angle": "benefit"},
        {"text": "30 hours of sound in your pocket", "angle": "feature"},
        {"text": "Loved by commuters everywhere", "angle": "social proof"}]}
    client = FakeClient([SimpleNamespace(text="not json"), SimpleNamespace(text=json.dumps(payload))])
    result = headlines.generate_headlines("earbuds", client=client)
    assert len(result.headlines) == 3 and result.product == "Wireless Earbuds"
    assert len(client.models.calls) == 2   # Day 1's own validation retry still works
