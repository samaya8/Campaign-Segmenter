"""Day 2 tests: every query runs, and the numbers match an independent pandas calculation.

Run:  python -m pytest -v
"""

import hashlib
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from run_queries import DB_PATH, SQL_PATH, connect_readonly, load_queries, run_query  # noqa: E402

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="database not downloaded (see README)")

QUERIES = {qid: sql for qid, _, sql in load_queries()}


@pytest.fixture(scope="module")
def conn():
    c = connect_readonly()
    yield c
    c.close()


@pytest.fixture(scope="module")
def raw(conn):
    """The four raw tables loaded into pandas, used as the independent 'answer key'."""
    return {t: pd.read_sql_query(f"SELECT * FROM {t}", conn)
            for t in ["campaigns", "daily_spend", "leads", "conversions"]}


@pytest.fixture(scope="module")
def by_campaign(raw):
    """Campaign-level totals computed in pandas (spend and revenue aggregated separately)."""
    spend = raw["daily_spend"].groupby("campaign_id")[["impressions", "clicks", "spend"]].sum()
    conv = raw["conversions"].merge(raw["leads"][["lead_id", "campaign_id"]], on="lead_id")
    results = conv.groupby("campaign_id").agg(conversions=("conversion_id", "count"), revenue=("revenue", "sum"))
    df = raw["campaigns"].set_index("campaign_id").join(spend).join(results).fillna({"conversions": 0, "revenue": 0})
    return df.reset_index()


# ---------- structure ----------

def test_at_least_ten_queries_and_all_run(conn):
    assert len(QUERIES) >= 10
    for qid, sql in QUERIES.items():
        run_query(conn, sql)  # raises on any SQL error, including division problems


def test_required_sql_features_present():
    text = SQL_PATH.read_text(encoding="utf-8").upper()
    for feature in ["INNER JOIN", "LEFT JOIN", "WITH ", "RANK() OVER", "DENSE_RANK() OVER", "PARTITION BY", "NULLIF", "HAVING"]:
        assert feature in text, f"missing {feature}"


def test_database_is_not_modified(conn):
    before = hashlib.sha256(DB_PATH.read_bytes()).hexdigest()
    for sql in QUERIES.values():
        run_query(conn, sql)
    assert hashlib.sha256(DB_PATH.read_bytes()).hexdigest() == before
    with pytest.raises(sqlite3.OperationalError):  # the connection is read-only
        conn.execute("CREATE TABLE should_fail (x INTEGER)")


# ---------- totals ----------

def test_q01_totals(conn, raw):
    row = run_query(conn, QUERIES["Q01"]).iloc[0]
    d = raw["daily_spend"]
    assert row.total_spend == pytest.approx(d.spend.sum(), abs=0.01)
    assert row.total_clicks == d.clicks.sum()
    assert row.total_impressions == d.impressions.sum()
    assert row.campaigns == 120


def test_q02_clicks_by_channel_add_up(conn, raw):
    df = run_query(conn, QUERIES["Q02"])
    assert df.clicks.sum() == raw["daily_spend"].clicks.sum()
    assert df.pct_of_all_clicks.sum() == pytest.approx(100, abs=0.05)


def test_q03_conversions_and_revenue(conn, raw):
    row = run_query(conn, QUERIES["Q03"]).iloc[0]
    v = raw["conversions"]
    assert row.total_conversions == len(v)
    assert row.purchases + row.trials == len(v)
    assert row.total_revenue == pytest.approx(v.revenue.sum(), abs=0.01)


# ---------- no double counting ----------

def test_q04_join_does_not_duplicate_spend_or_revenue(conn, raw):
    df = run_query(conn, QUERIES["Q04"])
    assert df.spend.sum() == pytest.approx(raw["daily_spend"].spend.sum(), abs=0.1)
    assert df.revenue.sum() == pytest.approx(raw["conversions"].revenue.sum(), abs=0.1)


