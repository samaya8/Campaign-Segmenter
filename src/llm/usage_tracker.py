"""Token usage and estimated cost logging for every LLM call.

Tokens are the pieces of text a model reads and writes (roughly 4 characters of English each).
  input tokens  = everything we SEND (instructions + cluster statistics)        -> Gemini `prompt_token_count`
  output tokens = everything the model WRITES (visible JSON + hidden "thinking")  -> `candidates_token_count`
                                                                                  + `thoughts_token_count`
Google bills thinking tokens at the output rate, so they are counted as output here.

  estimated cost = input_tokens  / 1,000,000 * input price per 1M
                 + output_tokens / 1,000,000 * output price per 1M

Token counts come ONLY from the `usage_metadata` Gemini returns. If it is missing, the CSV cells are left
empty. We never guess. Prices come from config/llm_pricing.json; if the model is not listed there,
the cost is left empty too.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PRICING_PATH = ROOT / "config" / "llm_pricing.json"
USAGE_LOG_PATH = ROOT / "reports" / "llm_usage.csv"

CSV_COLUMNS = [
    "run_id", "timestamp_utc", "cluster_id", "model", "status", "attempts", "retries",
    "input_tokens", "output_tokens", "thinking_tokens_in_output", "total_tokens",
    "input_price_per_1m_usd", "output_price_per_1m_usd",
    "est_input_cost_usd", "est_output_cost_usd", "est_total_cost_usd", "note", "error",
]


# --------------------------------------------------------------------------- tokens


@dataclass
class TokenUsage:
    """Token counts summed over every response received for one cluster (including retried ones,
    because a reply that failed validation was still billed)."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    total_tokens: int | None = None
    responses_with_usage: int = 0
    responses_without_usage: int = 0

    def add(self, usage_metadata: Any) -> None:
        prompt = getattr(usage_metadata, "prompt_token_count", None) if usage_metadata else None
        if prompt is None:
            self.responses_without_usage += 1
            return
        visible = getattr(usage_metadata, "candidates_token_count", None) or 0
        thoughts = getattr(usage_metadata, "thoughts_token_count", None) or 0
        total = getattr(usage_metadata, "total_token_count", None)
        self.input_tokens = (self.input_tokens or 0) + prompt
        self.output_tokens = (self.output_tokens or 0) + visible + thoughts
        self.thinking_tokens = (self.thinking_tokens or 0) + thoughts
        self.total_tokens = (self.total_tokens or 0) + (total if total is not None else prompt + visible + thoughts)
        self.responses_with_usage += 1

    @property
    def available(self) -> bool:
        return self.responses_with_usage > 0


# --------------------------------------------------------------------------- pricing


@dataclass(frozen=True)
class ModelPrice:
    input_per_1m: float
    output_per_1m: float


@dataclass
class PriceTable:
    models: dict[str, list[dict]] = field(default_factory=dict)
    source: str = ""

    @classmethod
    def load(cls, path: Path = PRICING_PATH) -> PriceTable:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(models=data.get("models", {}), source=data.get("source", ""))

    def price_for(self, model: str, on: date | None = None) -> ModelPrice | None:
        """Price that applies to `model` on date `on` (today by default), or None if unknown."""
        on = on or datetime.now(timezone.utc).date()
        for p in self.models.get(model, []):
            start = date.fromisoformat(p["effective_from"]) if p.get("effective_from") else date.min
            end = date.fromisoformat(p["effective_to"]) if p.get("effective_to") else date.max
            if start <= on <= end:
                return ModelPrice(float(p["input_per_1m"]), float(p["output_per_1m"]))
        return None


@dataclass(frozen=True)
class CostEstimate:
    input_cost: float
    output_cost: float

    @property
    def total(self) -> float:
        return self.input_cost + self.output_cost


def estimate_cost(usage: TokenUsage, price: ModelPrice | None) -> CostEstimate | None:
    if price is None or not usage.available:
        return None
    return CostEstimate(
        input_cost=usage.input_tokens / 1_000_000 * price.input_per_1m,
        output_cost=usage.output_tokens / 1_000_000 * price.output_per_1m,
    )


# --------------------------------------------------------------------------- CSV log


class UsageLogger:
    """Appends one row per cluster call to reports/llm_usage.csv (history is kept across runs)."""

    def __init__(self, path: Path = USAGE_LOG_PATH, prices: PriceTable | None = None, run_id: str | None = None):
        self.path = path
        self.prices = prices if prices is not None else PriceTable.load()
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.rows: list[dict] = []

    def log(self, *, cluster_id: int, model: str, status: str, attempts: int, usage: TokenUsage,
            error: str = "") -> dict:
        now = datetime.now(timezone.utc)
        price = self.prices.price_for(model, now.date())
        cost = estimate_cost(usage, price)

        notes = []
        if not usage.available:
            notes.append("no usage metadata returned - tokens and cost unknown")
        elif usage.responses_without_usage:
            notes.append(f"{usage.responses_without_usage} response(s) had no usage metadata - totals are a lower bound")
        if price is None:
            notes.append(f"no price configured for {model} - cost unknown")
        if cost is not None:
            notes.append("ESTIMATED cost from config/llm_pricing.json")

        row = {
            "run_id": self.run_id,
            "timestamp_utc": now.isoformat(timespec="seconds"),
            "cluster_id": cluster_id,
            "model": model,
            "status": status,
            "attempts": attempts,
            "retries": max(attempts - 1, 0),
            "input_tokens": usage.input_tokens if usage.available else "",
            "output_tokens": usage.output_tokens if usage.available else "",
            "thinking_tokens_in_output": usage.thinking_tokens if usage.available else "",
            "total_tokens": usage.total_tokens if usage.available else "",
            "input_price_per_1m_usd": price.input_per_1m if price else "",
            "output_price_per_1m_usd": price.output_per_1m if price else "",
            "est_input_cost_usd": f"{cost.input_cost:.6f}" if cost else "",
            "est_output_cost_usd": f"{cost.output_cost:.6f}" if cost else "",
            "est_total_cost_usd": f"{cost.total:.6f}" if cost else "",
            "note": "; ".join(notes),
            "error": error[:300],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        self.rows.append(row)
        return row
