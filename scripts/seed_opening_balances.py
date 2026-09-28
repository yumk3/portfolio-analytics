"""Seed opening_balances (units held before transaction history begins) from
inputs/opening_balances.csv. Idempotent upsert.
Usage: python scripts/seed_opening_balances.py
"""
import os
import sys
import pathlib

import duckdb
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import local_inputs                                          # noqa: E402

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DB_PATH = os.getenv("DB_PATH", "data/portfolio.duckdb")
db_file = pathlib.Path(DB_PATH)
if not db_file.is_absolute():
    db_file = PROJECT_ROOT / db_file

# Without these, a sale predating the recorded history rebuilds as a negative
# holding. Unexplained small residuals deliberately get no entry: a tiny
# opening balance and a missing fractional trade are indistinguishable, and
# inventing one would fabricate history.
OPENING_BALANCES = local_inputs.opening_balances()

UPSERT = """
INSERT INTO opening_balances
    (account_id, symbol, as_of_date, units, source, note)
VALUES (?,?,?,?,?,?)
ON CONFLICT (account_id, symbol, as_of_date) DO UPDATE SET
    units  = excluded.units,
    source = excluded.source,
    note   = excluded.note
"""


def main():
    if not db_file.exists():
        print("ERROR: %s not found. Run scripts/create_db.py first." % db_file)
        return 2

    con = duckdb.connect(str(db_file))
    try:
        # Keyed by account type, not id: re-linking a brokerage issues new ids.
        types = {r[0]: r[1] for r in con.execute(
            "SELECT account_type, account_id FROM accounts").fetchall()}

        missing = sorted({t for t, *_ in OPENING_BALANCES} - set(types))
        if missing:
            print("ERROR: no account with account_type %s. "
                  "Run scripts/ingest_snaptrade.py first." % missing)
            return 2

        for acct_type, symbol, as_of, units, source, note in OPENING_BALANCES:
            con.execute(UPSERT, [types[acct_type], symbol, as_of, units,
                                 source, note])

        print("opening_balances rows: %d\n"
              % con.execute("SELECT count(*) FROM opening_balances").fetchone()[0])
        for r in con.execute(
                "SELECT a.account_name, o.symbol, o.as_of_date, o.units, "
                "o.source, o.note FROM opening_balances o "
                "JOIN accounts a ON a.account_id = o.account_id "
                "ORDER BY o.symbol").fetchall():
            print("  %-22s %-5s %s  units=%-10s source=%s" % r[:5])
            print("      note: %s" % r[5])
    finally:
        con.close()
    print("\nDone. Safe to rerun.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
