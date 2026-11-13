"""Day 4: generate a segment description + personalised email draft for every K-Means cluster.

Flow for one cluster:
    ClusterProfile (aggregated stats) -> build_prompt() -> Gemini (JSON mode, response_schema)
        -> parse_response(): json.loads + Pydantic + business rules  -> SegmentEmailRecord
    all wrapped in call_with_retry() (timeout / 429 / 5xx / invalid JSON -> exponential backoff)
    and every call's tokens + estimated cost go to reports/llm_usage.csv.

Same Gemini setup as Day 1 (headlines.py): google-genai SDK, GEMINI_API_KEY from .env,
response_mime_type="application/json" + a Pydantic response_schema, then our own re-validation.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from google import genai
from google.genai import types
from pydantic import ValidationError

from src.llm.retry import InvalidResponseError, RetryError, RetryPolicy, call_with_retry
from src.llm.schemas import (
    ClusterProfile, FailedSegment, ProfileSet, SegmentEmailCampaign, SegmentEmailRecord, SegmentEmailResponse,
    SegmentStats,
)
from src.llm.usage_tracker import TokenUsage, UsageLogger

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
EMAILS_PATH = ROOT / "data" / "processed" / "segment_emails.json"
DEFAULT_TIMEOUT_S = 90.0
TEMPERATURE = 0.8   # some creativity for copywriting, but still grounded

# --------------------------------------------------------------------------- prompt

SYSTEM_INSTRUCTION = """You are a senior CRM marketing strategist and email copywriter for an online store.
The store sells these product categories: {categories}.

You receive AGGREGATED statistics for ONE customer segment found by K-Means clustering, plus the
same statistics for ALL customers as a baseline. You never see individual customers.

Rules:
1. Ground every claim in the numbers given. Compare the segment with the all-customer baseline to find
   what is genuinely distinctive. Do not invent demographics, motivations or facts the data does not show.
2. The cluster number is an arbitrary label from the algorithm. It carries no meaning.
3. Category preferences: `category_lift_vs_all_customers` = segment share / overall share. Only a lift
   ABOVE 1.15 is a real preference; lifts from 0.85 to 1.15 mean NO meaningful preference. If no category
   stands out, say the mix is broad and do not build the email or the segment name around a category.
   product_interests may contain ONLY category names from the store's list above.
   Build the segment name and email around the segment's most distinctive behaviour (the biggest
   difference from the baseline), whether that is spend, order size, frequency, recency or discounts.
   Recency: a HIGHER recency_days value means the customer bought LESS recently.
4. The store has NOT given you any promotions, discounts, coupon codes, free shipping, prices, new arrivals,
   stock information, curated or hand-picked collections, loyalty programme or policies. Never mention or
   imply any of these in the email (do not say "we have curated/gathered/selected ... for you").
   Invite the reader to browse the store's existing categories instead.
   The CTA must not promise an offer (e.g. "Browse Electronics", "Explore Books", not "Claim 20% off").
   For discount-sensitive segments, talk about value, smart choices or saved favourites, never an actual deal.
5. The email is customer-facing: never mention segments, clusters, statistics, percentages or money amounts in
   the subject, greeting, body or CTA. Use statistics only in the analysis fields and personalization_reason.
6. Merge tags: greet with {{{{first_name}}}}. If you sign off, sign as "The {{{{store_name}}}} team". Use no
   other placeholders, brand names, product names or URLs.
7. Tone: professional but warm, natural and human, concise. Body 80-160 words in 2-4 short paragraphs,
   plain text, no markdown. Subject line under 60 characters, no clickbait, no ALL CAPS.
"""

USER_PROMPT = """Analyse cluster_id {cluster_id} and write its email.

SEGMENT STATISTICS (cluster_id {cluster_id}):
{segment_json}

ALL-CUSTOMER BASELINE (for comparison):
{baseline_json}

