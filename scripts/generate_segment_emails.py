"""Day 4: write a personalised email draft for every customer segment (K-Means cluster) with Gemini.

Usage:
    python scripts/generate_segment_emails.py                 # all clusters
    python scripts/generate_segment_emails.py --dry-run       # build + print prompts, no API call, no cost
    python scripts/generate_segment_emails.py --clusters 0 2  # only some clusters
    python scripts/generate_segment_emails.py --timeout 60 --max-attempts 3

Reads   data/processed/customer_segments.csv (Day 3) + the raw orders CSV (categories only)
Writes  data/processed/cluster_profiles.json  aggregated statistics sent to the model
        data/processed/segment_emails.json    validated email drafts (nothing is ever sent)
        reports/llm_usage.csv                 tokens + ESTIMATED cost per call (appended)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from headlines import MODEL  # noqa: E402  - Day 1: same GEMINI_MODEL setting (.env) and default model
from src.data.segment_profiles import ProfileDataError, load_profiles, save_profiles  # noqa: E402
from src.llm.generator import (  # noqa: E402
    DEFAULT_TIMEOUT_S, EMAILS_PATH, build_campaign, build_prompt, build_system_instruction, generate_all,
    make_client, save_campaign,
)
from src.llm.retry import RetryPolicy  # noqa: E402
from src.llm.usage_tracker import USAGE_LOG_PATH, UsageLogger  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--clusters", type=int, nargs="+", help="only these cluster ids (default: all)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="seconds per request (default: %(default)s)")
    p.add_argument("--max-attempts", type=int, default=4, help="attempts per cluster incl. the first (default: %(default)s)")
    p.add_argument("--model", default=MODEL, help="Gemini model (default: GEMINI_MODEL from .env or %(default)s)")
    p.add_argument("--dry-run", action="store_true", help="print the prompts and exit without calling the API")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "google_genai", "google.genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # 1-2. load and validate the real Day 3 output
    try:
        profiles = load_profiles()
    except (ProfileDataError, ValueError) as err:
        print(f"Input data problem: {err}", file=sys.stderr)
        return 2
    save_profiles(profiles)
    known = {c.cluster_id for c in profiles.clusters}
    if args.clusters and (unknown := set(args.clusters) - known):
        print(f"Unknown cluster id(s) {sorted(unknown)}; available: {sorted(known)}", file=sys.stderr)
        return 2
    print(f"Loaded {len(profiles.clusters)} cluster profiles ({profiles.total_customers:,} customers, "
          f"{profiles.total_orders:,} orders). Model: {args.model}")

    if args.dry_run:
        print("\n=== SYSTEM INSTRUCTION ===\n" + build_system_instruction(profiles.overall))
        for prof in profiles.clusters:
            if not args.clusters or prof.cluster_id in args.clusters:
                print(f"\n=== PROMPT, cluster {prof.cluster_id} ===\n" + build_prompt(prof, profiles))
        return 0

    # 3-7. prompt -> Gemini -> retry -> validate -> log usage
    try:
        client = make_client(timeout_s=args.timeout)
    except RuntimeError as err:
        sys.exit(str(err))
    logger = UsageLogger()
    results = generate_all(client, profiles, model=args.model, logger=logger,
                           policy=RetryPolicy(max_attempts=args.max_attempts), cluster_ids=args.clusters)

    # 8. save validated emails
    campaign = build_campaign(results, args.model)
    if campaign.segments:
        save_campaign(campaign)

    # 9. summary
    print(f"\n{'cluster':>7}  {'status':<8} {'tries':>5} {'in tok':>7} {'out tok':>7} {'est. cost $':>11}  segment")
    for res, row in zip(results, logger.rows):
        name = res.record.segment_name if res.ok else res.error[:60]
        cost = row["est_total_cost_usd"] or "n/a"
        print(f"{res.cluster_id:>7}  {row['status']:<8} {res.attempts:>5} {row['input_tokens'] or '-':>7} "
              f"{row['output_tokens'] or '-':>7} {cost:>11}  {name}")
    costs = [float(r["est_total_cost_usd"]) for r in logger.rows if r["est_total_cost_usd"]]
    ok = sum(r.ok for r in results)
    print(f"\n{ok} succeeded, {len(results) - ok} failed. Estimated total cost: "
          f"${sum(costs):.6f} (estimate; see {USAGE_LOG_PATH.relative_to(ROOT)})")
    if campaign.segments:
        print(f"Saved {len(campaign.segments)} email draft(s) to {EMAILS_PATH.relative_to(ROOT)} (not sent).")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
