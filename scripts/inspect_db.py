"""Day 2: inspect the campaign database (tables, columns, keys, row counts, sample rows).

The database is opened read-only so the original file is never modified.

Usage:
    python scripts/inspect_db.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "raw" / "marketing_campaigns.sqlite"
EXPECTED_TABLES = ["campaigns", "daily_spend", "leads", "conversions"]


def connect_readonly(path: Path = DB_PATH) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"Database not found at {path}. See README 'Day 2' for download steps.")
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def main() -> None:
    conn = connect_readonly()
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print("Tables:", tables)
    missing = set(EXPECTED_TABLES) - set(tables)
    if missing:
        raise SystemExit(f"Missing expected tables: {missing}")

    for table in tables:
        print(f"\n=== {table} ===")
        rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"rows: {rows}")
        print("columns (name, type, not null, primary key):")
        for _, name, ctype, notnull, _, pk in conn.execute(f"PRAGMA table_info({table})"):
            print(f"  {name:<16} {ctype:<10} notnull={notnull} pk={pk}")
        fks = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        for fk in fks:
            print(f"  FK: {table}.{fk[3]} -> {fk[2]}.{fk[4]}")
        if not fks:
            print("  FK: none declared")
        print("sample:", conn.execute(f"SELECT * FROM {table} LIMIT 2").fetchall())

    other = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('view', 'index') AND name NOT LIKE 'sqlite_%'").fetchall()
    print("\nViews / indexes:", other or "none")

    # Relationship checks: every child row must point at an existing parent row.
    print("\n=== Relationship checks ===")
    checks = {
        "daily_spend rows with unknown campaign_id":
            "SELECT COUNT(*) FROM daily_spend d LEFT JOIN campaigns c ON c.campaign_id = d.campaign_id WHERE c.campaign_id IS NULL",
        "leads with unknown campaign_id":
            "SELECT COUNT(*) FROM leads l LEFT JOIN campaigns c ON c.campaign_id = l.campaign_id WHERE c.campaign_id IS NULL",
        "conversions with unknown lead_id":
            "SELECT COUNT(*) FROM conversions v LEFT JOIN leads l ON l.lead_id = v.lead_id WHERE l.lead_id IS NULL",
        "leads with more than one conversion":
            "SELECT COUNT(*) FROM (SELECT lead_id FROM conversions GROUP BY lead_id HAVING COUNT(*) > 1)",
        "duplicate (campaign_id, spend_date) in daily_spend":
            "SELECT COUNT(*) FROM (SELECT campaign_id, spend_date FROM daily_spend GROUP BY 1, 2 HAVING COUNT(*) > 1)",
    }
    for label, sql in checks.items():
        print(f"  {label}: {conn.execute(sql).fetchone()[0]}")
    conn.close()


if __name__ == "__main__":
    main()
