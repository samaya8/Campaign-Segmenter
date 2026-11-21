"""Load the saved K-Means results and turn them into API-ready segment summaries.

The API does NOT re-run K-Means. It reads the model's saved outputs (committed to the repo, so a fresh
clone works without the raw data):

  data/processed/cluster_profiles.json   Day 3/4: aggregated statistics per cluster (required)
  config/segment_names.json              Day 3: human-readable names (optional, checked against cluster sizes)
  data/processed/segment_emails.json     Day 4: Gemini's segment names (optional)

Only group-level numbers are read here; there are no customer IDs or personal data in these files.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from src.api.schemas import CategoryPreference, Segment, SegmentCharacteristics, SegmentsResponse
from src.llm.generator import EMAILS_PATH
from src.llm.schemas import ClusterProfile, ProfileSet, SegmentEmailCampaign

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
PROFILES_PATH = ROOT / "data" / "processed" / "cluster_profiles.json"
NAMES_PATH = ROOT / "config" / "segment_names.json"

# Same rule the Day 4 prompt gives Gemini: only a lift ABOVE 1.15 counts as a real category preference.
PREFERENCE_LIFT = 1.15


class SegmentDataError(RuntimeError):
    """The saved segmentation output is missing or invalid."""


@dataclass(frozen=True)
class SegmentLabel:
    name: str
    description: str | None = None


@dataclass(frozen=True)
class SegmentStore:
    profiles: ProfileSet
    labels: dict[int, SegmentLabel] = field(default_factory=dict)
    ai_names: dict[int, str] = field(default_factory=dict)

    @property
    def cluster_ids(self) -> list[int]:
        return [c.cluster_id for c in self.profiles.clusters]


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name


def load_profiles_file(path: Path) -> ProfileSet:
    if not path.exists():
        raise SegmentDataError(
            f"{_rel(path)} not found. It is committed to the repo; to rebuild it run the Day 3 notebook, "
            "then `python scripts/generate_segment_emails.py --dry-run`.")
    try:
        return ProfileSet.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError) as err:
        raise SegmentDataError(f"{_rel(path)} is not a valid cluster profile file: {str(err)[:300]}") from err


def load_labels(path: Path, profiles: ProfileSet) -> dict[int, SegmentLabel]:
    """Day 3 names. A name is used only if its cluster still has the same number of customers."""
    if not path.exists():
        return {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["clusters"]
    except (ValueError, KeyError, TypeError) as err:
        log.warning("ignoring %s: %s", _rel(path), err)
        return {}
    sizes = {c.cluster_id: c.customers for c in profiles.clusters}
    labels = {}
    for e in entries:
        cid = e.get("cluster_id")
        if cid in sizes and e.get("customers") == sizes[cid] and e.get("name"):
            labels[cid] = SegmentLabel(e["name"], e.get("description"))
        else:
            log.warning("segment name for cluster %s does not match the current model; using a generic name", cid)
    return labels


def load_ai_names(path: Path, profiles: ProfileSet) -> dict[int, str]:
    if not path.exists():
        return {}
    try:
        campaign = SegmentEmailCampaign.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError) as err:
        log.warning("ignoring %s: %s", _rel(path), str(err)[:200])
        return {}
    sizes = {c.cluster_id: c.customers for c in profiles.clusters}
    return {s.cluster_id: s.segment_name for s in campaign.segments if sizes.get(s.cluster_id) == s.customers}


def load_store(profiles_path: Path | None = None, names_path: Path | None = None,
               emails_path: Path | None = None) -> SegmentStore:
    """Paths default to the module constants, looked up at call time (so tests can point them elsewhere)."""
    profiles = load_profiles_file(profiles_path or PROFILES_PATH)
    return SegmentStore(
        profiles=profiles,
        labels=load_labels(names_path or NAMES_PATH, profiles),
        ai_names=load_ai_names(emails_path or EMAILS_PATH, profiles),
    )


def _preferences(profile: ClusterProfile) -> list[CategoryPreference]:
    prefs = [
        CategoryPreference(category=cat, lift_vs_all_customers=lift,
                           share_of_orders_pct=profile.category_share_of_orders_pct.get(cat, 0.0))
        for cat, lift in profile.category_lift_vs_all_customers.items()
        if lift > PREFERENCE_LIFT
    ]
    return sorted(prefs, key=lambda p: p.lift_vs_all_customers, reverse=True)


def summarize(store: SegmentStore) -> SegmentsResponse:
    p = store.profiles
    segments = []
    for c in sorted(p.clusters, key=lambda c: c.cluster_id):
        label = store.labels.get(c.cluster_id, SegmentLabel(f"Cluster {c.cluster_id}"))
        prefs = _preferences(c)
        segments.append(Segment(
            cluster_id=c.cluster_id,
            segment_name=label.name,
            description=label.description,
            ai_segment_name=store.ai_names.get(c.cluster_id),
            customer_count=c.customers,
            percentage=c.pct_of_all_customers,
            characteristics=SegmentCharacteristics(**c.model_dump(include=set(SegmentCharacteristics.model_fields))),
            top_category=prefs[0].category if prefs else None,
            preferred_categories=prefs,
        ))
    return SegmentsResponse(
        total_customers=p.total_customers,
        total_orders=p.total_orders,
        n_clusters=len(p.clusters),
        data_end_date=p.data_end_date,
        preference_lift_threshold=PREFERENCE_LIFT,
        segments=segments,
    )
