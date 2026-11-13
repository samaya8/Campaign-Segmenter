"""Pydantic schemas for Day 4: cluster profiles in, segment emails out.

Three groups of models:

1. INPUT  - `SegmentStats`, `ClusterProfile`, `ProfileSet`: the aggregated statistics we send to Gemini.
            Only group-level numbers, never individual customers.
2. LLM    - `SegmentEmailResponse`: the exact JSON shape Gemini must return for one cluster.
            It is passed to Gemini as `response_schema` AND re-validated by us afterwards.
3. OUTPUT - `SegmentEmailRecord`, `SegmentEmailCampaign`: what is saved to data/processed/segment_emails.json.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- 1. INPUT


class SegmentStats(BaseModel):
    """Aggregated behaviour of a group of customers (a cluster, or all customers as a baseline)."""

    model_config = ConfigDict(extra="forbid")

    customers: int = Field(gt=0)
    avg_total_spend: float = Field(ge=0)
    median_total_spend: float = Field(ge=0)
    avg_order_value: float = Field(ge=0)
    median_order_value: float = Field(ge=0)
    avg_orders_per_customer: float = Field(ge=1)
    median_orders_per_customer: float = Field(ge=1)
    pct_single_order_customers: float = Field(ge=0, le=100)
    avg_items_per_order: float = Field(ge=0)
    avg_recency_days: float = Field(ge=0)
    median_recency_days: float = Field(ge=0)
    pct_ordered_in_last_90_days: float = Field(ge=0, le=100)
    avg_discount_rate_pct: float = Field(ge=0, le=100)
    pct_customers_who_used_a_discount: float = Field(ge=0, le=100)
    avg_rating: float = Field(ge=1, le=5)
    category_share_of_orders_pct: dict[str, float]

    @field_validator("category_share_of_orders_pct")
    @classmethod
    def shares_add_up(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("category mix is empty")
        if abs(sum(v.values()) - 100) > 0.5:  # rounding tolerance
            raise ValueError(f"category shares sum to {sum(v.values()):.2f}%, expected 100%")
        return v


class ClusterProfile(SegmentStats):
    cluster_id: int = Field(ge=0)
    pct_of_all_customers: float = Field(gt=0, le=100)
    # cluster share / overall share for each category: 1.30 = 30% more popular than average
    category_lift_vs_all_customers: dict[str, float]


class ProfileSet(BaseModel):
    """All cluster profiles plus the all-customer baseline, with consistency checks."""

    model_config = ConfigDict(extra="forbid")

    source_files: list[str]
    data_end_date: str
    recency_reference_date: str
    total_customers: int = Field(gt=0)
    total_orders: int = Field(gt=0)
    overall: SegmentStats
    clusters: list[ClusterProfile] = Field(min_length=1)

    @model_validator(mode="after")
    def clusters_are_consistent(self) -> ProfileSet:
        ids = sorted(c.cluster_id for c in self.clusters)
        if ids != list(range(len(ids))):
            raise ValueError(f"cluster ids must be 0..{len(ids) - 1} with no gaps or duplicates, got {ids}")
        if sum(c.customers for c in self.clusters) != self.total_customers:
            raise ValueError("cluster sizes do not add up to total_customers")
        if self.overall.customers != self.total_customers:
            raise ValueError("overall.customers must equal total_customers")
        return self

    def get(self, cluster_id: int) -> ClusterProfile:
        for c in self.clusters:
            if c.cluster_id == cluster_id:
                return c
        raise KeyError(f"no cluster {cluster_id}")


# --------------------------------------------------------------------------- 2. LLM RESPONSE


class SegmentEmailResponse(BaseModel):
    """The JSON object Gemini must return for ONE cluster. Every field is required."""

    cluster_id: int = Field(ge=0, description="Echo the cluster_id given in the prompt.")
    segment_name: str = Field(min_length=3, max_length=60, description="Short, descriptive segment name (2-5 words).")
    typical_customer_description: str = Field(min_length=40, description="2-4 sentences describing a typical customer.")
    purchasing_behavior: str = Field(min_length=20, description="How often, how much and how recently they buy.")
    product_interests: list[str] = Field(min_length=1, max_length=6, description="Product categories they lean towards.")
    marketing_strategy: str = Field(min_length=20, description="Marketing needs and the recommended approach.")
    email_subject: str = Field(min_length=5, max_length=90, description="Email subject line, at most 90 characters.")
    email_greeting: str = Field(min_length=2, max_length=80, description="Greeting line, using the {{first_name}} merge tag.")
    email_body: str = Field(min_length=150, max_length=1500, description="Email body, 80-160 words, plain text.")
    call_to_action: str = Field(min_length=3, max_length=60, description="CTA button text.")
    marketing_angle: str = Field(min_length=3, max_length=120, description="The main persuasion angle.")
    personalization_reason: str = Field(min_length=20, description="Why this email fits this segment, citing its statistics.")

    @field_validator(
        "segment_name", "typical_customer_description", "purchasing_behavior", "marketing_strategy",
        "email_subject", "email_greeting", "email_body", "call_to_action", "marketing_angle",
        "personalization_reason",
    )
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("product_interests")
    @classmethod
    def interests_not_blank(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        if not cleaned:
            raise ValueError("product_interests must contain at least one non-blank item")
        return cleaned


# --------------------------------------------------------------------------- 3. SAVED OUTPUT


class EmailDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1)
    greeting: str = Field(min_length=1)
    body: str = Field(min_length=1)
    call_to_action: str = Field(min_length=1)


class SegmentEmailRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: int = Field(ge=0)
    segment_name: str
    customers: int = Field(gt=0)
    pct_of_all_customers: float
    typical_customer_description: str
    purchasing_behavior: str
    product_interests: list[str]
    marketing_strategy: str
    email: EmailDraft
    marketing_angle: str
    personalization_reason: str

    @classmethod
    def from_response(cls, r: SegmentEmailResponse, profile: ClusterProfile) -> SegmentEmailRecord:
        return cls(
            cluster_id=r.cluster_id,
            segment_name=r.segment_name,
            customers=profile.customers,
            pct_of_all_customers=profile.pct_of_all_customers,
            typical_customer_description=r.typical_customer_description,
            purchasing_behavior=r.purchasing_behavior,
            product_interests=r.product_interests,
            marketing_strategy=r.marketing_strategy,
            email=EmailDraft(subject=r.email_subject, greeting=r.email_greeting, body=r.email_body,
                             call_to_action=r.call_to_action),
            marketing_angle=r.marketing_angle,
            personalization_reason=r.personalization_reason,
        )


class FailedSegment(BaseModel):
    cluster_id: int
    attempts: int
    error: str


class SegmentEmailCampaign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign: str = "Customer Segment Email Campaign"
    status: str = "draft - not sent"
    model: str
    generated_at_utc: str
    merge_tags: dict[str, str] = Field(default_factory=lambda: {
        "{{first_name}}": "Recipient's first name, filled in by the email tool at send time",
    })
    segments: list[SegmentEmailRecord]
    failed_segments: list[FailedSegment] = Field(default_factory=list)

    @model_validator(mode="after")
    def one_email_per_cluster(self) -> SegmentEmailCampaign:
        ids = [s.cluster_id for s in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate cluster_id in segments: {ids}")
        return self