Notes on the data: {n_customers} customers, {n_orders} orders, data ends {data_end}. Recency = days between a
customer's last order and {ref_date}. Discount rate = discount amount / list price. Ratings are 1-5.

Return the JSON object with: cluster_id ({cluster_id}), a short segment_name, typical_customer_description
(a typical customer in this group), purchasing_behavior, product_interests (categories that actually stand out,
or the broad mix if none do), marketing_strategy (their marketing needs and the recommended approach),
the email (email_subject, email_greeting, email_body, call_to_action), marketing_angle, and
personalization_reason (why this email suits this segment, citing the statistics)."""


def build_prompt(profile: ClusterProfile, profiles: ProfileSet) -> str:
    segment = profile.model_dump(exclude={"cluster_id"})
    return USER_PROMPT.format(
        cluster_id=profile.cluster_id,
        segment_json=json.dumps(segment, indent=2),
        baseline_json=json.dumps(profiles.overall.model_dump(), indent=2),
        n_customers=profiles.total_customers,
        n_orders=profiles.total_orders,
        data_end=profiles.data_end_date,
        ref_date=profiles.recency_reference_date,
    )


def build_system_instruction(overall: SegmentStats) -> str:
    return SYSTEM_INSTRUCTION.format(categories=", ".join(sorted(overall.category_share_of_orders_pct)))


# --------------------------------------------------------------------------- response validation

# Customer-facing copy must not promise offers we were never given, or leak internal numbers.
UNSUPPORTED_CLAIMS = [
    (r"\d\s*%|\bper\s?cent\b", "percentage"),
    (r"[$€£¥]\s?\d|\b\d+(\.\d+)?\s?(usd|eur|gbp|dollars?)\b", "money amount"),
    (r"\bcoupons?\b|\bvouchers?\b|\b(promo|discount|offer)\s+codes?\b", "coupon/code"),
    (r"\bfree\s+(shipping|delivery|returns?|gifts?)\b", "free shipping/gift"),
    (r"\b(special|exclusive|limited[- ]time|flash)\s+(offers?|deals?|discounts?|prices?|sales?)\b", "offer claim"),
    (r"\b\d+\s?(off)\b|\bbogo\b|\bbuy one,? get\b", "deal claim"),
    (r"\bsale ends\b|\bends (tonight|today|soon)\b|\blast chance\b", "false urgency"),
    (r"\bloyalty (programme|program|points|rewards?)\b|\breward points\b", "loyalty programme"),
    (r"\bcluster\b|\bsegment\b", "internal segment wording"),
    (r"\[[^\]]+\]|\{\{(?!\s*(first_name|store_name)\s*\}\})[^}]*\}\}", "unknown placeholder"),
]


def find_unsupported_claims(text: str) -> list[str]:
    return [label for pattern, label in UNSUPPORTED_CLAIMS if re.search(pattern, text, flags=re.IGNORECASE)]


def parse_response(text: str | None, expected_cluster_id: int) -> SegmentEmailResponse:
    """Turn the raw model text into a validated object, or raise InvalidResponseError (which is retried)."""
    if not text or not text.strip():
        raise InvalidResponseError("empty response (possibly blocked or truncated)")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise InvalidResponseError(f"malformed JSON: {err.msg} at char {err.pos}") from err
    if not isinstance(data, dict):
        raise InvalidResponseError(f"expected a JSON object, got {type(data).__name__}")
    try:
        result = SegmentEmailResponse.model_validate(data)
    except ValidationError as err:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in err.errors())
        raise InvalidResponseError(f"schema validation failed: {problems}") from err
    if result.cluster_id != expected_cluster_id:
        raise InvalidResponseError(f"cluster_id mismatch: expected {expected_cluster_id}, got {result.cluster_id}")

    customer_facing = " \n ".join([result.email_subject, result.email_greeting, result.email_body,
                                   result.call_to_action])
    claims = find_unsupported_claims(customer_facing)
    if claims:
        raise InvalidResponseError(f"email contains unsupported content: {', '.join(sorted(set(claims)))}")
    return result


# --------------------------------------------------------------------------- Gemini calls


def make_client(timeout_s: float = DEFAULT_TIMEOUT_S, api_key: str | None = None) -> genai.Client:
    """Gemini client with a per-request timeout. SDK-level retries stay OFF: call_with_retry owns retries,
    so a request is never retried twice by two layers."""
    api_key = api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set. Add it to your .env file (see .env.example).")
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))


@dataclass
class SegmentResult:
    cluster_id: int
    record: SegmentEmailRecord | None
    attempts: int
    usage: TokenUsage = field(default_factory=TokenUsage)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.record is not None


def generate_segment_email(
    client: genai.Client,
    profile: ClusterProfile,
    profiles: ProfileSet,
    *,
    model: str,
    policy: RetryPolicy = RetryPolicy(),
    sleep: Callable[[float], None] | None = None,
) -> SegmentResult:
    usage = TokenUsage()
    config = types.GenerateContentConfig(
        system_instruction=build_system_instruction(profiles.overall),
        response_mime_type="application/json",
        response_schema=SegmentEmailResponse,
        temperature=TEMPERATURE,
    )
    prompt = build_prompt(profile, profiles)

    def attempt() -> SegmentEmailResponse:
        response = client.models.generate_content(model=model, contents=prompt, config=config)
        usage.add(getattr(response, "usage_metadata", None))   # count tokens even if the JSON turns out bad
        try:
            text = response.text
        except Exception as err:  # noqa: BLE001 - SDK raises on some blocked/odd responses
            raise InvalidResponseError(f"could not read response text: {err}") from err
        return parse_response(text, profile.cluster_id)

    kwargs = {"sleep": sleep} if sleep is not None else {}
    try:
        response, attempts = call_with_retry(attempt, policy, label=f"cluster {profile.cluster_id}", **kwargs)
    except RetryError as err:
        return SegmentResult(profile.cluster_id, None, err.attempts, usage, error=str(err))
    return SegmentResult(profile.cluster_id, SegmentEmailRecord.from_response(response, profile), attempts, usage)


def generate_all(
    client: genai.Client,
    profiles: ProfileSet,
    *,
    model: str,
    logger: UsageLogger,
    policy: RetryPolicy = RetryPolicy(),
    cluster_ids: list[int] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[SegmentResult]:
    """One Gemini call per cluster. A failing cluster does not stop the others."""
    results = []
    for profile in profiles.clusters:
        if cluster_ids is not None and profile.cluster_id not in cluster_ids:
            continue
        log.info("cluster %s: generating (%s customers)", profile.cluster_id, profile.customers)
        res = generate_segment_email(client, profile, profiles, model=model, policy=policy, sleep=sleep)
        logger.log(cluster_id=res.cluster_id, model=model, status="success" if res.ok else "failed",
                   attempts=res.attempts, usage=res.usage, error=res.error)
        results.append(res)
    return results


# --------------------------------------------------------------------------- saving


def build_campaign(results: list[SegmentResult], model: str) -> SegmentEmailCampaign:
    return SegmentEmailCampaign(
        model=model,
        generated_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        segments=sorted((r.record for r in results if r.ok), key=lambda s: s.cluster_id),
        failed_segments=[FailedSegment(cluster_id=r.cluster_id, attempts=r.attempts, error=r.error[:300])
                         for r in results if not r.ok],
    )


def save_campaign(campaign: SegmentEmailCampaign, path: Path = EMAILS_PATH) -> Path:
    """Re-validate, then write atomically (temp file + rename) so a crash never leaves half a JSON file."""
    payload = SegmentEmailCampaign.model_validate(campaign.model_dump()).model_dump_json(indent=2)
    json.loads(payload)  # paranoia: the file we write must be valid JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    return path
