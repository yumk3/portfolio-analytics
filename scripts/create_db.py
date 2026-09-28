"""Create every portfolio table (CREATE ... IF NOT EXISTS; never drops or inserts).
Usage: python scripts/create_db.py. FX as-of lookup: SELECT rate FROM fx_rates WHERE
from_currency = ? AND to_currency = ? AND rate_date <= ? ORDER BY rate_date DESC LIMIT 1
"""
import os
import sys
import pathlib

import duckdb
from dotenv import load_dotenv

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DB_PATH = os.getenv("DB_PATH", "data/portfolio.duckdb")
db_file = pathlib.Path(DB_PATH)
if not db_file.is_absolute():
    db_file = PROJECT_ROOT / db_file


# Enforced by CHECK constraints so a typo fails at write time instead of
# silently creating a new category.
ARCHIVE_STATUSES = ("automated", "blocked", "manual_permanent", "unresearched")
VALIDATIONS = ("passed", "rejected_html", "rejected_json",
               "rejected_small", "fetch_failed")


def _in_list(col, values):
    return "%s IN (%s)" % (col, ", ".join("'%s'" % v for v in values))


SCHEMA = [
    # One canonical row per ticker so holdings, positions and prices join.
    """
    CREATE TABLE IF NOT EXISTS securities (
        symbol         VARCHAR PRIMARY KEY,
        name           VARCHAR,
        security_type  VARCHAR,
        cusip          VARCHAR,
        isin           VARCHAR,
        sedol          VARCHAR,
        exchange       VARCHAR,
        currency       VARCHAR,
        country        VARCHAR,
        sector         VARCHAR,
        updated_at     TIMESTAMP DEFAULT current_timestamp
    )
    """,

    # fetch_method is how a fund is downloaded; archive_status is whether it can be.
    """
    CREATE TABLE IF NOT EXISTS funds (
        ticker            VARCHAR PRIMARY KEY,
        name              VARCHAR,
        issuer            VARCHAR,
        cusip             VARCHAR,
        fetch_method      VARCHAR,
        added_date        DATE DEFAULT current_date,
        archive_status    VARCHAR NOT NULL DEFAULT 'unresearched'
                          CHECK (%s),
        status_note       VARCHAR,
        last_attempted_at TIMESTAMP
    )
    """ % _in_list("archive_status", ARCHIVE_STATUSES),

    # One row per download attempt, successful or not: a failed fetch yields
    # no holdings rows, so this is its only trace. file_path and sha256 are
    # nullable for that reason; a 'passed' row must name a file.
    """
    CREATE SEQUENCE IF NOT EXISTS raw_files_seq
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_files (
        raw_file_id   BIGINT PRIMARY KEY DEFAULT nextval('raw_files_seq'),
        fund_ticker   VARCHAR NOT NULL,
        snapshot_date DATE    NOT NULL,
        file_path     VARCHAR,
        sha256        VARCHAR,
        byte_size     BIGINT,
        content_type  VARCHAR,
        http_status   INTEGER,
        final_url     VARCHAR,
        fetched_at    TIMESTAMP,
        validation    VARCHAR NOT NULL CHECK (%s),
        note          VARCHAR,
        loaded_at     TIMESTAMP DEFAULT current_timestamp,
        CHECK (validation <> 'passed' OR file_path IS NOT NULL)
    )
    """ % _in_list("validation", VALIDATIONS),

    # currency is stored verbatim: iShares reports UK holdings in GBp (pence),
    # and normalising it to GBP would overstate them 100x.
    """
    CREATE TABLE IF NOT EXISTS fund_holdings (
        fund_ticker     VARCHAR NOT NULL,
        snapshot_date   DATE    NOT NULL,
        holding_ticker  VARCHAR NOT NULL,
        holding_name    VARCHAR,
        identifier      VARCHAR,
        weight_pct      DOUBLE,
        shares          DOUBLE,
        market_value    DOUBLE,
        currency        VARCHAR,
        raw_file_id     BIGINT REFERENCES raw_files(raw_file_id),
        source          VARCHAR,
        loaded_at       TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (fund_ticker, snapshot_date, holding_ticker)
    )
    """,

    # Read with an as-of lookup (latest rate on or before a date), which also
    # covers weekends, gaps and fixed denominations such as GBX.
    """
    CREATE TABLE IF NOT EXISTS fx_rates (
        from_currency VARCHAR NOT NULL,
        to_currency   VARCHAR NOT NULL,
        rate_date     DATE    NOT NULL,
        rate          DOUBLE,
        source        VARCHAR,
        loaded_at     TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (from_currency, to_currency, rate_date)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS accounts (
        account_id    VARCHAR PRIMARY KEY,
        brokerage     VARCHAR,
        account_name  VARCHAR,
        account_type  VARCHAR,
        currency      VARCHAR,
        last_synced   TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS positions (
        account_id    VARCHAR NOT NULL,
        symbol        VARCHAR NOT NULL,
        as_of_date    DATE    NOT NULL,
        quantity      DOUBLE,
        avg_cost      DOUBLE,
        market_value  DOUBLE,
        currency      VARCHAR,
        loaded_at     TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (account_id, symbol, as_of_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transactions (
        transaction_id  VARCHAR PRIMARY KEY,
        account_id      VARCHAR,
        symbol          VARCHAR,
        txn_type        VARCHAR,
        quantity        DOUBLE,
        price           DOUBLE,
        amount          DOUBLE,
        fee             DOUBLE,
        currency        VARCHAR,
        trade_date      DATE,
        settle_date     DATE,
        loaded_at       TIMESTAMP DEFAULT current_timestamp
    )
    """,
    # Split-adjusted Close, not Adj Close: dividends are already counted as
    # REI units, and Close pairs correctly with units in today's share terms.
    # close_price is NOT NULL so a missing day stays an absent row, keeping
    # gaps visible. source_ticker is the ticker actually queried (BTC-USD).
    """
    CREATE TABLE IF NOT EXISTS prices (
        symbol        VARCHAR NOT NULL,
        price_date    DATE    NOT NULL,
        close_price   DOUBLE  NOT NULL,
        currency      VARCHAR,
        source        VARCHAR,
        source_ticker VARCHAR,
        loaded_at     TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (symbol, price_date)
    )
    """,

    # ratio is the forward factor: 10:1 -> 10, 1:20 reverse -> 0.05.
    """
    CREATE TABLE IF NOT EXISTS splits (
        symbol     VARCHAR NOT NULL,
        split_date DATE    NOT NULL,
        ratio      DOUBLE  NOT NULL,
        source     VARCHAR NOT NULL DEFAULT 'yfinance',
        loaded_at  TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (symbol, split_date)
    )
    """,

    # Units held before the transaction history begins; without them an early
    # sale rebuilds as a negative holding. Split-adjusted like transactions.
    """
    CREATE TABLE IF NOT EXISTS opening_balances (
        account_id VARCHAR NOT NULL,
        symbol     VARCHAR NOT NULL,
        as_of_date DATE    NOT NULL,
        units      DOUBLE  NOT NULL,
        source     VARCHAR NOT NULL,
        note       VARCHAR,
        loaded_at  TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (account_id, symbol, as_of_date)
    )
    """,

    # Units are in today's share terms: each quantity is scaled by every later
    # split (made-up XYZ, 10:1 in 2024: 2 shares bought in 2022 are stored as
    # 20 units), so dates compare directly but old rows do not match
    # statements of the day. Dense rows, zeros included, make exits explicit.
    """
    CREATE TABLE IF NOT EXISTS holdings_daily (
        account_id VARCHAR NOT NULL,
        symbol     VARCHAR NOT NULL,
        as_of_date DATE    NOT NULL,
        units      DOUBLE,
        source     VARCHAR NOT NULL DEFAULT 'rebuilt_from_transactions',
        rebuilt_at TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (account_id, symbol, as_of_date)
    )
    """,

    """
    CREATE SEQUENCE IF NOT EXISTS sync_log_seq
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_log (
        id             BIGINT PRIMARY KEY DEFAULT nextval('sync_log_seq'),
        run_ts         TIMESTAMP DEFAULT current_timestamp,
        source         VARCHAR,
        status         VARCHAR,
        rows_affected  BIGINT,
        message        VARCHAR
    )
    """,
]

