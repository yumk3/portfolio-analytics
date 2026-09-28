"""Load split-adjusted daily closes from yfinance into `prices` for each held
symbol's full holding window, then report coverage and check against positions.
Idempotent upsert. Usage: python scripts/load_prices.py
"""
import os
import sys
import time
import pathlib
import warnings
import datetime

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

SOURCE = "yfinance"
CURRENCY = "USD"
DELAY = 0.4                       # seconds between downloads, to avoid rate limits
EPS = 1e-9

# yfinance quotes crypto against a currency pair (BTC -> BTC-USD).
CRYPTO = {"BTC", "ETH", "XRP", "SOL", "DOGE", "LTC", "ADA", "MATIC"}


def source_ticker_for(symbol):
    """Map a held symbol to the ticker yfinance knows it by."""
    return "%s-USD" % symbol if symbol in CRYPTO else symbol


def assert_unambiguous(con):
    """Return False if any symbol is held under more than one account type.

    Such a symbol could be two instruments (a coin and a stock sharing a
    ticker), which one price series cannot serve.
    """
    ambiguous = con.execute("""
        SELECT h.symbol,
               count(DISTINCT a.account_type) AS n_types,
               string_agg(DISTINCT a.account_type, ' + ') AS acct_types
        FROM holdings_daily h
        JOIN accounts a ON a.account_id = h.account_id
        GROUP BY 1 HAVING count(DISTINCT a.account_type) > 1
        ORDER BY 1
    """).fetchall()
    if ambiguous:
        print("ERROR: a symbol maps to more than one instrument.\n")
        for sym, n, kinds in ambiguous:
            print("   %-8s held under %d account types: %s  -> %s"
                  % (sym, n, kinds, source_ticker_for(sym)))
        print("\nOne price series cannot serve two instruments. "
              "Disambiguate the symbols before loading prices.")
        return False
    return True


def holding_windows(con):
    """First and last day each symbol was actually held (units != 0)."""
    return con.execute("""
        SELECT symbol,
               min(CASE WHEN abs(units) > 1e-9 THEN as_of_date END) AS first_held,
               max(CASE WHEN abs(units) > 1e-9 THEN as_of_date END) AS last_held
        FROM holdings_daily
        GROUP BY 1 ORDER BY 1
    """).fetchall()


