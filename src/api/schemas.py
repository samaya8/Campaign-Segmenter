"""Pydantic models for the HTTP API (Day 5).

Request bodies are validated BEFORE our code runs (FastAPI returns 422 if they are wrong), and every response
is validated against its `response_model` before it is sent, so the API can never return a malformed payload.

The email part reuses Day 4's models: `SegmentEmailRecord` (the validated Gemini result) and `EmailDraft`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.llm.schemas import SegmentEmailRecord

# --------------------------------------------------------------------------- GET /segments


class SegmentCharacteristics(BaseModel):
    """Group-level purchasing behaviour of one cluster (averages over its customers)."""

    avg_total_spend: float = Field(ge=0, description="Average lifetime spend per customer.")
    median_total_spend: float = Field(ge=0)
    avg_order_value: float = Field(ge=0, description="Average value of one order.")
    avg_orders_per_customer: float = Field(ge=1, description="Purchase frequency.")
    pct_single_order_customers: float = Field(ge=0, le=100)
    avg_items_per_order: float = Field(ge=0)
    avg_recency_days: float = Field(ge=0, description="Days since the last order (higher = less recent).")
    pct_ordered_in_last_90_days: float = Field(ge=0, le=100)
    avg_discount_rate_pct: float = Field(ge=0, le=100)
    pct_customers_who_used_a_discount: float = Field(ge=0, le=100)
    avg_rating: float = Field(ge=1, le=5)


class CategoryPreference(BaseModel):
    category: str
    share_of_orders_pct: float = Field(ge=0, le=100, description="Share of this segment's orders.")
    lift_vs_all_customers: float = Field(ge=0, description="Segment share / overall share. 1.35 = 35% more popular.")


class Segment(BaseModel):
    cluster_id: int = Field(ge=0)
    segment_name: str = Field(description="Human-readable name from the Day 3 analysis.")
    description: str | None = Field(default=None, description="One-line summary from the Day 3 analysis.")
    ai_segment_name: str | None = Field(
        default=None, description="Name Gemini gave this segment in the saved Day 4 email drafts, if available.")
    customer_count: int = Field(gt=0)
    percentage: float = Field(gt=0, le=100, description="Share of the whole customer base.")
    characteristics: SegmentCharacteristics
    top_category: str | None = Field(
        description="Category with the highest lift, if it is a real preference (lift above the threshold). "
                    "null = broad mix, no category stands out.")
    preferred_categories: list[CategoryPreference] = Field(
        description="All categories with lift above the threshold, strongest first.")


class SegmentsResponse(BaseModel):
    total_customers: int = Field(gt=0)
    total_orders: int = Field(gt=0)
    n_clusters: int = Field(gt=0)
    data_end_date: str
    preference_lift_threshold: float
    segments: list[Segment]


# --------------------------------------------------------------------------- POST /generate


class GenerateRequest(BaseModel):
    """Which segment to write an email for. Strict: "0", 0.5, true and unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"cluster_id": 0}]})

    cluster_id: int = Field(ge=0, strict=True, description="A cluster_id returned by GET /segments.")


class GenerationInfo(BaseModel):
    model: str
    attempts: int = Field(ge=1, description="Gemini calls used (1 = no retries).")
    input_tokens: int | None = None
    output_tokens: int | None = Field(default=None, description="Includes thinking tokens (billed as output).")
    total_tokens: int | None = None
    estimated_cost_usd: float | None = Field(
        default=None, description="ESTIMATE from config/llm_pricing.json; null if usage or price is unknown.")


class GenerateResponse(SegmentEmailRecord):
    """Day 4's validated email record, plus delivery status and token/cost info."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["draft - not sent"] = "draft - not sent"
    generation: GenerationInfo


# --------------------------------------------------------------------------- misc


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    segments_available: bool
    gemini_api_key_configured: bool = Field(description="Whether a key is set. The key itself is never returned.")
    model: str


class ErrorResponse(BaseModel):
    detail: str
