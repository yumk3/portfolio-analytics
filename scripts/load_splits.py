"""Load split history from yfinance into `splits` for every traded symbol (idempotent upsert).
ratio is the forward factor: 10:1 -> 10.0, 1:20 reverse -> 0.05.
Usage: python scripts/load_splits.py
"""
import os
import sys
import time
import pathlib
import warnings

warnings.filterwarnings("ignore")

import duckdb
import yfinance as yf
from dotenv import load_dotenv

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DB_PATH = os.getenv("DB_PATH", "data/portfolio.duckdb")
db_file = pathlib.Path(DB_PATH)
if not db_file.is_absolute():
    db_file = PROJECT_ROOT / db_file

# Crypto has no corporate actions, so it is never queried.
CRYPTO = {"BTC", "ETH", "XRP", "SOL", "DOGE", "LTC", "ADA", "MATIC"}
DELAY = 0.4                       # seconds between lookups, to avoid rate limits


def main():
    if not db_file.exists():
        print("ERROR: %s not found. Run scripts/create_db.py first." % db_file)
        return 2

    con = duckdb.connect(str(db_file))
    try:
        symbols = [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM transactions "
            "WHERE symbol IS NOT NULL ORDER BY symbol").fetchall()]
        print("symbols in transactions: %d" % len(symbols))

        inserted, skipped_crypto, no_splits, unverified = 0, [], [], []

        for sym in symbols:
            if sym in CRYPTO:
                skipped_crypto.append(sym)
                continue
            try:
                series = yf.Ticker(sym).splits
                time.sleep(DELAY)
            except Exception as e:
                unverified.append((sym, type(e).__name__))
                continue

            # Empty means Yahoo has no record (common for delisted tickers),
            # not that the symbol never split.
            if series is None or len(series) == 0:
                no_splits.append(sym)
                continue

            for ts, ratio in series.items():
                con.execute(
                    "INSERT INTO splits (symbol, split_date, ratio, source)"
                    " VALUES (?,?,?,'yfinance')"
                    " ON CONFLICT (symbol, split_date) DO UPDATE SET"
                    "   ratio = excluded.ratio, source = excluded.source",
                    [sym, ts.date(), float(ratio)])
                inserted += 1

        total = con.execute("SELECT count(*) FROM splits").fetchone()[0]
        n_sym = con.execute(
            "SELECT count(DISTINCT symbol) FROM splits").fetchone()[0]

        print()
        print("split events written : %d" % inserted)
        print("rows now in splits   : %d across %d symbols" % (total, n_sym))
        print("crypto skipped       : %d %s" % (len(skipped_crypto), skipped_crypto))
        print("no splits returned   : %d" % len(no_splits))
        if unverified:
            print("lookup failed        : %d %s" % (len(unverified), unverified))
        print()
        print("Per symbol:")
        for sym, n, lo, hi in con.execute(
                "SELECT symbol, count(*), min(split_date), max(split_date) "
                "FROM splits GROUP BY 1 ORDER BY 1").fetchall():
            print("   %-7s %2d events   %s .. %s" % (sym, n, lo, hi))
    finally:
        con.close()
    print("\nDone. Safe to rerun.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
