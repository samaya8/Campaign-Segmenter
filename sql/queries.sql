-- =============================================================================
-- Campaign Segmenter · Day 2 · SQL on real campaign data
-- Database: data/raw/marketing_campaigns.sqlite (SQLite)
--
-- Tables and how they connect (logical keys; none are declared in the file):
--   campaigns   (campaign_id)  1 ──< daily_spend (campaign_id)   one row per campaign per day
--   campaigns   (campaign_id)  1 ──< leads       (campaign_id)   one row per lead
--   leads       (lead_id)      1 ──o conversions (lead_id)       at most one conversion per lead
--
-- Revenue lives in conversions, which only knows lead_id. To get revenue per
-- campaign we go conversions -> leads -> campaign_id.
--
-- Metric rules used everywhere:
--   CTR  = clicks / impressions * 100      CPC = spend / clicks
--   CVR  = conversions / clicks * 100      CPA = spend / conversions
--   ROAS = revenue / spend
--   * Ratios are computed from SUMMED totals, never by averaging daily ratios.
--   * NULLIF(x, 0) turns a zero denominator into NULL, so we get NULL, not an error.
--   * "* 1.0" forces decimal division (in SQLite, integer / integer drops decimals).
--   * Spend and revenue are aggregated to campaign level SEPARATELY before
--     joining, so daily rows never multiply conversion rows (no double counting).
--   * "conversions" counts every converted lead (trials + purchases);
--     only purchases carry revenue.
--
-- Each query starts with a "-- @query" line so scripts/run_queries.py can run
-- them one by one.
-- =============================================================================


-- @query Q01: Total advertising spend across all campaigns
-- Concepts: SELECT, aggregate functions (SUM, COUNT, MIN, MAX), ROUND
SELECT
    COUNT(DISTINCT campaign_id)  AS campaigns,
    COUNT(*)                     AS active_campaign_days,
    MIN(spend_date)              AS first_day,
    MAX(spend_date)              AS last_day,
    SUM(impressions)             AS total_impressions,
    SUM(clicks)                  AS total_clicks,
    ROUND(SUM(spend), 2)         AS total_spend
FROM daily_spend;