def test_naive_join_would_double_count(conn):
    """Why we aggregate first: joining daily rows to conversion rows multiplies spend."""
    naive = conn.execute("""
        SELECT SUM(d.spend) FROM daily_spend d
        JOIN leads l ON l.campaign_id = d.campaign_id
        JOIN conversions v ON v.lead_id = l.lead_id
        WHERE d.campaign_id = 'CMP0001'""").fetchone()[0]
    correct = conn.execute("SELECT SUM(spend) FROM daily_spend WHERE campaign_id = 'CMP0001'").fetchone()[0]
    assert naive > correct * 10  # each day's spend is repeated once per conversion


def test_campaign_level_queries_have_one_row_per_campaign(conn):
    df = run_query(conn, QUERIES["Q05"])
    assert len(df) == 120 and df.campaign_id.is_unique
    assert run_query(conn, QUERIES["Q08"]).campaigns.sum() == 120


# ---------- metric formulas ----------

def test_q04_channel_metrics_match_pandas(conn, by_campaign):
    sql = run_query(conn, QUERIES["Q04"]).set_index("channel")
    g = by_campaign.groupby("channel")[["impressions", "clicks", "spend", "conversions", "revenue"]].sum()
    expected = pd.DataFrame({
        "ctr_pct": g.clicks / g.impressions * 100,
        "cpc": g.spend / g.clicks,
        "cvr_pct": g.conversions / g.clicks * 100,
        "cpa": g.spend / g.conversions,
        "roas": g.revenue / g.spend,
    })
    for col in expected:
        for ch in expected.index:
            assert sql.loc[ch, col] == pytest.approx(expected.loc[ch, col], abs=0.006), (ch, col)


def test_ratios_use_totals_not_daily_averages(conn, raw):
    """Average of daily CTRs differs from total clicks / total impressions; we use the latter."""
    d = raw["daily_spend"]
    pooled = d.clicks.sum() / d.impressions.sum() * 100
    q = run_query(conn, QUERIES["Q04"])
    # weighted-by-impressions average of channel CTRs must equal the pooled CTR
    imps = d.merge(raw["campaigns"][["campaign_id", "channel"]]).groupby("channel").impressions.sum()
    weighted = (q.set_index("channel").ctr_pct * imps).sum() / imps.sum()
    assert weighted == pytest.approx(pooled, abs=0.01)


def test_q06_top_revenue(conn, by_campaign):
    df = run_query(conn, QUERIES["Q06"])
    expected = by_campaign.sort_values("revenue", ascending=False).head(10)
    assert list(df.campaign_id) == list(expected.campaign_id)
    assert df.revenue.is_monotonic_decreasing


def test_q09_monthly_totals_add_up(conn, raw):
    df = run_query(conn, QUERIES["Q09"])
    assert df.spend.sum() == pytest.approx(raw["daily_spend"].spend.sum(), abs=1)
    assert df.revenue.sum() == pytest.approx(raw["conversions"].revenue.sum(), abs=1)
    assert df.month.is_unique


# ---------- window functions ----------

def test_q10_ctr_rank(conn, by_campaign):
    df = run_query(conn, QUERIES["Q10"])
    exp = by_campaign.assign(ctr=by_campaign.clicks / by_campaign.impressions * 100)
    exp["overall_rank"] = exp.ctr.rank(method="min", ascending=False).astype(int)
    exp["rank_in_channel"] = exp.groupby("channel").ctr.rank(method="min", ascending=False).astype(int)
    exp = exp.sort_values("overall_rank").head(10)
    assert list(df.campaign_id) == list(exp.campaign_id)
    assert list(df.overall_rank) == list(exp.overall_rank)
    assert list(df.rank_in_channel) == list(exp.rank_in_channel)


def test_q11_one_best_and_one_worst_per_channel(conn, by_campaign):
    df = run_query(conn, QUERIES["Q11"])
    exp = by_campaign.assign(ctr=by_campaign.clicks / by_campaign.impressions)
    for ch, grp in exp.groupby("channel"):
        rows = df[df.channel == ch].set_index("position")
        assert rows.loc["best", "campaign_id"] == grp.loc[grp.ctr.idxmax(), "campaign_id"]
        assert rows.loc["worst", "campaign_id"] == grp.loc[grp.ctr.idxmin(), "campaign_id"]


