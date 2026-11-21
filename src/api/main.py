"""Campaign Segmenter HTTP API (Day 5).

    uvicorn src.api.main:app --reload          # local
    docker compose up --build                  # container
    open http://localhost:8000/docs            # Swagger UI

Endpoints
    GET  /health     the app is running (+ whether data and a Gemini key are available)
    GET  /segments   the K-Means segments with sizes, behaviour and category preferences
    POST /generate   a Gemini-written email DRAFT for one segment (nothing is ever sent)

The API is a thin layer: it reuses Day 3/4 outputs and Day 4's `generate_segment_email()` (prompt, JSON mode,
Pydantic validation, offer guardrails, retries with backoff, per-request timeout, token/cost logging).

Configuration (environment variables, never baked into the image):
    GEMINI_API_KEY        required for POST /generate only
    GEMINI_MODEL          default: gemini-3.8-flash (same setting as Day 1/4)
    GEMINI_TIMEOUT_S      per-request timeout, default 90
    GEMINI_MAX_ATTEMPTS   attempts per email incl. the first, default 4
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import RedirectResponse
from google import genai

from headlines import MODEL as DEFAULT_MODEL  # Day 1: loads .env (if present) and reads GEMINI_MODEL
from src.api.schemas import (
    ErrorResponse, GenerateRequest, GenerateResponse, GenerationInfo, HealthResponse, SegmentsResponse,
)
from src.api.segments import SegmentDataError, SegmentStore, load_store, summarize
from src.llm.generator import DEFAULT_TIMEOUT_S, SegmentResult, generate_segment_email, make_client
from src.llm.retry import RetryPolicy
from src.llm.usage_tracker import UsageLogger, estimate_cost

log = logging.getLogger("campaign_segmenter.api")

VERSION = "1.0.0"

app = FastAPI(
    title="Campaign Segmenter API",
    version=VERSION,
    description=(
        "Customer segments from a K-Means model (K = 4, 5,000 e-commerce customers) and Gemini-written, "
        "personalised marketing email **drafts** for each segment.\n\n"
        "* `GET /segments` - the segments and what makes each one different\n"
        "* `POST /generate` - write an email draft for one segment (calls Gemini; **no email is sent**)\n"
        "* `GET /health` - liveness check\n\n"
        "Only aggregated, group-level statistics are used; no customer personal data is exposed or sent to the LLM."
    ),
)


# --------------------------------------------------------------------------- settings & dependencies
# FastAPI "dependencies" are small functions that endpoints declare as parameters. FastAPI calls them for
# every request. Tests replace them with `app.dependency_overrides` (fake Gemini, temp files, fast retries).


@dataclass(frozen=True)
class Settings:
    model: str
    timeout_s: float
    max_attempts: int


def _env_number(name: str, default: float, cast: type) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return cast(raw) if raw else default
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def get_settings() -> Settings:
    return Settings(
        model=os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL,
        timeout_s=_env_number("GEMINI_TIMEOUT_S", DEFAULT_TIMEOUT_S, float),
        max_attempts=max(1, int(_env_number("GEMINI_MAX_ATTEMPTS", 4, int))),
    )


def get_store() -> SegmentStore:
    """The saved segmentation results. Read per request: the files are tiny and this picks up regenerations."""
    try:
        return load_store()
    except SegmentDataError as err:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Segment data unavailable: {err}") from err


@lru_cache(maxsize=4)
def _cached_client(api_key: str, timeout_s: float) -> genai.Client:
    return make_client(timeout_s=timeout_s, api_key=api_key)  # Day 4: timeout set, SDK retries off


def get_client_factory(settings: Settings = Depends(get_settings)) -> Callable[[], genai.Client]:
    """Returns a function that builds the Gemini client. It is called only after the cluster_id has been
    checked, so an invalid id gets a 404 even when no API key is configured."""

    def factory() -> genai.Client:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "GEMINI_API_KEY is not configured on the server. Set it in .env (local / docker compose) "
                "or pass it with `docker run -e GEMINI_API_KEY=...`.")
        return _cached_client(api_key, settings.timeout_s)

    return factory


def get_retry_policy(settings: Settings = Depends(get_settings)) -> RetryPolicy:
    return RetryPolicy(max_attempts=settings.max_attempts)  # Day 4 backoff: 2s -> 4s -> 8s, capped at 30s


def get_usage_logger() -> UsageLogger:
    return UsageLogger(run_id="api-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))


# --------------------------------------------------------------------------- endpoints


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/health", response_model=HealthResponse, tags=["system"], summary="Is the API running?")
def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Always 200 while the app is up. Also reports whether segment data and a Gemini key are available
    (the key itself is never returned)."""
    try:
        load_store()
        segments_ok = True
    except SegmentDataError:
        segments_ok = False
    return HealthResponse(
        version=VERSION,
        segments_available=segments_ok,
        gemini_api_key_configured=bool(os.getenv("GEMINI_API_KEY", "").strip()),
        model=settings.model,
    )