-- @query Q02: Total clicks by channel (and each channel's share)
-- Concepts: INNER JOIN, GROUP BY, ORDER BY, SUM
SELECT
    c.channel,
    SUM(d.clicks)                                                   AS clicks,
    ROUND(SUM(d.clicks) * 100.0 / (SELECT SUM(clicks) FROM daily_spend), 2) AS pct_of_all_clicks
FROM daily_spend AS d
INNER JOIN campaigns AS c
        ON c.campaign_id = d.campaign_id
GROUP BY c.channel
ORDER BY clicks DESC;


-- @query Q03: Total conversions and revenue
-- Concepts: COUNT, SUM, CASE WHEN inside an aggregate (conditional counting)
SELECT
    COUNT(*)                                                  AS total_conversions,
    SUM(CASE WHEN conversion_type = 'purchase' THEN 1 ELSE 0 END) AS purchases,
    SUM(CASE WHEN conversion_type = 'trial'    THEN 1 ELSE 0 END) AS trials,
    ROUND(SUM(revenue), 2)                                    AS total_revenue,
    ROUND(SUM(revenue) * 1.0
          / NULLIF(SUM(CASE WHEN conversion_type = 'purchase' THEN 1 ELSE 0 END), 0), 2)
                                                              AS avg_order_value
FROM conversions;


-- @query Q04: Full funnel metrics and ROAS by channel
-- Concepts: CTE (WITH), INNER JOIN, LEFT JOIN, GROUP BY, NULLIF, aggregate-then-join
WITH spend_by_campaign AS (            -- step 1: delivery totals, 1 row per campaign
    SELECT campaign_id,
           SUM(impressions) AS impressions,
           SUM(clicks)      AS clicks,
           SUM(spend)       AS spend
    FROM daily_spend
    GROUP BY campaign_id
),
results_by_campaign AS (               -- step 2: conversion totals, 1 row per campaign
    SELECT l.campaign_id,
           COUNT(v.conversion_id) AS conversions,
           SUM(v.revenue)         AS revenue
    FROM leads AS l
    INNER JOIN conversions AS v ON v.lead_id = l.lead_id
    GROUP BY l.campaign_id
)
SELECT                                 -- step 3: join 1-to-1 at campaign level, roll up to channel
    c.channel,
    ROUND(SUM(s.spend), 2)                                               AS spend,
    ROUND(SUM(COALESCE(r.revenue, 0)), 2)                                AS revenue,
    ROUND(SUM(s.clicks) * 100.0 / NULLIF(SUM(s.impressions), 0), 2)      AS ctr_pct,
    ROUND(SUM(s.spend) * 1.0    / NULLIF(SUM(s.clicks), 0), 2)           AS cpc,
    ROUND(SUM(COALESCE(r.conversions, 0)) * 100.0 / NULLIF(SUM(s.clicks), 0), 2) AS cvr_pct,
    ROUND(SUM(s.spend) * 1.0    / NULLIF(SUM(COALESCE(r.conversions, 0)), 0), 2) AS cpa,
    ROUND(SUM(COALESCE(r.revenue, 0)) * 1.0 / NULLIF(SUM(s.spend), 0), 2) AS roas
FROM campaigns AS c
INNER JOIN spend_by_campaign   AS s ON s.campaign_id = c.campaign_id
LEFT  JOIN results_by_campaign AS r ON r.campaign_id = c.campaign_id
GROUP BY c.channel
ORDER BY roas DESC;


-- @query Q05: Total spend and clicks for each campaign
-- Concepts: INNER JOIN, GROUP BY several columns, ORDER BY
SELECT
    c.campaign_id,
    c.campaign_name,
    c.channel,
    ROUND(SUM(d.spend), 2) AS spend,
    SUM(d.clicks)          AS clicks,
    ROUND(c.budget, 2)     AS budget,
    ROUND(SUM(d.spend) * 100.0 / NULLIF(c.budget, 0), 1) AS pct_of_budget_used
FROM campaigns AS c
INNER JOIN daily_spend AS d
        ON d.campaign_id = c.campaign_id
GROUP BY c.campaign_id, c.campaign_name, c.channel, c.budget
ORDER BY spend DESC;


-- @query Q06: Top 10 campaigns by revenue
-- Concepts: CTEs, LEFT JOIN, ORDER BY ... DESC, LIMIT
WITH spend_by_campaign AS (
    SELECT campaign_id, SUM(spend) AS spend
    FROM daily_spend
    GROUP BY campaign_id
),
revenue_by_campaign AS (
    SELECT l.campaign_id,
           SUM(CASE WHEN v.conversion_type = 'purchase' THEN 1 ELSE 0 END) AS purchases,
           SUM(v.revenue) AS revenue
    FROM leads AS l
    INNER JOIN conversions AS v ON v.lead_id = l.lead_id
    GROUP BY l.campaign_id
)
SELECT
    c.campaign_id,
    c.campaign_name,
    c.channel,
    c.objective,
    ROUND(COALESCE(r.revenue, 0), 2)                              AS revenue,
    ROUND(s.spend, 2)                                             AS spend,
    COALESCE(r.purchases, 0)                                      AS purchases,
    ROUND(COALESCE(r.revenue, 0) * 1.0 / NULLIF(s.spend, 0), 2)   AS roas
FROM campaigns AS c
LEFT JOIN spend_by_campaign   AS s ON s.campaign_id = c.campaign_id
LEFT JOIN revenue_by_campaign AS r ON r.campaign_id = c.campaign_id
ORDER BY revenue DESC
LIMIT 10;


-- @query Q07: High-spend campaigns with NO conversions
-- Concepts: LEFT JOIN + IS NULL (find rows with no match), HAVING, subquery
-- "High spend" = more than the average spend per campaign.
WITH spend_by_campaign AS (
    SELECT campaign_id, SUM(spend) AS spend
    FROM daily_spend
    GROUP BY campaign_id
),
conversions_by_campaign AS (
    SELECT l.campaign_id, COUNT(v.conversion_id) AS conversions
    FROM leads AS l
    INNER JOIN conversions AS v ON v.lead_id = l.lead_id
    GROUP BY l.campaign_id
)
SELECT
    c.campaign_id,
    c.campaign_name,
    c.channel,
    ROUND(s.spend, 2)              AS spend,
    COALESCE(k.conversions, 0)     AS conversions
FROM campaigns AS c
INNER JOIN spend_by_campaign       AS s ON s.campaign_id = c.campaign_id
LEFT  JOIN conversions_by_campaign AS k ON k.campaign_id = c.campaign_id
WHERE k.campaign_id IS NULL                                     -- no conversion row at all
  AND s.spend > (SELECT AVG(spend) FROM spend_by_campaign)      -- above-average spend
ORDER BY s.spend DESC;


-- @query Q08: Average campaign performance by channel
-- Concepts: CTE, AVG over campaign-level totals, COUNT, HAVING
-- First build ONE row per campaign, then average those rows per channel.
WITH campaign_totals AS (
    SELECT c.campaign_id,
           c.channel,
           s.impressions, s.clicks, s.spend,
           COALESCE(r.conversions, 0) AS conversions,
           COALESCE(r.revenue, 0)     AS revenue
    FROM campaigns AS c
    INNER JOIN (SELECT campaign_id,
                       SUM(impressions) AS impressions,
                       SUM(clicks)      AS clicks,
                       SUM(spend)       AS spend
                FROM daily_spend GROUP BY campaign_id) AS s
            ON s.campaign_id = c.campaign_id
    LEFT JOIN (SELECT l.campaign_id,
                      COUNT(v.conversion_id) AS conversions,
                      SUM(v.revenue)         AS revenue
               FROM leads AS l
               INNER JOIN conversions AS v ON v.lead_id = l.lead_id
               GROUP BY l.campaign_id) AS r
            ON r.campaign_id = c.campaign_id
)
SELECT
    channel,
    COUNT(*)                     AS campaigns,
    ROUND(AVG(spend), 2)         AS avg_spend_per_campaign,
    ROUND(AVG(clicks), 0)        AS avg_clicks_per_campaign,
    ROUND(AVG(conversions), 0)   AS avg_conversions_per_campaign,
    ROUND(AVG(revenue), 2)       AS avg_revenue_per_campaign,
    ROUND(SUM(revenue) * 1.0 / NULLIF(SUM(spend), 0), 2) AS channel_roas,
    ROUND(MIN(revenue * 1.0 / NULLIF(spend, 0)), 2)      AS worst_campaign_roas,
    ROUND(MAX(revenue * 1.0 / NULLIF(spend, 0)), 2)      AS best_campaign_roas
FROM campaign_totals
GROUP BY channel
HAVING COUNT(*) >= 5                       -- only channels with enough campaigns to compare
ORDER BY avg_revenue_per_campaign DESC;


-- @query Q09: Monthly advertising spend and revenue
-- Concepts: CTE, strftime() to group dates by month, LEFT JOIN on month
-- Spend is dated by spend_date, revenue by converted_at (cash-flow view).
WITH monthly_spend AS (
    SELECT strftime('%Y-%m', spend_date) AS month,
           SUM(spend)  AS spend,
           SUM(clicks) AS clicks
    FROM daily_spend
    GROUP BY month
),
monthly_revenue AS (
    SELECT strftime('%Y-%m', converted_at) AS month,
           COUNT(*)     AS conversions,
           SUM(revenue) AS revenue
    FROM conversions
    GROUP BY month
)
SELECT
    s.month,
    ROUND(s.spend, 2)                                         AS spend,
    s.clicks,
    COALESCE(r.conversions, 0)                                AS conversions,
    ROUND(COALESCE(r.revenue, 0), 2)                          AS revenue,
    ROUND(COALESCE(r.revenue, 0) * 1.0 / NULLIF(s.spend, 0), 2) AS roas
FROM monthly_spend AS s
LEFT JOIN monthly_revenue AS r ON r.month = s.month
ORDER BY s.month;


-- @query Q10: Rank campaigns by CTR (window function)
-- Concepts: window functions, RANK() OVER (ORDER BY ...), PARTITION BY
WITH ctr_by_campaign AS (
    SELECT c.campaign_id,
           c.campaign_name,
           c.channel,
           SUM(d.impressions) AS impressions,
           SUM(d.clicks)      AS clicks,
           SUM(d.clicks) * 100.0 / NULLIF(SUM(d.impressions), 0) AS ctr_pct
    FROM campaigns AS c
    INNER JOIN daily_spend AS d ON d.campaign_id = c.campaign_id
    GROUP BY c.campaign_id, c.campaign_name, c.channel
)
SELECT
    RANK() OVER (ORDER BY ctr_pct DESC)                       AS overall_rank,
    RANK() OVER (PARTITION BY channel ORDER BY ctr_pct DESC)  AS rank_in_channel,
    campaign_id,
    campaign_name,
    channel,
    impressions,
    clicks,
    ROUND(ctr_pct, 2) AS ctr_pct
FROM ctr_by_campaign
ORDER BY overall_rank
LIMIT 10;


-- @query Q11: Best and worst CTR campaign in each channel
-- Concepts: window function in a CTE, then filtering on the rank with WHERE
-- (You cannot use a window function directly in WHERE, so rank first, filter after.)
WITH ctr_by_campaign AS (
    SELECT c.campaign_id,
           c.campaign_name,
           c.channel,
           SUM(d.clicks) * 100.0 / NULLIF(SUM(d.impressions), 0) AS ctr_pct
    FROM campaigns AS c
    INNER JOIN daily_spend AS d ON d.campaign_id = c.campaign_id
    GROUP BY c.campaign_id, c.campaign_name, c.channel
),
ranked AS (
    SELECT *,
           RANK() OVER (PARTITION BY channel ORDER BY ctr_pct DESC) AS best_rank,
           RANK() OVER (PARTITION BY channel ORDER BY ctr_pct ASC)  AS worst_rank
    FROM ctr_by_campaign
)
SELECT
    channel,
    CASE WHEN best_rank = 1 THEN 'best' ELSE 'worst' END AS position,
    campaign_id,
    campaign_name,
    ROUND(ctr_pct, 2) AS ctr_pct
FROM ranked
WHERE best_rank = 1 OR worst_rank = 1
ORDER BY channel, position;


-- @query Q12: Do the biggest spenders also earn the most? (spend rank vs revenue rank)
-- Concepts: two window functions side by side, DENSE_RANK(), CTEs, LEFT JOIN
WITH spend_by_campaign AS (
    SELECT campaign_id, SUM(spend) AS spend
    FROM daily_spend
    GROUP BY campaign_id
),
revenue_by_campaign AS (
    SELECT l.campaign_id, SUM(v.revenue) AS revenue
    FROM leads AS l
    INNER JOIN conversions AS v ON v.lead_id = l.lead_id
    GROUP BY l.campaign_id
),
ranked AS (
    SELECT c.campaign_id,
           c.campaign_name,
           c.channel,
           s.spend,
           COALESCE(r.revenue, 0) AS revenue,
           DENSE_RANK() OVER (ORDER BY s.spend DESC)                  AS spend_rank,
           DENSE_RANK() OVER (ORDER BY COALESCE(r.revenue, 0) DESC)   AS revenue_rank
    FROM campaigns AS c
    INNER JOIN spend_by_campaign  AS s ON s.campaign_id = c.campaign_id
    LEFT JOIN revenue_by_campaign AS r ON r.campaign_id = c.campaign_id
)
SELECT
    spend_rank,
    revenue_rank,
    campaign_id,
    campaign_name,
    channel,
    ROUND(spend, 2)   AS spend,
    ROUND(revenue, 2) AS revenue,
    ROUND(revenue * 1.0 / NULLIF(spend, 0), 2) AS roas
FROM ranked
WHERE spend_rank <= 10
ORDER BY spend_rank;


-- @query Q13: Poor performers: 10 lowest-ROAS campaigns vs the portfolio average
-- Concepts: CTEs, CROSS JOIN to a one-row CTE, RANK(), WHERE on calculated columns
-- No campaign in this dataset has ROAS below 1.0 (none loses money; checked in
-- tests), so "poor" means ROAS below the portfolio ROAS = total revenue / total spend.
WITH spend_by_campaign AS (
    SELECT campaign_id,
           SUM(clicks) AS clicks,
           SUM(spend)  AS spend
    FROM daily_spend
    GROUP BY campaign_id
),
revenue_by_campaign AS (
    SELECT l.campaign_id,
           COUNT(v.conversion_id) AS conversions,
           SUM(v.revenue)         AS revenue
    FROM leads AS l
    INNER JOIN conversions AS v ON v.lead_id = l.lead_id
    GROUP BY l.campaign_id
),
campaign_roas AS (
    SELECT c.campaign_id,
           c.campaign_name,
           c.channel,
           c.objective,
           s.spend,
           COALESCE(r.revenue, 0) AS revenue,
           s.spend * 1.0 / NULLIF(COALESCE(r.conversions, 0), 0) AS cpa,
           COALESCE(r.revenue, 0) * 1.0 / NULLIF(s.spend, 0)     AS roas
    FROM campaigns AS c
    INNER JOIN spend_by_campaign  AS s ON s.campaign_id = c.campaign_id
    LEFT JOIN revenue_by_campaign AS r ON r.campaign_id = c.campaign_id
),
portfolio AS (                         -- a one-row table holding the benchmark
    SELECT SUM(revenue) * 1.0 / NULLIF(SUM(spend), 0) AS portfolio_roas
    FROM campaign_roas
),
ranked AS (
    SELECT cr.*,
           p.portfolio_roas,
           RANK() OVER (ORDER BY cr.roas ASC) AS worst_rank
    FROM campaign_roas AS cr
    CROSS JOIN portfolio AS p          -- attaches the benchmark to every campaign row
)
SELECT
    worst_rank,
    campaign_id,
    campaign_name,
    channel,
    objective,
    ROUND(spend, 2)          AS spend,
    ROUND(revenue, 2)        AS revenue,
    ROUND(cpa, 2)            AS cpa,
    ROUND(roas, 2)           AS roas,
    ROUND(portfolio_roas, 2) AS portfolio_roas,
    ROUND(spend * portfolio_roas - revenue, 2) AS revenue_gap_vs_avg  -- revenue missed vs average efficiency
FROM ranked
WHERE roas < portfolio_roas
  AND worst_rank <= 10
ORDER BY worst_rank;


-- @query Q14: Below-average campaigns per channel (where to look for budget savings)
-- Concepts: CTE, CROSS JOIN, GROUP BY, conditional aggregates (CASE inside SUM), HAVING
WITH campaign_roas AS (
    SELECT c.campaign_id,
           c.channel,
           s.spend,
           COALESCE(r.revenue, 0) AS revenue
    FROM campaigns AS c
    INNER JOIN (SELECT campaign_id, SUM(spend) AS spend
                FROM daily_spend GROUP BY campaign_id) AS s
            ON s.campaign_id = c.campaign_id
    LEFT JOIN (SELECT l.campaign_id, SUM(v.revenue) AS revenue
               FROM leads AS l
               INNER JOIN conversions AS v ON v.lead_id = l.lead_id
               GROUP BY l.campaign_id) AS r
            ON r.campaign_id = c.campaign_id
),
portfolio AS (
    SELECT SUM(revenue) * 1.0 / NULLIF(SUM(spend), 0) AS portfolio_roas
    FROM campaign_roas
),
flagged AS (
    SELECT cr.*,
           CASE WHEN cr.revenue * 1.0 / NULLIF(cr.spend, 0) < p.portfolio_roas
                THEN 1 ELSE 0 END AS below_avg
    FROM campaign_roas AS cr
    CROSS JOIN portfolio AS p
)
SELECT
    channel,
    COUNT(*)                                              AS campaigns,
    SUM(below_avg)                                        AS below_avg_campaigns,
    ROUND(SUM(CASE WHEN below_avg = 1 THEN spend ELSE 0 END), 2) AS spend_in_below_avg,
    ROUND(SUM(CASE WHEN below_avg = 1 THEN spend ELSE 0 END) * 100.0
          / NULLIF(SUM(spend), 0), 1)                     AS pct_of_channel_spend,
    ROUND(SUM(revenue) * 1.0 / NULLIF(SUM(spend), 0), 2)  AS channel_roas
FROM flagged
GROUP BY channel
HAVING SUM(below_avg) > 0
ORDER BY spend_in_below_avg DESC;