def test_q12_dense_rank(conn, by_campaign):
    df = run_query(conn, QUERIES["Q12"])
    exp = by_campaign.assign(revenue_rank=by_campaign.revenue.rank(method="dense", ascending=False).astype(int))
    exp = exp.sort_values("spend", ascending=False).head(10)
    assert list(df.campaign_id) == list(exp.campaign_id)
    assert list(df.spend_rank) == list(range(1, 11))
    assert list(df.revenue_rank) == list(exp.revenue_rank)


# ---------- poor performers ----------

def test_no_campaign_loses_money(by_campaign):
    """Backs the comment in Q13: the minimum campaign ROAS is above 1.0."""
    assert (by_campaign.revenue / by_campaign.spend).min() > 1.0


def test_q13_q14_use_portfolio_benchmark(conn, by_campaign):
    portfolio = by_campaign.revenue.sum() / by_campaign.spend.sum()
    q13 = run_query(conn, QUERIES["Q13"])
    assert (q13.roas < portfolio).all() and q13.roas.is_monotonic_increasing
    exp = by_campaign.assign(roas=by_campaign.revenue / by_campaign.spend).nsmallest(10, "roas")
    assert list(q13.campaign_id) == list(exp.campaign_id)
    q14 = run_query(conn, QUERIES["Q14"]).set_index("channel")
    below = by_campaign[by_campaign.revenue / by_campaign.spend < portfolio]
    assert q14.below_avg_campaigns.sum() == len(below)


# ---------- edge cases on a tiny fake database ----------

@pytest.fixture
def tiny():
    """3 campaigns: C1 normal, C2 high spend + zero conversions, C3 zero clicks + zero impressions."""
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE campaigns (campaign_id, campaign_name, channel, objective, start_date, end_date, budget);
        CREATE TABLE daily_spend (spend_id, campaign_id, spend_date, impressions, clicks, spend);
        CREATE TABLE leads (lead_id, campaign_id, created_at, lead_source, lead_score);
        CREATE TABLE conversions (conversion_id, lead_id, converted_at, conversion_type, revenue);
        INSERT INTO campaigns VALUES
          ('C1','One','search','acq','2024-01-01','2024-01-31',1000),
          ('C2','Two','search','acq','2024-01-01','2024-01-31',1000),
          ('C3','Three','video','acq','2024-01-01','2024-01-31',0);
        INSERT INTO daily_spend VALUES
          ('S1','C1','2024-01-01',1000,50,100), ('S2','C1','2024-01-02',1000,50,100),
          ('S3','C2','2024-01-01',1000,40,900),
          ('S4','C3','2024-01-01',0,0,10);
        INSERT INTO leads VALUES ('L1','C1','2024-01-01','search',50), ('L2','C1','2024-01-01','search',60),
                                 ('L3','C2','2024-01-01','search',10);
        INSERT INTO conversions VALUES ('V1','L1','2024-01-03','purchase',500), ('V2','L2','2024-01-03','trial',0);
    """)
    yield c
    c.close()


def test_q07_finds_high_spend_campaign_without_conversions(tiny):
    df = run_query(tiny, QUERIES["Q07"])
    assert list(df.campaign_id) == ["C2"]  # C2 spent 900 (> avg 336.67) and has no conversions


def test_nullif_prevents_division_by_zero(tiny):
    df = run_query(tiny, QUERIES["Q04"]).set_index("channel")
    # video: 0 impressions, 0 clicks, 0 conversions -> NULL ratios, not an error
    assert df.loc["video", ["ctr_pct", "cpc", "cvr_pct", "cpa"]].isna().all()
    assert df.loc["video", "roas"] == 0
    # search: 2 days for C1 must not be multiplied by its 2 conversions
    assert df.loc["search", "spend"] == 1100
    assert df.loc["search", "revenue"] == 500
    assert df.loc["search", "cpa"] == pytest.approx(1100 / 2, abs=0.01)


def test_all_queries_survive_zero_denominators(tiny):
    for qid, sql in QUERIES.items():
        run_query(tiny, sql)