def main():
    if not db_file.exists():
        print("ERROR: %s not found. Run scripts/create_db.py first." % db_file)
        return 2

    con = duckdb.connect(str(db_file))
    try:
        if not assert_unambiguous(con):
            return 3

        windows = holding_windows(con)
        today = con.execute("SELECT current_date").fetchone()[0]

        print("=" * 78)
        print("LOAD PRICES  (split-adjusted Close, auto_adjust=False)")
        print("=" * 78)

        before = con.execute("SELECT count(*) FROM prices").fetchone()[0]
        written, skipped_no_window, failed, mapping = 0, [], [], {}
        coverage = []

        for symbol, first_held, last_held in windows:
            if first_held is None:
                skipped_no_window.append(symbol)
                continue

            ticker = source_ticker_for(symbol)
            mapping[symbol] = ticker

            # Always re-pull the full window, never incrementally: a split
            # restates every historical Close, and rebuild_holdings restates
            # units the same way, so appending would leave pre-split prices
            # paired with post-split units.
            # Padded one day each side: the exit day nets to zero units and
            # falls outside the held window, but its close is still needed.
            start = first_held - datetime.timedelta(days=1)
            last_wanted = min(last_held + datetime.timedelta(days=1), today)
            end = last_wanted + datetime.timedelta(days=1)  # yfinance end is exclusive

            # Close with auto_adjust=False is split-adjusted but not
            # dividend-adjusted: dividends are already counted as REI units,
            # so Adj Close would count them twice.
            try:
                df = yf.download(ticker, start=start.isoformat(),
                                 end=end.isoformat(), auto_adjust=False,
                                 progress=False, actions=False)
                time.sleep(DELAY)
            except Exception as e:
                failed.append((symbol, ticker, type(e).__name__))
                continue

            if df is None or df.empty or "Close" not in df:
                coverage.append((symbol, ticker, first_held, last_held, 0))
                continue

            closes = df["Close"]
            if hasattr(closes, "columns"):        # yfinance may return a frame
                closes = closes.iloc[:, 0]

            n = 0
            for ts, close in closes.items():
                # Observed prices only: a missing day gets no row, never a
                # NULL or a forward-filled value.
                if close is None or close != close:      # NaN
                    continue
                con.execute(
                    "INSERT INTO prices (symbol, price_date, close_price,"
                    " currency, source, source_ticker) VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT (symbol, price_date) DO UPDATE SET"
                    "   close_price   = excluded.close_price,"
                    "   currency      = excluded.currency,"
                    "   source        = excluded.source,"
                    "   source_ticker = excluded.source_ticker,"
                    "   loaded_at     = now()",
                    [symbol, ts.date(), float(close), CURRENCY, SOURCE, ticker])
                n += 1
                written += 1
            coverage.append((symbol, ticker, first_held, last_held, n))

        after = con.execute("SELECT count(*) FROM prices").fetchone()[0]

        print("  symbols processed   : %d" % len(windows))
        print("  price rows written  : %d" % written)
        print("  prices rows         : %d -> %d" % (before, after))
        if skipped_no_window:
            print("  skipped (never held): %s" % ", ".join(skipped_no_window))
        if failed:
            print("  download failed     : %s" % failed)

        print()
        print("=" * 78)
        print("TICKER MAPPING  (recorded per row in prices.source_ticker)")
        print("=" * 78)
        remapped = {s: t for s, t in mapping.items() if s != t}
        for s, t in sorted(remapped.items()):
            print("   %-8s -> %s" % (s, t))
        print("   %d symbols remapped, %d map to themselves"
              % (len(remapped), len(mapping) - len(remapped)))

        # Weekends and holidays are expected absences for equities, so only
        # missing weekdays are reported as potential gaps.
        print()
        print("=" * 78)
        print("COVERAGE  (held days with no observed price)")
        print("=" * 78)
        cov = con.execute("""
            WITH held AS (
                SELECT h.symbol, h.as_of_date,
                       dayofweek(h.as_of_date) AS dow,
                       p.close_price
                FROM holdings_daily h
                LEFT JOIN prices p
                  ON p.symbol = h.symbol AND p.price_date = h.as_of_date
                WHERE abs(h.units) > 1e-9
            )
            SELECT symbol,
                   count(*) AS held_days,
                   count(close_price) AS priced,
                   sum(CASE WHEN close_price IS NULL
                             AND dow NOT IN (0, 6) THEN 1 ELSE 0 END) AS miss_weekday,
                   sum(CASE WHEN close_price IS NULL
                             AND dow IN (0, 6) THEN 1 ELSE 0 END) AS miss_weekend
            FROM held GROUP BY 1 ORDER BY 4 DESC, 1
        """).fetchall()

        crypto_syms = {s for s in mapping if s in CRYPTO}
        zero_price, real_gaps, clean = [], [], []
        for sym, held, priced, mwd, mwe in cov:
            if priced == 0:
                zero_price.append((sym, held, mwd, mwe))
            elif mwd > 0:
                real_gaps.append((sym, held, priced, mwd, mwe))
            else:
                clean.append(sym)

        print("  NO PRICE DATA AT ALL  (delisted - Yahoo has no record)")
        if zero_price:
            for sym, held, mwd, mwe in zero_price:
                rng = con.execute(
                    "SELECT min(as_of_date), max(as_of_date) FROM holdings_daily "
                    "WHERE symbol = ? AND abs(units) > 1e-9", [sym]).fetchone()
                print("     %-8s ticker=%-9s held %d days  %s .. %s  "
                      "(%d weekdays unpriced)"
                      % (sym, mapping.get(sym, sym), held, rng[0], rng[1], mwd))
        else:
            print("     none")

        print()
        print("  MISSING WEEKDAYS  (holidays, halts, or genuine gaps)")
        if real_gaps:
            print("     %-8s %10s %8s %10s %10s" %
                  ("SYMBOL", "HELD DAYS", "PRICED", "MISS WKDAY", "MISS WKEND"))
            for sym, held, priced, mwd, mwe in real_gaps:
                print("     %-8s %10d %8d %10d %10d"
                      % (sym, held, priced, mwd, mwe))
        else:
            print("     none")

        print()
        print("  FULLY COVERED on every weekday held: %d symbols" % len(clean))
        print("     %s" % ", ".join(sorted(clean)))
        tot_wkend = sum(r[4] for r in cov)
        print()
        print("  weekend/non-trading days skipped (expected): %d" % tot_wkend)
        print("  crypto symbols (trade daily): %s"
              % ", ".join(sorted(crypto_syms)))

        print()
        print("=" * 78)
        print("VALIDATION  (rebuilt units x latest close vs positions.market_value)")
        print("=" * 78)
        rows = con.execute("""
            WITH latest_pos AS (
                SELECT account_id, symbol, quantity, market_value
                FROM positions
                WHERE as_of_date = (SELECT max(as_of_date) FROM positions)
            ),
            latest_close AS (
                SELECT symbol, close_price, price_date,
                       row_number() OVER (PARTITION BY symbol
                                          ORDER BY price_date DESC) AS rn
                FROM prices
            ),
            rebuilt AS (
                SELECT account_id, symbol, units
                FROM holdings_daily
                WHERE as_of_date = (SELECT max(as_of_date) FROM holdings_daily)
            )
            SELECT a.account_name, p.symbol, r.units, c.close_price,
                   c.price_date, r.units * c.close_price AS computed,
                   p.market_value,
                   CASE WHEN p.market_value IS NULL OR p.market_value = 0
                        THEN NULL
                        ELSE 100.0 * (r.units * c.close_price - p.market_value)
                             / p.market_value END AS pct_diff
            FROM latest_pos p
            JOIN accounts a ON a.account_id = p.account_id
            LEFT JOIN rebuilt r
              ON r.account_id = p.account_id AND r.symbol = p.symbol
            LEFT JOIN latest_close c ON c.symbol = p.symbol AND c.rn = 1
            ORDER BY a.account_name, p.symbol
        """).fetchall()

        print("  %-22s %-6s %13s %11s %-11s %12s %12s %8s" %
              ("ACCOUNT", "SYMBOL", "UNITS", "CLOSE", "CLOSE DATE",
               "COMPUTED", "MARKET_VALUE", "DIFF %"))
        over = []
        for acct, sym, units, close, pdate, computed, mv, pct in rows:
            print("  %-22s %-6s %13s %11s %-11s %12s %12s %8s"
                  % (acct, sym,
                     "%.8f" % units if units is not None else "n/a",
                     "%.4f" % close if close is not None else "n/a",
                     pdate if pdate is not None else "n/a",
                     "%.4f" % computed if computed is not None else "n/a",
                     "%.4f" % mv if mv is not None else "n/a",
                     "%+.2f%%" % pct if pct is not None else "n/a"))
            if pct is not None and abs(pct) > 1.0:
                over.append((acct, sym, pct, pdate, close, units, computed, mv))

        print("\n  positions checked: %d   within 1%%: %d   over 1%%: %d"
              % (len(rows), len(rows) - len(over), len(over)))
        if over:
            print()
            for acct, sym, pct, pdate, close, units, computed, mv in over:
                print("  OVER 1%%: %s %s  diff %+.2f%%" % (acct, sym, pct))
                print("     latest close %s on %s; units %.8f -> %.4f vs %.4f"
                      % (close, pdate, units, computed, mv))
    finally:
        con.close()
    print("\nDone. Safe to rerun.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
