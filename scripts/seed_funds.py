"""Upsert the funds table from archiver/config.ini, plus the fixed GBX/GBP FX row.
config.ini is authoritative: existing rows are overwritten on every run.
Usage: python scripts/seed_funds.py
"""
import os
import sys
import pathlib
import configparser

import duckdb
from dotenv import load_dotenv

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DB_PATH = os.getenv("DB_PATH", "data/portfolio.duckdb")
db_file = pathlib.Path(DB_PATH)
if not db_file.is_absolute():
    db_file = PROJECT_ROOT / db_file


# Shared with the ETF archiver so the fund list has a single source.
FUNDS_CONFIG = PROJECT_ROOT / "archiver" / "config.ini"


def load_funds(path=FUNDS_CONFIG):
    """Return one funds-table row per [fund TICKER] section; blanks become NULL.

    archive_status is one of: automated, blocked (endpoint known, issuer
    blocks it), manual_permanent (no public endpoint) or unresearched
    (not yet examined - not a conclusion). name is left NULL on purpose.
    """
    cp = configparser.ConfigParser(interpolation=None)
    if not cp.read(path, encoding="utf-8"):
        raise SystemExit("ERROR: %s not found. Copy archiver/config.example.ini "
                         "to archiver/config.ini and list your funds." % path)
    funds = []
    for sec in cp.sections():
        if not sec.lower().startswith("fund "):
            continue
        f = cp[sec]
        val = lambda k: f.get(k, "").strip() or None
        funds.append((sec[5:].strip().upper(), val("issuer_name"), val("cusip"),
                      (f.get("fetch_method", "") or "cffi").strip().lower(),
                      val("archive_status") or "unresearched",
                      val("status_note"), val("last_attempted_at")))
    return funds

# GBX (pence) is always exactly 1/100 GBP, so one row with a sentinel date
# satisfies every as-of lookup (rate_date <= target). No live FX source.
FX_SEED = [("GBX", "GBP", "1900-01-01", 0.01, "fixed_denomination")]


UPSERT_FUND = """
INSERT INTO funds
    (ticker, issuer, cusip, fetch_method, archive_status,
     status_note, last_attempted_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (ticker) DO UPDATE SET
    issuer            = excluded.issuer,
    cusip             = excluded.cusip,
    fetch_method      = excluded.fetch_method,
    archive_status    = excluded.archive_status,
    status_note       = excluded.status_note,
    last_attempted_at = excluded.last_attempted_at
"""

UPSERT_FX = """
INSERT INTO fx_rates (from_currency, to_currency, rate_date, rate, source)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (from_currency, to_currency, rate_date) DO UPDATE SET
    rate   = excluded.rate,
    source = excluded.source
"""


def main():
    if not db_file.exists():
        print("ERROR: %s does not exist. Run scripts/create_db.py first."
              % db_file)
        return 1

    con = duckdb.connect(str(db_file))
    try:
        for row in load_funds():
            con.execute(UPSERT_FUND, list(row))
        for row in FX_SEED:
            con.execute(UPSERT_FX, list(row))

        n_funds = con.execute("SELECT count(*) FROM funds").fetchone()[0]
        n_fx = con.execute("SELECT count(*) FROM fx_rates").fetchone()[0]
        print("Seeded %d funds and %d fx_rates row(s).\n" % (n_funds, n_fx))

        print("Count by archive_status:")
        for status, n in con.execute(
                "SELECT archive_status, count(*) FROM funds "
                "GROUP BY archive_status ORDER BY 2 DESC, 1").fetchall():
            print("   %-18s %d" % (status, n))
    finally:
        con.close()
    print("\nDone. Safe to rerun.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
