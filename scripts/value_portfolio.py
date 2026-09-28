"""Value every held position daily into positions_value_daily, with per-account
and ALL totals in portfolio_value_daily (securities only; rebuilt each run).
Usage: python scripts/value_portfolio.py
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

# Database: --db, else PORTFOLIO_DB, else DB_PATH (.env), else the default.
# Relative paths resolve against the project root.
DB_PATH = os.getenv("PORTFOLIO_DB") or os.getenv("DB_PATH", "data/portfolio.duckdb")
db_file = pathlib.Path(DB_PATH)
if not db_file.is_absolute():
    db_file = PROJECT_ROOT / db_file

# Beyond this a row is 'missing' with NULL value - never zero or guessed.
FORWARD_FILL_MAX_DAYS = 5      # calendar days; covers a long holiday weekend
ZERO_TOL = 1e-9

# Reviewed negative residuals: reported as accepted; any other negative
# quantity is a validation failure.
ACCEPTED_EXCEPTIONS = local_inputs.accepted_exceptions("valuation")

# Money is computed in DECIMAL, not float. quantity x price must fit DuckDB's
# 38-digit limit: (20,10) x (14,4) -> (34,14).
DEC_QTY = "DECIMAL(20,10)"
DEC_PRICE = "DECIMAL(14,4)"
DEC_MONEY = "DECIMAL(20,4)"


def build(con):
    con.execute("DROP TABLE IF EXISTS portfolio_value_daily")
    con.execute("DROP TABLE IF EXISTS positions_value_daily")

    # Units are in today's share terms and prices are split-adjusted Close
    # (auto_adjust=False), so the product is right across splits (made-up
    # XYZ, 10:1: 20 units x $15 = 2 shares x $150). Never adjust one side
    # alone. Close is not dividend-adjusted because reinvested dividends are
    # already counted as REI units. ASOF JOIN takes the latest price on or
    # before each date.
    con.execute(f"""
        CREATE TABLE positions_value_daily AS
        WITH held AS (
            SELECT account_id, symbol, as_of_date AS date, units
            FROM holdings_daily
            WHERE abs(units) > {ZERO_TOL}
        ),
        priced AS (
            SELECT h.date, h.account_id, h.symbol, h.units,
                   p.close_price, p.price_date
            FROM held h
            ASOF LEFT JOIN prices p
              ON p.symbol = h.symbol
             AND p.price_date <= h.date
        ),
        classified AS (
            SELECT date, account_id, symbol, units, close_price, price_date,
                   CASE WHEN price_date IS NULL THEN NULL
                        ELSE date_diff('day', price_date, date) END AS days_stale
            FROM priced
        )
        SELECT
            date,
            account_id,
            symbol,
            CAST(units AS {DEC_QTY})                       AS quantity,
            CASE WHEN price_date IS NULL
                      OR days_stale > {FORWARD_FILL_MAX_DAYS}
                 THEN NULL ELSE CAST(close_price AS {DEC_PRICE}) END AS price,
            CASE WHEN price_date IS NULL
                      OR days_stale > {FORWARD_FILL_MAX_DAYS}
                 THEN NULL ELSE price_date END             AS price_date,
            CASE WHEN price_date IS NULL
                      OR days_stale > {FORWARD_FILL_MAX_DAYS}
                 THEN NULL ELSE days_stale END             AS days_stale,
            CASE WHEN price_date IS NULL
                      OR days_stale > {FORWARD_FILL_MAX_DAYS}
                 THEN NULL
                 ELSE CAST(CAST(units AS {DEC_QTY})
                           * CAST(close_price AS {DEC_PRICE}) AS {DEC_MONEY})
            END                                            AS market_value,
            CASE WHEN price_date IS NULL
                      OR days_stale > {FORWARD_FILL_MAX_DAYS} THEN 'missing'
                 WHEN days_stale = 0                        THEN 'actual'
                 ELSE 'forward_filled' END                  AS price_status
        FROM classified
    """)

    # includes_cash is always false: holdings contain no cash instrument.
    con.execute(f"""
        CREATE TABLE portfolio_value_daily AS
        WITH per_account AS (
            SELECT date, account_id,
                   CAST(sum(market_value) AS {DEC_MONEY}) AS market_value,
                   count(*)                                AS position_count,
                   sum(CASE WHEN price_status = 'missing' THEN 1 ELSE 0 END)
                                                           AS missing_price_count
            FROM positions_value_daily
            GROUP BY 1, 2
        ),
        all_accounts AS (
            SELECT date, 'ALL' AS account_id,
                   CAST(sum(market_value) AS {DEC_MONEY}) AS market_value,
                   count(*)                                AS position_count,
                   sum(CASE WHEN price_status = 'missing' THEN 1 ELSE 0 END)
                                                           AS missing_price_count
            FROM positions_value_daily
            GROUP BY 1
        )
        SELECT date, account_id, market_value, position_count,
               missing_price_count, FALSE AS includes_cash
        FROM (SELECT * FROM per_account UNION ALL SELECT * FROM all_accounts)
        ORDER BY date, (account_id = 'ALL'), account_id
    """)


def report(con):
    print("=" * 78)
    print("VALUATION BUILT")
    print("=" * 78)
    n_pos = con.execute("SELECT count(*) FROM positions_value_daily").fetchone()[0]
    n_prt = con.execute("SELECT count(*) FROM portfolio_value_daily").fetchone()[0]
    rng = con.execute("SELECT min(date), max(date) FROM positions_value_daily").fetchone()
    print("  positions_value_daily : %d rows" % n_pos)
    print("  portfolio_value_daily : %d rows" % n_prt)
    print("  date range            : %s .. %s" % rng)
    print("  totals are SECURITIES ONLY - no cash instrument exists in holdings")

    print()
    print("  price_status distribution:")
    for status, n, mx in con.execute("""
            SELECT price_status, count(*), max(days_stale)
            FROM positions_value_daily GROUP BY 1 ORDER BY 2 DESC""").fetchall():
        print("     %-15s %6d rows  (%.1f%%)   max days_stale: %s"
              % (status, n, 100.0 * n / n_pos, mx if mx is not None else "-"))

    print()
    print("  rows with NO usable price, by symbol:")
    rows = con.execute("""
        SELECT symbol, count(*), min(date), max(date)
        FROM positions_value_daily WHERE price_status = 'missing'
        GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    if rows:
        for sym, n, lo, hi in rows:
            print("     %-7s %5d rows   %s .. %s" % (sym, n, lo, hi))
        print()
        print("  what the unpriced symbols cost (net cash from transactions):")
        syms = tuple(r[0] for r in rows)
        marks = ",".join("?" * len(syms))
        for sym, bought, sold, net in con.execute(f"""
                SELECT symbol,
                       CAST(sum(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS {DEC_MONEY}),
                       CAST(sum(CASE WHEN amount > 0 THEN  amount ELSE 0 END) AS {DEC_MONEY}),
                       CAST(sum(-amount) AS {DEC_MONEY})
                FROM transactions WHERE symbol IN ({marks})
                GROUP BY 1 ORDER BY 1""", list(syms)).fetchall():
            print("     %-7s paid in $%-10s received $%-10s net outlay $%s"
                  % (sym, bought, sold, net))
    else:
        print("     none")

    print()
    print("  negative quantities (carried and flagged, not corrected):")
    neg = con.execute("""
        SELECT symbol, count(*), min(quantity), min(market_value), max(market_value)
        FROM positions_value_daily WHERE quantity < 0
        GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    if neg:
        for sym, n, minq, minv, maxv in neg:
            tag = "ACCEPTED" if sym in ACCEPTED_EXCEPTIONS else "*** NOT ON ACCEPTED LIST ***"
            print("     %-7s %5d rows  qty %s  value %s .. %s   %s"
                  % (sym, n, minq, minv, maxv, tag))
            if sym in ACCEPTED_EXCEPTIONS:
                print("        %s" % ACCEPTED_EXCEPTIONS[sym])
    else:
        print("     none")

    print()
    print("  portfolio value over time (ALL accounts, securities only):")
    for label, sql in (
            ("first date", "SELECT date, market_value FROM portfolio_value_daily "
                           "WHERE account_id='ALL' ORDER BY date LIMIT 1"),
            ("last date",  "SELECT date, market_value FROM portfolio_value_daily "
                           "WHERE account_id='ALL' ORDER BY date DESC LIMIT 1"),
            ("all-time high", "SELECT date, market_value FROM portfolio_value_daily "
                              "WHERE account_id='ALL' ORDER BY market_value DESC LIMIT 1"),
            ("all-time low",  "SELECT date, market_value FROM portfolio_value_daily "
                              "WHERE account_id='ALL' AND market_value IS NOT NULL "
                              "ORDER BY market_value ASC LIMIT 1")):
        r = con.execute(sql).fetchone()
        if r:
            print("     %-14s %s   $%s" % (label, r[0], r[1]))


def main():
    if not db_file.exists():
        print("ERROR: %s not found." % db_file)
        return 2
    con = duckdb.connect(str(db_file))
    try:
        build(con)
        report(con)
    finally:
        con.close()
    print("\nDone. Safe to rerun - both output tables are rebuilt from scratch.")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Value every held position daily.")
    ap.add_argument("--db", help="DuckDB file to use instead of PORTFOLIO_DB / DB_PATH / "
                                 "data/portfolio.duckdb (relative to the project root)")
    args = ap.parse_args()
    if args.db:
        db_file = pathlib.Path(args.db)
        if not db_file.is_absolute():
            db_file = PROJECT_ROOT / db_file
    sys.exit(main())
