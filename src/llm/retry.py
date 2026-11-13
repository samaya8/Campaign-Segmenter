"""Retry with exponential backoff for LLM calls.

Why retry?   Network calls fail for reasons that fix themselves: rate limits (HTTP 429), an overloaded
             server (500/502/503/504), a request that took too long (timeout), or a model reply that was
             not valid JSON. Trying again a moment later usually works.
Why backoff? If we retried instantly we would hit an overloaded / rate-limited server even harder.
             So we wait 2s, then 4s, then 8s ... (doubling = "exponential"), capped at `max_delay`,
             plus a little random "jitter" so many clients don't all retry at the same instant.
Why a cap?   `max_attempts` guarantees we never loop forever.

Errors that will NOT fix themselves (bad API key 401/403, unknown model 404, bad request 400) are
raised immediately - retrying them only wastes time and quota.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from google.genai import errors

log = logging.getLogger(__name__)
T = TypeVar("T")

RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _transient_network_errors() -> tuple[type[BaseException], ...]:
    """Timeout / connection exceptions from whichever HTTP library the SDK uses (httpx or httpx2)."""
    excs: list[type[BaseException]] = [TimeoutError, ConnectionError]
    for name in ("httpx", "httpx2"):
        try:
            mod = __import__(name)
        except ImportError:
            continue
        excs += [mod.TimeoutException, mod.TransportError]  # TransportError covers connect/read/protocol errors
    return tuple(excs)


TRANSIENT_NETWORK_ERRORS = _transient_network_errors()


class InvalidResponseError(Exception):
    """The model answered, but the answer was not valid JSON or failed Pydantic validation."""


class RetryError(Exception):
    """All attempts failed (or a non-retryable error happened)."""

    def __init__(self, message: str, attempts: int, last_error: BaseException):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4        # 1 call + up to 3 retries
    initial_delay: float = 2.0   # seconds before the 1st retry
    multiplier: float = 2.0      # 2s -> 4s -> 8s ...
    max_delay: float = 30.0      # never wait longer than this between attempts
    jitter: float = 0.25         # add up to +25% random extra wait

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

    def backoff(self, retry_number: int, rand: float = 0.0) -> float:
        """Delay before retry number `retry_number` (1-based). `rand` in [0, 1) adds jitter."""
        base = min(self.max_delay, self.initial_delay * self.multiplier ** (retry_number - 1))
        return min(self.max_delay, base * (1 + self.jitter * rand))


def classify(exc: BaseException) -> tuple[str, bool]:
    """Return (error_kind, is_retryable)."""
    if isinstance(exc, InvalidResponseError):
        return "invalid_response", True
    if isinstance(exc, errors.APIError):
        code = exc.code
        if code == 429:
            return "rate_limited", True
        if code == 408:
            return "timeout", True
        if code in RETRYABLE_STATUS_CODES:
            return "server_error", True
        return f"http_{code}", False
    if isinstance(exc, TRANSIENT_NETWORK_ERRORS):
        return ("timeout" if "timeout" in type(exc).__name__.lower() or isinstance(exc, TimeoutError)
                else "network_error"), True
    return type(exc).__name__, False


def server_retry_delay(exc: BaseException) -> float | None:
    """On a 429 Gemini may say how long to wait (RetryInfo.retryDelay, e.g. "17s"). Honour it if present."""
    if not isinstance(exc, errors.APIError) or not isinstance(exc.details, dict):
        return None
    for d in (exc.details.get("error", exc.details).get("details") or []):
        if isinstance(d, dict) and str(d.get("@type", "")).endswith("RetryInfo"):
            m = re.fullmatch(r"([\d.]+)s", str(d.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    return None


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy = RetryPolicy(),
    *,
    label: str = "llm call",
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> tuple[T, int]:
    """Call `fn()` until it succeeds or attempts run out. Returns (result, attempts_used).

    `sleep` and `rand` are injectable so tests run instantly and deterministically.
    """
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn(), attempt
        except Exception as exc:  # noqa: BLE001 - we classify and re-raise below
            kind, retryable = classify(exc)
            msg = f"{label}: attempt {attempt}/{policy.max_attempts} failed [{kind}] {str(exc)[:200]}"
            if not retryable:
                log.error("%s - not retryable, giving up", msg)
                raise RetryError(f"{kind}: {exc}", attempt, exc) from exc
            if attempt == policy.max_attempts:
                log.error("%s - no attempts left", msg)
                raise RetryError(f"gave up after {attempt} attempts, last error {kind}: {exc}", attempt, exc) from exc

            delay = policy.backoff(attempt, rand())
            hinted = server_retry_delay(exc)
            if hinted is not None:
                delay = max(delay, min(hinted, 60.0))   # respect the server, but never wait more than a minute
            log.warning("%s - retrying in %.1fs", msg, delay)
            sleep(delay)
    raise AssertionError("unreachable")
