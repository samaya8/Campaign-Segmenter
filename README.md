# Campaign Segmenter

Project 1 of the AI Engineer sprint: Python, SQL, K-Means and a first LLM app for marketing campaigns.

## Status

- [x] **Day 1** — Set up and make the first LLM call (ad headlines as validated JSON)
- [x] **Day 2** — SQL on real campaign data (14 queries, 21 tests, [full report](reports/sql_results.md))
- [ ] Day 3 — Split customers into groups
- [ ] Day 4 — Write an email for each group
- [ ] Day 5 — Finish Project 1 (FastAPI + Docker)

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
cp .env.example .env            # then paste your Gemini API key
```

## Day 1: ad headline generator

```bash
python headlines.py "Noise-cancelling wireless earbuds with 30-hour battery life"
```

Output (validated by Pydantic before it is printed):

```json
{
  "product": "Wireless Earbuds",
  "headlines": [
    { "text": "...", "angle": "benefit" },
    { "text": "...", "angle": "urgency" },
    { "text": "...", "angle": "social proof" }
  ]
}
```

How it stays valid every time:

1. Gemini is called with `response_mime_type="application/json"` and the Pydantic model as `response_schema`.
2. The raw text is re-checked with `HeadlineSet.model_validate_json` (exactly 3 headlines, each ≤ 90 chars).
3. If validation fails, the call is retried (up to 3 attempts).

## Day 2: SQL on real campaign data

### Get the data

The database is not in this repo (it's 14.6 MB, and the source requires a free account).

1. Sign in or register at <https://www.analyticsengineering.com/datasets/marketing-campaigns>.
2. Download **SQLite database** (`marketing-campaigns-v1.0.0.sqlite`).
3. Save it as `data/raw/marketing_campaigns.sqlite`.

Dataset: *Marketing Campaigns & Conversions* v1.0.0, Analytics Engineering (2026), CC BY 4.0.
Scripts open the file read-only, so the original database is never changed. No credentials are needed.

### Run it

```bash
python scripts/inspect_db.py            # tables, columns, row counts, key/relationship checks
python scripts/run_queries.py           # run all queries in sql/queries.sql and print results
python scripts/run_queries.py Q04 Q10   # run selected queries
python scripts/run_queries.py --markdown  # also regenerate reports/query_outputs.md
python -m pytest -v                     # 21 tests: totals, metrics, joins, ranks, zero-division
```

### Files

| File | What it is |
|---|---|
| [`sql/queries.sql`](sql/queries.sql) | 14 commented queries (INNER/LEFT JOIN, CTEs, RANK/DENSE_RANK, PARTITION BY, HAVING, NULLIF) |
| [`reports/sql_results.md`](reports/sql_results.md) | SQL concepts for beginners, every query's real output, and what it means for marketing |
| [`reports/query_outputs.md`](reports/query_outputs.md) | Raw output of every query (generated) |
| `scripts/inspect_db.py` | Schema and data-quality inspection |
| `scripts/run_queries.py` | Runs the queries with `sqlite3` + pandas |
| `tests/test_sql.py` | Checks every result against an independent pandas calculation |

### Data model

```
campaigns (120) 1 ──< daily_spend (11,240)   one row per campaign per day
campaigns       1 ──< leads (261,684)        one row per lead
leads           1 ──o conversions (39,696)   at most one conversion per lead
```

Revenue sits in `conversions`, so it reaches a campaign through `leads`. Spend and revenue are
aggregated to one row per campaign **before** joining. Joining daily rows straight to conversion
rows would repeat each day's spend once per conversion (a test shows a >10× inflation).

### Key results

| Channel | Spend | Revenue | CTR | CPC | CVR | CPA | ROAS |
|---|---:|---:|---:|---:|---:|---:|---:|
| affiliate | $857.8k | $5.61M | 2.50% | $0.36 | 0.43% | $82.87 | **6.54** |
| paid_search | $897.1k | $5.44M | 3.49% | $0.52 | 0.58% | $89.54 | 6.06 |
| paid_social | $984.7k | $4.69M | 1.79% | $0.61 | 0.54% | $113.87 | 4.76 |
| display | $955.3k | $3.73M | 0.60% | $1.09 | 0.77% | $141.65 | 3.91 |
| video | $828.1k | $2.09M | 0.89% | $1.68 | 0.80% | $210.55 | **2.52** |
| **total** | **$4.52M** | **$21.56M** | 1.63% | $0.64 | 0.56% | $113.94 | **4.77** |

- **Affiliate** earns the most revenue and has the best ROAS. **Paid search** has the highest CTR (the top 10 CTR campaigns are all paid search).
- **Video** takes 18.3% of spend for 9.7% of revenue. All 24 video campaigns are below the 4.77 average, as are 22 of 24 display campaigns. These are the channels to investigate first.
- Big spend ≠ big revenue: only 2 of the 10 biggest spenders are top 10 in revenue.
- No campaign loses money (lowest ROAS 1.82) and every campaign has conversions.

## Marketing terms

| Term | Meaning | Formula |
|------|---------|---------|
| **CTR** — Click-through rate | Share of people who saw the ad and clicked it | clicks ÷ impressions |
| **CVR** — Conversion rate | Share of clicks that turned into a sale or sign-up | conversions ÷ clicks |
| **CPC** — Cost per click | What each click cost | spend ÷ clicks |
| **CPA** — Cost per acquisition | What each conversion cost | spend ÷ conversions |
| **ROAS** — Return on ad spend | Revenue earned per unit of spend | revenue ÷ spend |

Example: 10,000 impressions, 200 clicks, 10 sales, $100 spend, $500 revenue →
CTR 2%, CVR 5%, CPC $0.50, CPA $10, ROAS 5.0×.
