"""Turn Day 3's per-customer cluster labels into one aggregated profile per cluster.

Inputs (both read-only, never modified):
  data/processed/customer_segments.csv        - Day 3 output: one row per customer with its cluster
  ecommerce_customer_behavior_dataset_v2.csv  - raw orders, used ONLY for Product_Category and Date

Output: a validated `ProfileSet` - group-level numbers only. No Customer_ID, age, gender or city ever
leaves this module, so no personal information can reach the LLM.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.llm.schemas import ClusterProfile, ProfileSet, SegmentStats

ROOT = Path(__file__).resolve().parents[2]
SEGMENTS_PATH = ROOT / "data" / "processed" / "customer_segments.csv"
ORDERS_PATH = ROOT / "ecommerce_customer_behavior_dataset_v2.csv"
PROFILES_PATH = ROOT / "data" / "processed" / "cluster_profiles.json"

SEGMENT_COLUMNS = ["Customer_ID", "cluster", "frequency", "recency_days", "avg_order_value",
                   "avg_quantity", "discount_rate", "total_spend", "avg_rating"]
ORDER_COLUMNS = ["Customer_ID", "Product_Category", "Date"]   # nothing personal is loaded
ACTIVE_WINDOW_DAYS = 90


class ProfileDataError(ValueError):
    """The input files are missing, incomplete or inconsistent."""


def load_segments(path: Path = SEGMENTS_PATH) -> pd.DataFrame:
    if not path.exists():
        raise ProfileDataError(f"{path} not found - run the Day 3 notebook first")
    df = pd.read_csv(path)
    missing = set(SEGMENT_COLUMNS) - set(df.columns)
    if missing:
        raise ProfileDataError(f"{path.name} is missing columns: {sorted(missing)}")
    df = df[SEGMENT_COLUMNS]
    if df.isna().any().any():
        raise ProfileDataError(f"{path.name} has missing values in: {df.columns[df.isna().any()].tolist()}")
    if df["Customer_ID"].duplicated().any():
        raise ProfileDataError(f"{path.name} has duplicated Customer_ID rows")
    if not pd.api.types.is_integer_dtype(df["cluster"]) or (df["cluster"] < 0).any():
        raise ProfileDataError("cluster labels must be non-negative integers")
    if (df["frequency"] < 1).any() or (df[["recency_days", "avg_order_value", "total_spend"]] < 0).any().any():
        raise ProfileDataError("negative spend/recency or a customer with zero orders")
    if not df["discount_rate"].between(0, 1).all():
        raise ProfileDataError("discount_rate must be between 0 and 1")
    return df


def load_orders(path: Path = ORDERS_PATH) -> pd.DataFrame:
    if not path.exists():
        raise ProfileDataError(f"{path} not found (see README, Day 3)")
    df = pd.read_csv(path, usecols=ORDER_COLUMNS, parse_dates=["Date"])
    if df.isna().any().any():
        raise ProfileDataError(f"{path.name} has missing Customer_ID/Product_Category/Date values")
    return df


def _stats(customers: pd.DataFrame, orders: pd.DataFrame) -> dict:
    """Group-level statistics for any set of customers and their orders."""
    cat = orders["Product_Category"].value_counts(normalize=True).mul(100).round(1)
    return {
        "customers": len(customers),
        "avg_total_spend": round(customers["total_spend"].mean(), 2),
        "median_total_spend": round(customers["total_spend"].median(), 2),
        "avg_order_value": round(customers["avg_order_value"].mean(), 2),
        "median_order_value": round(customers["avg_order_value"].median(), 2),
        "avg_orders_per_customer": round(customers["frequency"].mean(), 2),
        "median_orders_per_customer": float(customers["frequency"].median()),
        "pct_single_order_customers": round((customers["frequency"] == 1).mean() * 100, 1),
        "avg_items_per_order": round(customers["avg_quantity"].mean(), 2),
        "avg_recency_days": round(customers["recency_days"].mean(), 1),
        "median_recency_days": float(customers["recency_days"].median()),
        "pct_ordered_in_last_90_days": round((customers["recency_days"] <= ACTIVE_WINDOW_DAYS).mean() * 100, 1),
        "avg_discount_rate_pct": round(customers["discount_rate"].mean() * 100, 2),
        "pct_customers_who_used_a_discount": round((customers["discount_rate"] > 0).mean() * 100, 1),
        "avg_rating": round(customers["avg_rating"].mean(), 2),
        "category_share_of_orders_pct": cat.to_dict(),
    }


def build_profiles(segments: pd.DataFrame, orders: pd.DataFrame,
                   source_files: list[str] | None = None) -> ProfileSet:
    """Aggregate customers into one `ClusterProfile` per cluster, plus an all-customer baseline."""
    orders = orders.merge(segments[["Customer_ID", "cluster"]], on="Customer_ID", how="left")
    if orders["cluster"].isna().any():
        raise ProfileDataError(f"{int(orders['cluster'].isna().sum())} orders belong to customers without a cluster")
    order_counts = orders.groupby("Customer_ID").size()
    if not order_counts.reindex(segments["Customer_ID"]).eq(segments["frequency"].values).all():
        raise ProfileDataError("order counts in the raw data do not match `frequency` in the segments file")

    overall = _stats(segments, orders)
    overall_cat = pd.Series(overall["category_share_of_orders_pct"])

    clusters = []
    for cid, members in segments.groupby("cluster"):
        stats = _stats(members, orders[orders["cluster"] == cid])
        lift = (pd.Series(stats["category_share_of_orders_pct"]) / overall_cat).round(2)
        clusters.append(ClusterProfile(
            cluster_id=int(cid),
            pct_of_all_customers=round(len(members) / len(segments) * 100, 1),
            category_lift_vs_all_customers=lift.sort_values(ascending=False).to_dict(),
            **stats,
        ))

    end = orders["Date"].max()
    return ProfileSet(
        source_files=source_files or [],
        data_end_date=end.date().isoformat(),
        recency_reference_date=(end + pd.Timedelta(days=1)).date().isoformat(),  # same rule as the Day 3 notebook
        total_customers=len(segments),
        total_orders=len(orders),
        overall=SegmentStats(**overall),
        clusters=clusters,
    )


def load_profiles(segments_path: Path = SEGMENTS_PATH, orders_path: Path = ORDERS_PATH) -> ProfileSet:
    return build_profiles(
        load_segments(segments_path), load_orders(orders_path),
        source_files=[p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else p.name
                      for p in (segments_path, orders_path)],
    )


def save_profiles(profiles: ProfileSet, path: Path = PROFILES_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profiles.model_dump_json(indent=2), encoding="utf-8")
    return path