@app.get(
    "/segments",
    response_model=SegmentsResponse,
    tags=["segments"],
    summary="List the customer segments found by K-Means",
    responses={503: {"model": ErrorResponse, "description": "Saved segmentation output is missing or invalid"}},
)
def list_segments(store: SegmentStore = Depends(get_store)) -> SegmentsResponse:
    """Returns every segment with its size, share of the customer base, average purchasing behaviour and
    product-category preferences.

    A category counts as a preference only if its **lift** (segment share / overall share) is above 1.15.
    `top_category` is `null` when the segment buys a broad mix with no category standing out.
    """
    return summarize(store)


_GEMINI_FAILURE_STATUS = {
    "timeout": status.HTTP_504_GATEWAY_TIMEOUT,
    "rate_limited": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _failure_detail(result: SegmentResult) -> str:
    kind, n = result.error_kind or "unknown", result.attempts
    if kind == "invalid_response":
        # Our own validation message (e.g. "email contains unsupported content: percentage"): safe to show.
        return f"Gemini replied, but no reply passed validation after {n} attempt(s): {result.error[:300]}"
    if kind == "timeout":
        return f"Gemini timed out on all {n} attempt(s). Try again, or raise GEMINI_TIMEOUT_S."
    if kind == "rate_limited":
        return f"Gemini rate limit reached after {n} attempt(s). Try again in a minute."
    if kind.startswith("http_4"):
        return (f"Gemini rejected the request ({kind}). Check that GEMINI_API_KEY is valid and "
                f"GEMINI_MODEL is a model your key can use.")
    return f"Gemini request failed after {n} attempt(s) ({kind})."


@app.post(
    "/generate",
    response_model=GenerateResponse,
    tags=["emails"],
    summary="Generate a personalised email draft for one segment",
    responses={
        404: {"model": ErrorResponse, "description": "Unknown cluster_id"},
        422: {"description": "Invalid request body (e.g. missing, negative or non-integer cluster_id)"},
        502: {"model": ErrorResponse, "description": "Gemini failed or returned no valid email"},
        503: {"model": ErrorResponse, "description": "Gemini key not configured, rate-limited, or data missing"},
        504: {"model": ErrorResponse, "description": "Gemini timed out on every attempt"},
    },
)
def generate(
    request: GenerateRequest,
    store: SegmentStore = Depends(get_store),
    settings: Settings = Depends(get_settings),
    client_factory: Callable[[], genai.Client] = Depends(get_client_factory),
    policy: RetryPolicy = Depends(get_retry_policy),
    usage_logger: UsageLogger = Depends(get_usage_logger),
) -> GenerateResponse:
    """Sends the segment's **aggregated statistics** (plus the all-customer baseline) to Gemini and returns
    the validated email draft. Uses Day 4's prompt, JSON-mode response schema, offer guardrails, timeout and
    retry-with-backoff. Each call can take 10-30 s. **No email is sent.**

    Every call uses tokens; the response reports them with an *estimated* cost.
    """
    try:
        profile = store.profiles.get(request.cluster_id)
    except KeyError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"cluster_id {request.cluster_id} does not exist. Available cluster_ids: {store.cluster_ids}.",
        ) from None

    client = client_factory()
    # A plain `def` endpoint runs in FastAPI's thread pool, so this blocking call doesn't freeze the server.
    result = generate_segment_email(client, profile, store.profiles, model=settings.model, policy=policy)

    try:
        usage_logger.log(cluster_id=result.cluster_id, model=settings.model,
                         status="success" if result.ok else "failed", attempts=result.attempts,
                         usage=result.usage, error=result.error)
    except OSError as err:  # e.g. read-only filesystem: logging must never break the response
        log.warning("could not write usage log: %s", err)

    if not result.ok:
        log.error("cluster %s: generation failed: %s", result.cluster_id, result.error[:300])
        raise HTTPException(_GEMINI_FAILURE_STATUS.get(result.error_kind, status.HTTP_502_BAD_GATEWAY),
                            _failure_detail(result))

    cost = estimate_cost(result.usage, usage_logger.prices.price_for(settings.model))
    usage = result.usage
    return GenerateResponse(
        **result.record.model_dump(),
        generation=GenerationInfo(
            model=settings.model,
            attempts=result.attempts,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost_usd=round(cost.total, 6) if cost else None,
        ),
    )