MACRO = """
CREATE OR REPLACE MACRO fx_rate_asof(p_from, p_to, p_date) AS (
    SELECT rate FROM fx_rates
    WHERE from_currency = p_from
      AND to_currency   = p_to
      AND rate_date    <= p_date
    ORDER BY rate_date DESC
    LIMIT 1
)
"""


def main():
    db_file.parent.mkdir(parents=True, exist_ok=True)
    existed = db_file.exists()

    print("Database file  : %s" % db_file)
    print("Already existed: %s" % ("yes - adding anything missing"
                                   if existed else "no - creating fresh"))
    print()

    con = duckdb.connect(str(db_file))
    try:
        for stmt in SCHEMA:
            con.execute(stmt)
        try:
            con.execute(MACRO)
            macro_ok = True
        except Exception as e:
            macro_ok = False
            print("note: fx_rate_asof macro not created (%s)" % e)
            print("      use the as-of SELECT in this file's docstring instead.\n")

        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()]

        print("Tables (%d):\n" % len(tables))
        for t in tables:
            cols = con.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema='main' AND table_name=? "
                "ORDER BY ordinal_position", [t]).fetchall()
            n = con.execute('SELECT count(*) FROM "%s"' % t).fetchone()[0]
            print("  %s  (%d columns, %d rows)" % (t, len(cols), n))
            for name, dtype, nullable in cols:
                print("      - %-18s %-12s%s"
                      % (name, dtype, "" if nullable == "YES" else " NOT NULL"))
            print()

        if macro_ok:
            print("Helper macro   : fx_rate_asof(from, to, date)")
        print("Allowed archive_status: %s" % ", ".join(ARCHIVE_STATUSES))
        print("Allowed validation    : %s" % ", ".join(VALIDATIONS))
    finally:
        con.close()

    print("\nDone. Structure only - no data was inserted.")
    print("Run scripts/seed_funds.py to populate the funds table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
