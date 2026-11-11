# Day 2 · SQL on Real Campaign Data — Results and Explanations

Every number in this report comes from running [`sql/queries.sql`](../sql/queries.sql) against
`data/raw/marketing_campaigns.sqlite` with `python scripts/run_queries.py`.
The full raw output of every query (including all 120 rows of Q05 and all 36 months of Q09) is in
[`query_outputs.md`](query_outputs.md).

**Dataset:** Marketing Campaigns & Conversions v1.0.0, Analytics Engineering (2026), CC BY 4.0.

---

## Contents

1. [The data](#1-the-data)
2. [SQL concepts, explained for beginners](#2-sql-concepts-explained-for-beginners)
3. [The metrics and the two traps](#3-the-metrics-and-the-two-traps)
4. [Query-by-query results](#4-query-by-query-results)
5. [Answers to the 7 business questions](#5-answers-to-the-7-business-questions)
6. [How the results were tested](#6-how-the-results-were-tested)

---

## 1. The data

Output of `python scripts/inspect_db.py`:

| Table | Rows | One row is… | Key | Links to |
|---|---:|---|---|---|
| `campaigns` | 120 | a campaign | `campaign_id` | — |
| `daily_spend` | 11,240 | one campaign on one day | `spend_id` | `campaign_id` → campaigns |
| `leads` | 261,684 | one person who signed up | `lead_id` | `campaign_id` → campaigns |
| `conversions` | 39,696 | one lead who converted | `conversion_id` | `lead_id` → leads |

```
campaigns 1 ──< daily_spend        (a campaign has many days)
campaigns 1 ──< leads              (a campaign has many leads)
leads     1 ──o conversions        (a lead converts at most once)
```

What the inspection found:

- **No keys are declared in the file.** SQLite reports `pk=0` and no foreign keys, so the keys
  above are *logical* keys. The inspect script checked them itself: every ID is unique, every
  `daily_spend` and `leads` row points to a real campaign, every conversion points to a real lead,
  and no lead converts twice.
- **Revenue has no `campaign_id`.** To credit revenue to a campaign you must go
  `conversions → leads → campaigns`. That's an important design point for every revenue query.
- 5 channels × 24 campaigns each (affiliate, display, paid_search, paid_social, video).
  3 objectives (awareness 44, lead_gen 41, acquisition 35). Spend runs 2023-01-04 → 2025-12-02.
- Conversions are either `trial` (23,005, revenue always 0) or `purchase` (16,691, $99–$2,499).
- No zero-click or zero-impression days, no spend outside campaign dates, and every campaign
  has at least one purchase.

---

## 2. SQL concepts, explained for beginners

Think of a table as a spreadsheet. SQL is how you ask questions of it. Every query is built from
the same few pieces, and the database always runs them in this order (not the order you type them):

```
FROM / JOIN  →  WHERE  →  GROUP BY  →  HAVING  →  SELECT  →  window functions  →  ORDER BY  →  LIMIT
  which rows    filter    make groups  filter     pick/compute  rank, running     sort         keep top N
  to start      rows      of rows      groups     columns       totals
```

Knowing this order explains most beginner errors. For example, you can't use a `SELECT` alias inside `WHERE`,
because `WHERE` runs first.

### SELECT — choose what to show
```sql
SELECT campaign_name, channel FROM campaigns;
```
"From the `campaigns` table, give me these two columns." `SELECT` can also compute new columns:
`SELECT clicks * 100.0 / impressions AS ctr_pct` creates a column named `ctr_pct`. `AS` names it.

### WHERE — keep only some rows
```sql
SELECT * FROM daily_spend WHERE campaign_id = 'CMP0001';
```
`WHERE` filters **individual rows before any grouping**. Think of it as a filter on the spreadsheet.

### Aggregate functions — collapse many rows into one number
`SUM()`, `COUNT()`, `AVG()`, `MIN()`, `MAX()`.
```sql
SELECT SUM(spend) FROM daily_spend;          -- one number: total spend
```
`COUNT(*)` counts rows. `COUNT(DISTINCT campaign_id)` counts unique values. `COUNT(column)` counts
non-NULL values (Q04 relies on this).

### GROUP BY — one result row per group
```sql
SELECT campaign_id, SUM(spend) FROM daily_spend GROUP BY campaign_id;
```
"Put the rows into piles by `campaign_id`, then `SUM` each pile." Rule: every column in `SELECT` must
either be in `GROUP BY` or be inside an aggregate function.

### HAVING — filter groups (after GROUP BY)
```sql
SELECT channel, COUNT(*) FROM campaigns GROUP BY channel HAVING COUNT(*) >= 5;
```
`WHERE` filters rows **before** grouping. `HAVING` filters groups **after**, so it can use aggregates.
You can't write `WHERE COUNT(*) >= 5`, because the count doesn't exist yet when `WHERE` runs.
(Used in Q08 and Q14.)

### ORDER BY and LIMIT — sort and keep the top
```sql
... ORDER BY revenue DESC LIMIT 10;
```
`DESC` = biggest first, `ASC` (default) = smallest first. `LIMIT 10` keeps the first 10 rows.

### INNER JOIN — combine tables, keep only matches
```sql
FROM daily_spend AS d
INNER JOIN campaigns AS c ON c.campaign_id = d.campaign_id
```
For each `daily_spend` row, find the campaign with the same `campaign_id` and glue its columns on
(like a VLOOKUP). Rows with **no match on either side are dropped**. `AS d` and `AS c` are short nicknames.
(Q02, Q04, Q05, Q10…)

### LEFT JOIN — keep every row from the left table
```sql
FROM campaigns AS c
LEFT JOIN conversions_by_campaign AS k ON k.campaign_id = c.campaign_id
```
Keeps **every campaign**, even with no match. Where there is no match, the right-side columns are
`NULL`. That makes it the tool for "find X with no Y": `LEFT JOIN … WHERE k.campaign_id IS NULL` (Q07).
`COALESCE(x, 0)` turns those `NULL`s into 0.

### CTE (WITH) — name a step, then use it like a table
```sql
WITH spend_by_campaign AS (
    SELECT campaign_id, SUM(spend) AS spend FROM daily_spend GROUP BY campaign_id
)
SELECT * FROM spend_by_campaign WHERE spend > 50000;
```
A CTE is a temporary, named result that exists only for this one query. It lets you write a hard
query as small readable steps (step 1, step 2, step 3) instead of nesting subqueries inside each
other. You can chain several, separated by commas (Q04, Q09, Q13).

### Window functions — calculate across rows *without* collapsing them
`GROUP BY` squashes 120 campaigns into 5 channel rows. A window function keeps all 120 rows and
adds a column computed from other rows, like a rank.
```sql
RANK() OVER (ORDER BY ctr_pct DESC)
```
"Sort all rows by CTR, highest first, and write each row's position next to it."

### PARTITION BY — restart the window for each group
```sql
RANK() OVER (PARTITION BY channel ORDER BY ctr_pct DESC)
```
Same as above, but the ranking **restarts at 1 for every channel**. It's like `GROUP BY` for windows,
except the rows are not collapsed. (Q10, Q11)

### RANK() vs DENSE_RANK()
Both give ties the same number. After a tie:
- `RANK()` **skips** numbers: 1, 2, 2, **4**
- `DENSE_RANK()` **doesn't skip**: 1, 2, 2, **3**

Q10 uses `RANK()`, Q12 uses `DENSE_RANK()`.
**Gotcha:** you can't put a window function in `WHERE` (it runs after `WHERE`). Rank inside a CTE
first, then filter in the outer query. Q11 shows this pattern.

### NULLIF — avoid dividing by zero
```sql
clicks * 1.0 / NULLIF(impressions, 0)
```
`NULLIF(a, b)` returns `NULL` when `a = b`, otherwise `a`. So a zero denominator becomes `NULL`, and
anything divided by `NULL` is `NULL`. You get "no answer" instead of a crash or a misleading 0.
This dataset has no zero denominators, but the tests feed every query a tiny fake database that
does, to prove it holds.

### CASE WHEN — if/else inside SQL
```sql
SUM(CASE WHEN conversion_type = 'purchase' THEN 1 ELSE 0 END) AS purchases
```
Counts only the rows that match a condition (Q03, Q14).

---

## 3. The metrics and the two traps

| Metric | Formula | Plain English |
|---|---|---|
| CTR | clicks / impressions × 100 | % of people who saw the ad and clicked |
| CPC | spend / clicks | cost of one click |
| CVR | conversions / clicks × 100 | % of clicks that became a trial or purchase |
| CPA | spend / conversions | cost of one conversion |
| ROAS | revenue / spend | $ of revenue per $1 of ads |

"Conversions" counts trials + purchases. Only purchases have revenue.

**Trap 1: averaging ratios.** If day 1 has CTR 10% on 100 impressions and day 2 has CTR 1% on
10,000 impressions, the average of the two CTRs is 5.5%, but the true CTR is 110 / 10,100 = 1.09%.
Always compute `SUM(clicks) / SUM(impressions)`. Every ratio in `queries.sql` does this.

**Trap 2: joining tables with different grain.** `daily_spend` has one row per **day**, while
`conversions` has one row per **conversion**. Join them directly on campaign and every day's spend gets
repeated once per conversion. The test `test_naive_join_would_double_count` shows that for a
single campaign (CMP0001) this inflates spend more than 10×. The fix, used everywhere:
**aggregate each table to one row per campaign first (in a CTE), then join** 1-to-1.

**Trap 3 (SQLite-specific): integer division.** Some `spend` and `revenue` values are stored as
integers, and in SQLite `7 / 2 = 3`. Writing `* 1.0` or `* 100.0` forces decimal maths.

---

## 4. Query-by-query results

### Q01 — Total advertising spend across all campaigns
**Does:** adds up every row of `daily_spend`.
**Concepts:** `SELECT`, `SUM`, `COUNT(DISTINCT)`, `MIN`/`MAX`, `ROUND`.

| campaigns | active_campaign_days | first_day | last_day | total_impressions | total_clicks | total_spend |
|---:|---:|---|---|---:|---:|---:|
| 120 | 11,240 | 2023-01-04 | 2025-12-02 | 436,844,819 | 7,102,319 | 4,523,050.02 |

**Means:** about $4.52M was spent over roughly 3 years to buy 437M impressions and 7.1M clicks.
Portfolio-wide that's a CTR of 1.63% and a CPC of $0.64.

### Q02 — Total clicks by channel
**Does:** joins each day to its campaign to learn the channel, then sums clicks per channel.
**Concepts:** `INNER JOIN`, `GROUP BY`, `ORDER BY`, a subquery for the share-of-total.

| channel | clicks | % of all clicks |
|---|---:|---:|
| affiliate | 2,386,856 | 33.61 |
| paid_search | 1,740,314 | 24.50 |
| paid_social | 1,602,785 | 22.57 |
| display | 879,874 | 12.39 |
| video | 492,490 | 6.93 |

**Means:** affiliate drives a third of all traffic. Video drives under 7% of clicks while taking
18% of spend, which is an early warning sign.

### Q03 — Total conversions and revenue
**Does:** counts conversions, splits them into purchases and trials with `CASE`, and sums revenue.
**Concepts:** `COUNT`, `SUM(CASE WHEN …)`, `NULLIF`.

| total_conversions | purchases | trials | total_revenue | avg_order_value |
|---:|---:|---:|---:|---:|
| 39,696 | 16,691 | 23,005 | 21,561,839.01 | 1,291.82 |

**Means:** $21.56M of revenue against $4.52M spend gives a portfolio **ROAS of 4.77**. 58% of
conversions are free trials. Revenue comes only from the 16,691 purchases, at about $1,292 each.
The avg order value is almost identical in every channel ($1,274–$1,301), so channels differ in
*how many* purchases they produce per dollar, not in how big the purchases are.

### Q04 — Full funnel metrics and ROAS by channel ⭐
**Does:** step 1 totals spend, clicks and impressions per campaign. Step 2 totals conversions and
revenue per campaign (via leads). Step 3 joins them 1-to-1 and rolls up to channel.
**Concepts:** CTEs, `INNER JOIN`, `LEFT JOIN`, `NULLIF`, `COALESCE`, aggregate-then-join.

| channel | spend | revenue | CTR % | CPC | CVR % | CPA | ROAS |
|---|---:|---:|---:|---:|---:|---:|---:|
| affiliate | 857,835.07 | 5,610,700.73 | 2.50 | 0.36 | 0.43 | 82.87 | **6.54** |
| paid_search | 897,091.53 | 5,439,329.35 | 3.49 | 0.52 | 0.58 | 89.54 | 6.06 |
| paid_social | 984,731.88 | 4,690,137.28 | 1.79 | 0.61 | 0.54 | 113.87 | 4.76 |
| display | 955,297.00 | 3,734,117.43 | 0.60 | 1.09 | 0.77 | 141.65 | 3.91 |
| video | 828,094.54 | 2,087,554.22 | 0.89 | 1.68 | 0.80 | 210.55 | **2.52** |

**Means:** spend is spread almost evenly (18–22% per channel), but returns are not. Affiliate and
paid search earn $6+ per $1. Video earns $2.52. Video and display actually convert clicks *better*
(highest CVR), but their clicks are 2–5× more expensive, so CPA ends up high. Cheap clicks win here.

### Q05 — Total spend and clicks for each campaign
**Does:** one row per campaign with spend, clicks and % of budget used (120 rows, top 10 shown).
**Concepts:** `INNER JOIN`, `GROUP BY` on several columns, `ORDER BY`.

| campaign_id | campaign_name | channel | spend | clicks | budget | % of budget used |
|---|---|---|---:|---:|---:|---:|
| CMP0093 | Display 2024 093 | display | 72,153.85 | 65,717 | 78,979.48 | 91.4 |
| CMP0021 | Paid Search 2024 021 | paid_search | 71,337.50 | 137,678 | 76,193.25 | 93.6 |
| CMP0102 | Paid Social 2025 102 | paid_social | 67,070.47 | 110,317 | 75,494.94 | 88.8 |
| CMP0107 | Paid Social 2024 107 | paid_social | 66,794.72 | 110,441 | 74,343.86 | 89.8 |
| CMP0041 | Paid Search 2024 041 | paid_search | 66,691.10 | 125,449 | 74,625.44 | 89.4 |
| CMP0024 | Video 2024 024 | video | 66,524.17 | 39,676 | 77,960.53 | 85.3 |
| CMP0073 | Display 2023 073 | display | 64,824.15 | 58,897 | 79,487.06 | 81.6 |
| CMP0117 | Paid Social 2023 117 | paid_social | 64,353.11 | 105,202 | 78,212.52 | 82.3 |
| CMP0008 | Display 2023 008 | display | 62,684.63 | 57,850 | 76,141.89 | 82.3 |
| CMP0060 | Affiliate 2024 060 | affiliate | 62,309.32 | 174,518 | 79,884.92 | 78.0 |

**Means:** campaign spend ranges from $6.1k (CMP0084) to $72.2k (CMP0093). Every campaign stayed
within budget (69.9–98.7% used). The same spend buys very different traffic: $66.5k bought 39.7k clicks
on video (CMP0024) but $62.3k bought 174.5k clicks on affiliate (CMP0060).

### Q06 — Top 10 campaigns by revenue
**Does:** builds spend and revenue per campaign in separate CTEs, joins them, sorts by revenue.
**Concepts:** CTEs, `LEFT JOIN`, `ORDER BY … DESC`, `LIMIT`.

| campaign_id | campaign_name | channel | objective | revenue | spend | purchases | ROAS |
|---|---|---|---|---:|---:|---:|---:|
| CMP0060 | Affiliate 2024 060 | affiliate | acquisition | 417,738.92 | 62,309.32 | 326 | 6.70 |
| CMP0050 | Affiliate 2023 050 | affiliate | awareness | 388,919.88 | 35,219.40 | 299 | 11.04 |
| CMP0021 | Paid Search 2024 021 | paid_search | awareness | 380,892.89 | 71,337.50 | 291 | 5.34 |
| CMP0061 | Paid Search 2023 061 | paid_search | lead_gen | 369,829.93 | 48,907.03 | 283 | 7.56 |
| CMP0081 | Paid Search 2023 081 | paid_search | awareness | 361,820.39 | 43,017.04 | 278 | 8.41 |
| CMP0020 | Affiliate 2025 020 | affiliate | lead_gen | 348,047.15 | 60,255.89 | 282 | 5.78 |
| CMP0110 | Affiliate 2025 110 | affiliate | acquisition | 330,555.85 | 30,384.26 | 257 | 10.88 |
| CMP0062 | Paid Social 2023 062 | paid_social | lead_gen | 321,241.52 | 49,907.15 | 246 | 6.44 |
| CMP0015 | Affiliate 2024 015 | affiliate | awareness | 319,263.94 | 58,887.07 | 244 | 5.42 |
| CMP0056 | Paid Search 2025 056 | paid_search | acquisition | 316,984.23 | 52,535.84 | 237 | 6.03 |

**Means:** 9 of the top 10 earners are affiliate or paid search; there are no video or display
campaigns here. CMP0050 and CMP0110 are the standouts: about $11 back per $1 on below-average spend,
so they're prime candidates for **more** budget.

### Q07 — High-spend campaigns with no conversions
**Does:** keeps every campaign (`LEFT JOIN`) and keeps only those where the conversion side found
no match (`IS NULL`) **and** spend is above the average campaign's.
**Concepts:** `LEFT JOIN … IS NULL` (anti-join), a subquery inside `WHERE`.

**Result: 0 rows.** This is a real result, not a bug: every one of the 120 campaigns has at least one
conversion and at least one purchase (confirmed during inspection). The test
`test_q07_finds_high_spend_campaign_without_conversions` runs this exact query on a tiny fake
database that *does* contain such a campaign and checks it is found.
**Means:** there is no "dead" spend in this portfolio. Waste here is about campaigns that convert
*inefficiently*, which Q13 and Q14 cover.

### Q08 — Average campaign performance by channel
**Does:** first makes one row per campaign (totals), then averages those rows per channel.
**Concepts:** CTE, `AVG` of campaign-level totals, `MIN`/`MAX`, `HAVING`.

| channel | campaigns | avg spend | avg clicks | avg conversions | avg revenue | channel ROAS | worst campaign ROAS | best campaign ROAS |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| affiliate | 24 | 35,743.13 | 99,452 | 431 | 233,779.20 | 6.54 | 3.05 | 13.01 |
| paid_search | 24 | 37,378.81 | 72,513 | 417 | 226,638.72 | 6.06 | 2.65 | 10.85 |
| paid_social | 24 | 41,030.50 | 66,783 | 360 | 195,422.39 | 4.76 | 2.48 | 6.93 |
| display | 24 | 39,804.04 | 36,661 | 281 | 155,588.23 | 3.91 | 2.17 | 4.86 |
| video | 24 | 34,503.94 | 20,520 | 164 | 86,981.43 | 2.52 | 1.82 | 3.49 |

**Means:** a typical video campaign costs about the same as a typical affiliate campaign ($34.5k vs $35.7k)
but returns well under half the revenue ($87k vs $234k). Even the **best** video campaign (ROAS 3.49)
is below the *average* of display, paid social, paid search and affiliate. This is a channel-level
problem, not a few bad campaigns.

### Q09 — Monthly advertising spend and revenue (CTE)
**Does:** one CTE totals spend per month, another totals revenue per month, then they're joined
on the month.
**Concepts:** two CTEs, `strftime('%Y-%m', date)` to turn a date into a month, `LEFT JOIN`.
36 rows, all in `query_outputs.md`. Highlights:

| month | spend | clicks | conversions | revenue | ROAS |
|---|---:|---:|---:|---:|---:|
| 2023-01 | 23,686.55 | 26,846 | 110 | 52,800.58 | 2.23 |
| 2023-10 | 171,832.62 | 279,402 | 1,654 | 939,852.38 | 5.47 |
| 2024-04 | 196,892.88 | 294,204 | 1,424 | 795,905.02 | 4.04 |
| 2024-10 | 219,378.73 | 376,815 | 1,830 | 961,348.25 | 4.38 |
| 2025-02 | 97,550.77 | 178,946 | 1,116 | 619,062.19 | 6.35 |
| 2025-11 | 16,510.95 | 26,975 | 256 | 137,433.25 | 8.32 |
| 2025-12 | 807.04 | 1,238 | 61 | 31,085.00 | 38.52 |

By year: 2023 ROAS 4.85 · 2024 ROAS 4.40 · 2025 ROAS 5.31.

**Means:** in 2023 and 2024 spend peaks late in the year (Aug/Sep–Dec). In 2025 spend winds down
from September as campaigns end. 2024 spent the most but was the least efficient year. **Read the first and last months carefully:** revenue is dated by `converted_at`, and leads
take 10.5 days on average (up to 21) to convert. So January 2023 looks weak because revenue hadn't
arrived yet, and December 2025's 38.5 ROAS is revenue from November's spend arriving after the ads
stopped. That's a timing artefact, not a great month. Calendar-month ROAS is a cash-flow view.
Campaign-level ROAS (Q04, Q06) is the fairer measure of performance.

### Q10 — Rank campaigns by CTR (window function) ⭐
**Does:** computes each campaign's CTR from totals, then adds two ranks: one across all campaigns
and one that restarts in each channel.
**Concepts:** `RANK() OVER (ORDER BY …)`, `RANK() OVER (PARTITION BY channel ORDER BY …)`.

| overall_rank | rank_in_channel | campaign_id | campaign_name | channel | impressions | clicks | CTR % |
|---:|---:|---|---|---|---:|---:|---:|
| 1 | 1 | CMP0016 | Paid Search 2024 016 | paid_search | 858,967 | 30,641 | 3.57 |
| 2 | 2 | CMP0096 | Paid Search 2025 096 | paid_search | 1,320,200 | 47,049 | 3.56 |
| 3 | 3 | CMP0056 | Paid Search 2025 056 | paid_search | 2,918,609 | 103,965 | 3.56 |
| 4 | 4 | CMP0036 | Paid Search 2024 036 | paid_search | 2,300,663 | 81,904 | 3.56 |
| 5 | 5 | CMP0071 | Paid Search 2023 071 | paid_search | 3,455,663 | 122,752 | 3.55 |
| 6 | 6 | CMP0086 | Paid Search 2023 086 | paid_search | 649,503 | 23,060 | 3.55 |
| 7 | 7 | CMP0031 | Paid Search 2024 031 | paid_search | 1,565,701 | 55,470 | 3.54 |
| 8 | 8 | CMP0061 | Paid Search 2023 061 | paid_search | 2,716,997 | 96,240 | 3.54 |
| 9 | 9 | CMP0026 | Paid Search 2025 026 | paid_search | 2,285,552 | 80,957 | 3.54 |
| 10 | 10 | CMP0011 | Paid Search 2024 011 | paid_search | 2,275,604 | 80,009 | 3.52 |

(Ranks use the unrounded CTR, so 3.56 / 3.56 / 3.56 are not real ties.)
**Means:** the top 10 is all paid search. That makes sense: people searching already want the
product, so they click. Comparing CTR across channels mostly measures the *channel*, which is why
the `PARTITION BY channel` rank is the useful one for judging individual campaigns.

### Q11 — Best and worst CTR campaign in each channel
**Does:** ranks campaigns inside each channel twice (best-first and worst-first) in a CTE, then
keeps rank 1 of each.
**Concepts:** window function in a CTE, then filtering the rank in the outer `WHERE`.

| channel | position | campaign_id | campaign_name | CTR % |
|---|---|---|---|---:|
| affiliate | best | CMP0100 | Affiliate 2023 100 | 2.61 |
| affiliate | worst | CMP0080 | Affiliate 2024 080 | 2.42 |
| display | best | CMP0063 | Display 2025 063 | 0.62 |
| display | worst | CMP0078 | Display 2023 078 | 0.58 |
| paid_search | best | CMP0016 | Paid Search 2024 016 | 3.57 |
| paid_search | worst | CMP0111 | Paid Search 2023 111 | 3.38 |
| paid_social | best | CMP0067 | Paid Social 2024 067 | 1.83 |
| paid_social | worst | CMP0002 | Paid Social 2023 002 | 1.71 |
| video | best | CMP0074 | Video 2023 074 | 0.95 |
| video | worst | CMP0069 | Video 2024 069 | 0.87 |

**Means:** inside each channel, CTR barely moves (paid search: 3.38–3.57). In this dataset CTR is
almost entirely decided by the channel, so creative/CTR tweaks won't fix a weak channel. Budget
allocation will.

### Q12 — Do the biggest spenders also earn the most?
**Does:** ranks every campaign by spend and by revenue with `DENSE_RANK()`, then shows the top 10
spenders next to their revenue rank.
**Concepts:** two window functions side by side, `DENSE_RANK()`, CTEs.

| spend_rank | revenue_rank | campaign_id | campaign_name | channel | spend | revenue | ROAS |
|---:|---:|---|---|---|---:|---:|---:|
| 1 | 27 | CMP0093 | Display 2024 093 | display | 72,153.85 | 250,142.87 | 3.47 |
| 2 | 3 | CMP0021 | Paid Search 2024 021 | paid_search | 71,337.50 | 380,892.89 | 5.34 |
| 3 | 23 | CMP0102 | Paid Social 2025 102 | paid_social | 67,070.47 | 270,077.02 | 4.03 |
| 4 | 11 | CMP0107 | Paid Social 2024 107 | paid_social | 66,794.72 | 316,085.27 | 4.73 |
| 5 | 55 | CMP0041 | Paid Search 2024 041 | paid_search | 66,691.10 | 176,741.68 | 2.65 |
| 6 | 53 | CMP0024 | Video 2024 024 | video | 66,524.17 | 179,567.16 | 2.70 |
| 7 | 19 | CMP0073 | Display 2023 073 | display | 64,824.15 | 289,502.55 | 4.47 |
| 8 | 12 | CMP0117 | Paid Social 2023 117 | paid_social | 64,353.11 | 315,925.45 | 4.91 |
| 9 | 25 | CMP0008 | Display 2023 008 | display | 62,684.63 | 251,896.65 | 4.02 |
| 10 | 1 | CMP0060 | Affiliate 2024 060 | affiliate | 62,309.32 | 417,738.92 | 6.70 |

**Means:** only **2 of the 10 biggest spenders** (CMP0021 and CMP0060) are in the top 10 for revenue.
The #1 spender ranks 27th in revenue. CMP0041 is the surprise: a paid search campaign (the
second-best channel) that ranks 5th in spend but 55th in revenue, with ROAS 2.65, the worst in its
channel. **Spending more doesn't guarantee more revenue.**

### Q13 — Poor performers: 10 lowest-ROAS campaigns
**Does:** computes each campaign's ROAS, builds a one-row "benchmark" CTE with the portfolio ROAS,
attaches it to every row with `CROSS JOIN`, ranks campaigns worst-first and keeps the 10 worst that
are below the benchmark.
**Concepts:** CTEs, `CROSS JOIN`, `RANK()`, `WHERE` on calculated columns.
**Why not "ROAS < 1"?** No campaign loses money here (the minimum ROAS is 1.82; a test checks this).
"Poor" therefore means "below the portfolio average of 4.77".

| worst_rank | campaign_id | campaign_name | channel | objective | spend | revenue | CPA | ROAS | revenue gap vs avg |
|---:|---|---|---|---|---:|---:|---:|---:|---:|
| 1 | CMP0084 | Video 2023 084 | video | awareness | 6,105.33 | 11,132.24 | 436.09 | 1.82 | 17,972.49 |
| 2 | CMP0034 | Video 2024 034 | video | awareness | 34,180.02 | 67,619.25 | 244.14 | 1.98 | 95,320.38 |
| 3 | CMP0039 | Video 2025 039 | video | acquisition | 24,066.86 | 51,293.31 | 211.11 | 2.13 | 63,435.86 |
| 4 | CMP0119 | Video 2025 119 | video | acquisition | 16,825.46 | 36,568.85 | 236.98 | 2.17 | 43,639.83 |
| 5 | CMP0018 | Display 2023 018 | display | acquisition | 7,405.03 | 16,103.51 | 154.27 | 2.17 | 19,197.02 |
| 6 | CMP0074 | Video 2023 074 | video | awareness | 26,109.81 | 56,859.44 | 219.41 | 2.18 | 67,608.68 |
| 7 | CMP0009 | Video 2024 009 | video | lead_gen | 54,786.64 | 120,655.46 | 210.72 | 2.20 | 140,518.02 |
| 8 | CMP0079 | Video 2024 079 | video | lead_gen | 62,230.93 | 137,737.16 | 213.12 | 2.21 | 158,924.01 |
| 9 | CMP0099 | Video 2023 099 | video | awareness | 9,348.40 | 21,200.08 | 267.10 | 2.27 | 23,364.69 |
| 10 | CMP0064 | Video 2024 064 | video | lead_gen | 55,178.97 | 128,190.29 | 223.40 | 2.32 | 134,853.47 |

"Revenue gap vs avg" = spend × 4.77 − revenue: how much more the campaign would have earned at the
portfolio's average efficiency.
**Means:** 9 of the 10 worst campaigns are video. The biggest money question is CMP0079 and
CMP0009: about $117k of spend between them, with roughly $300k of revenue left on the table vs average.

### Q14 — Below-average campaigns per channel
**Does:** flags each campaign as below/above the portfolio ROAS, then counts flagged campaigns and
their spend per channel. `HAVING` keeps only channels with at least one flagged campaign.
**Concepts:** CTEs, `CROSS JOIN`, `CASE` inside `SUM`, `GROUP BY`, `HAVING`.

| channel | campaigns | below-avg campaigns | spend in below-avg | % of channel spend | channel ROAS |
|---|---:|---:|---:|---:|---:|
| display | 24 | 22 | 929,349.76 | 97.3 | 3.91 |
| video | 24 | 24 | 828,094.54 | 100.0 | 2.52 |
| paid_social | 24 | 12 | 576,656.39 | 58.6 | 4.76 |
| paid_search | 24 | 5 | 263,246.66 | 29.3 | 6.06 |
| affiliate | 24 | 5 | 257,571.91 | 30.0 | 6.54 |

**Means:** every video campaign and 22 of 24 display campaigns are below average, together about
$1.76M (39%) of the total budget. Paid social is split down the middle, so it's worth optimizing
campaign by campaign rather than cutting.

---

## 5. Answers to the 7 business questions

**1. Which channel generates the most revenue?**
**Affiliate**: $5,610,700.73 (26.0% of all revenue) from 19.0% of spend (Q04). Paid search is a
close second with $5,439,329.35.

**2. Which channel has the highest ROAS?**
**Affiliate, 6.54** ($6.54 back per $1), then paid search 6.06. The lowest is video at 2.52 (Q04).

**3. Which campaigns have the highest CTR?**
Paid search campaigns hold all top 10 spots: CMP0016 (3.57%), CMP0096, CMP0056, CMP0036 (3.56%)…
(Q10). Within each channel the best are CMP0100 (affiliate 2.61%), CMP0067 (paid social 1.83%),
CMP0074 (video 0.95%) and CMP0063 (display 0.62%) (Q11).

**4. Which campaigns spend the most?**
CMP0093 Display 2024 093 ($72,153.85), CMP0021 Paid Search 2024 021 ($71,337.50), CMP0102 Paid
Social 2025 102 ($67,070.47), CMP0107 ($66,794.72) and CMP0041 ($66,691.10) (Q05).

**5. Are the highest-spending campaigns also generating the highest revenue?**
**Mostly no.** Only 2 of the top 10 spenders (CMP0021 and CMP0060) are top 10 in revenue. The #1
spender, CMP0093, ranks 27th in revenue, and CMP0041 ranks 5th in spend but 55th in revenue (Q12).
Meanwhile CMP0050 and CMP0110 earn about $11 per $1 on below-average budgets (Q06).

**6. Which campaigns have poor performance?**
No campaign loses money (lowest ROAS 1.82) and none has zero conversions (Q07 returns 0 rows).
Against the 4.77 portfolio average, the weakest are CMP0084, CMP0034, CMP0039, CMP0119, CMP0018,
CMP0074, CMP0009, CMP0079, CMP0099 and CMP0064, with ROAS 1.82–2.32. Nine of these ten are video (Q13).
In paid search, CMP0041 (ROAS 2.65) is badly out of line with its channel's 6.06 (Q12).

**7. Which channels should a marketing team investigate for budget optimization?**
- **Video first.** 18.3% of spend for 9.7% of revenue, ROAS 2.52, CPA $210.55, and all 24 campaigns
  below average. This holds for every objective (acquisition 2.63, awareness 2.52, lead_gen 2.41),
  so it's not explained by video simply being used for awareness. The clicks are the problem:
  CPC $1.68 vs $0.36 on affiliate.
- **Display second.** ROAS 3.91, and 97% of its spend sits in below-average campaigns.
- **Where the budget could go:** affiliate (6.54) and paid search (6.06), especially proven
  high-ROAS campaigns like CMP0050 and CMP0110.
- **Paid social:** keep, but prune the 12 below-average campaigns.

Caveats a marketer should raise before moving money:
1. Revenue is credited to the campaign that created the lead (single-touch). Video and display
   often influence purchases credited elsewhere, so test cuts gradually and watch total revenue.
2. Moving budget has diminishing returns: affiliate won't necessarily keep a 6.54 ROAS at double the spend.
3. Calendar-month ROAS is distorted at the edges of the data by the 10–21-day conversion lag (Q09).

---

## 6. How the results were tested

`python -m pytest -v` runs 21 tests in [`tests/test_sql.py`](../tests/test_sql.py). The "answer
key" is computed independently in pandas from the raw tables, never from the SQL output.

| What was checked | How |
|---|---|
| All 14 queries execute | each one runs without error |
| Required SQL features are used | `INNER JOIN`, `LEFT JOIN`, `WITH`, `RANK()`, `DENSE_RANK()`, `PARTITION BY`, `NULLIF`, `HAVING` all present |
| Totals are right | Q01–Q03 match pandas sums of the raw tables |
| Joins don't duplicate | Q04 channel spend/revenue add up to the raw table totals; Q09 months add up too; Q05 has exactly 120 unique campaigns |
| Why aggregating first matters | a naive day × conversion join inflates CMP0001's spend more than 10× |
| CTR, CPC, CVR, CPA, ROAS are right | Q04 matches a pandas recomputation for every channel and metric |
| Ratios use totals | impression-weighted channel CTRs reproduce the pooled CTR exactly |
| Window functions rank correctly | Q10 `RANK()` (overall and per channel), Q11 best/worst, Q12 `DENSE_RANK()` match pandas `rank()` |
| Poor-performer logic | no campaign has ROAS < 1; Q13 returns the 10 lowest ROAS; Q14 counts match |
| No division-by-zero errors | every query runs on a tiny fake DB with a 0-impression, 0-click, 0-conversion campaign; ratios come back NULL |
| Q07 really works | on the tiny DB it finds the one high-spend, zero-conversion campaign |
| Original database untouched | file hash is identical before and after; the connection refuses writes |
