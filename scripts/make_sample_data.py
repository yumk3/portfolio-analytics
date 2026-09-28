"""Build sample_data/sample.duckdb: a fully fabricated, offline dataset (create_db.py schema)
for trying the pipeline without a .env, a SnapTrade account or network access.
Usage: python scripts/make_sample_data.py [--out PATH]
"""
import argparse
import csv
import datetime
import io
import math
import pathlib
import random
import sys

import duckdb

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import create_db                                             # noqa: E402  (schema only)

SAMPLE_DIR = REPO / "sample_data"
DEFAULT_OUT = SAMPLE_DIR / "sample.duckdb"
PRICES_CSV = SAMPLE_DIR / "sample_prices.csv"
OPENING_EXAMPLE = REPO / "inputs" / "opening_balances.example.csv"

# Everything below is invented. Fixed seed and dates make the output reproducible.
SEED = 20240902
START = datetime.date(2024, 9, 2)        # first trading day of the sample
END = datetime.date(2026, 9, 25)         # last price and position date
START_PRICE = {"SPY": 560.0, "AAPL": 225.0, "MSFT": 430.0, "JNJ": 160.0}
XYZ_START_PRICE = 15.0                   # split-adjusted, i.e. already in today's terms
SPLITS = [("XYZ", datetime.date(2024, 6, 10), 10.0)]
SPY_DIVIDEND_PER_UNIT = 1.70             # reinvested (DIVIDEND + REI)
JNJ_DIVIDEND_PER_UNIT = 1.20             # paid to cash (DIVIDEND only)


def business_days(first, last):
    d = first
    while d <= last:
        if d.weekday() < 5:
            yield d
        d += datetime.timedelta(days=1)


def price_series(symbol, first, start_price):
    """Deterministic random walk of split-adjusted closes, one per weekday."""
    rng = random.Random("%s-%s" % (SEED, symbol))
    price, out = start_price, {}
    for d in business_days(first, END):
        out[d] = round(price, 2)
        price *= math.exp(rng.gauss(0.0003, 0.012))
    return out


def read_opening_example():
    with open(OPENING_EXAMPLE, encoding="utf-8", newline="") as f:
        lines = [l for l in f if l.strip() and not l.lstrip().startswith("#")]
    return [{k: v.strip() for k, v in r.items()} for r in csv.DictReader(lines)]


def first_weekday_of_month(year, month):
    d = datetime.date(year, month, 1)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


def build_transactions(prices, account_id):
    """Monthly buys, a few sells, and quarterly dividends over the sample window."""
    txns, units = [], {}

    def add(kind, sym, d, amount=None, qty=None):
        px = prices[sym][d]
        if qty is None:
            qty = round(abs(amount) / px, 6) * (1 if kind in ("BUY", "REI") else -1)
        if amount is None:
            amount = round(-qty * px, 2)
        units[sym] = units.get(sym, 0.0) + (qty if kind != "DIVIDEND" else 0.0)
        txns.append(("SAMPLE-%04d" % (len(txns) + 1), account_id, sym, kind,
                     qty if kind != "DIVIDEND" else 0.0, px if kind != "DIVIDEND" else 0.0,
                     amount, 0.0, "USD", d, d))

    y, m = START.year, START.month
    n = 0
    while datetime.date(y, m, 1) <= END:
        d = first_weekday_of_month(y, m)
        if START <= d <= END:
            add("BUY", "SPY", d, amount=-200.0)
            add("BUY", "AAPL" if n % 2 == 0 else "MSFT", d, amount=-100.0)
            if m in (1, 4, 7, 10):
                add("BUY", "JNJ", d, amount=-150.0)
            if m in (3, 6, 9, 12) and n > 0:
                div_day = d + datetime.timedelta(days=14)
                while div_day.weekday() >= 5:
                    div_day += datetime.timedelta(days=1)
                spy_div = round(units["SPY"] * SPY_DIVIDEND_PER_UNIT / 4, 2)
                add("DIVIDEND", "SPY", div_day, amount=spy_div)
                add("REI", "SPY", div_day, amount=-spy_div)
                if units.get("JNJ"):
                    add("DIVIDEND", "JNJ", div_day,
                        amount=round(units["JNJ"] * JNJ_DIVIDEND_PER_UNIT / 4, 2))
            if (y, m) == (2025, 6):
                add("SELL", "AAPL", d, qty=-round(units["AAPL"] * 0.25, 6))
            if (y, m) == (2026, 3):
                add("SELL", "MSFT", d, qty=-round(units["MSFT"] * 0.40, 6))
            if (y, m) == (2025, 3):
                add("SELL", "XYZ", d, qty=-20.0)     # post-split share count
            n += 1
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return txns


def split_factor(symbol, when):
    f = 1.0
    for sym, d, ratio in SPLITS:
        if sym == symbol and d > when:
            f *= ratio
    return f


def prices_csv_text(prices):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["symbol", "price_date", "close_price"])
    for sym in sorted(prices):
        for d in sorted(prices[sym]):
            w.writerow([sym, d.isoformat(), "%.2f" % prices[sym][d]])
    return buf.getvalue()


def main(out):
    out = pathlib.Path(out).resolve()
    real_data = (REPO / "data").resolve()
    if out == real_data or real_data in out.parents:
        print("ERROR: refusing to write sample data under %s" % real_data)
        return 2

    opening = read_opening_example()
    types = sorted({r["account_type"] for r in opening} | {"INDIVIDUAL"})
    accounts = {t: "ACCT-%04d" % (i + 1) for i, t in enumerate(types)}
    main_acct = accounts["INDIVIDUAL"]

    prices = {s: price_series(s, START, p) for s, p in START_PRICE.items()}
    first_xyz = min([datetime.date.fromisoformat(r["as_of_date"]) for r in opening
                     if r["symbol"] == "XYZ"] + [START])
    prices["XYZ"] = price_series("XYZ", first_xyz, XYZ_START_PRICE)
    for r in opening:
        if r["symbol"] not in prices:
            prices[r["symbol"]] = price_series(r["symbol"],
                                               datetime.date.fromisoformat(r["as_of_date"]), 50.0)

    text = prices_csv_text(prices)
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    if not PRICES_CSV.exists() or PRICES_CSV.read_text(encoding="utf-8") != text:
        PRICES_CSV.write_text(text, encoding="utf-8", newline="\n")

    txns = build_transactions(prices, main_acct)

    # Latest broker snapshot = what the transactions imply, so the rebuild reconciles.
    final = {}
    for r in opening:
        d = datetime.date.fromisoformat(r["as_of_date"])
        key = (accounts[r["account_type"]], r["symbol"])
        final[key] = final.get(key, 0.0) + float(r["units"]) * split_factor(r["symbol"], d)
    for t in txns:
        key = (t[1], t[2])
        final[key] = final.get(key, 0.0) + t[4] * split_factor(t[2], t[9])

    out.parent.mkdir(parents=True, exist_ok=True)
    for stale in (out, out.with_name(out.name + ".wal")):
        if stale.exists():
            stale.unlink()
    con = duckdb.connect(str(out))
    try:
        for stmt in create_db.SCHEMA:
            con.execute(stmt)
        con.execute(create_db.MACRO)
        for t, aid in accounts.items():
            con.execute("INSERT INTO accounts VALUES (?, 'Example Brokerage', ?, ?, 'USD', ?)",
                        [aid, "Example %s" % t.title(), t, datetime.datetime.combine(END, datetime.time())])
        for r in opening:
            con.execute("INSERT INTO opening_balances (account_id, symbol, as_of_date, units, "
                        "source, note) VALUES (?, ?, ?, ?, ?, ?)",
                        [accounts[r["account_type"]], r["symbol"], r["as_of_date"],
                         float(r["units"]), r["source"], r["note"]])
        con.executemany("INSERT INTO splits (symbol, split_date, ratio, source) "
                        "VALUES (?, ?, ?, 'sample')", SPLITS)
        con.executemany("INSERT INTO transactions (transaction_id, account_id, symbol, txn_type, "
                        "quantity, price, amount, fee, currency, trade_date, settle_date) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", txns)
        con.execute("INSERT INTO prices (symbol, price_date, close_price, currency, source, "
                    "source_ticker) SELECT symbol, price_date, close_price, 'USD', 'sample', "
                    "symbol FROM read_csv(?, header = true, "
                    "columns = {'symbol': 'VARCHAR', 'price_date': 'DATE', 'close_price': 'DOUBLE'})",
                    [str(PRICES_CSV)])
        for (aid, sym), units in sorted(final.items()):
            if abs(units) > 1e-9:
                px = prices[sym][END]
                con.execute("INSERT INTO positions (account_id, symbol, as_of_date, quantity, "
                            "avg_cost, market_value, currency) VALUES (?, ?, ?, ?, NULL, ?, 'USD')",
                            [aid, sym, END, units, units * px])
        counts = {t: con.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
                  for t in ("accounts", "transactions", "prices", "splits",
                            "opening_balances", "positions")}
    finally:
        con.close()

    print("Sample database: %s" % out)
    print("Price file     : %s" % PRICES_CSV)
    for t, n in counts.items():
        print("  %-17s %d rows" % (t, n))
    print("All data is fabricated. Next:")
    print("  python scripts/rebuild_holdings.py --db %s" % out)
    print("  python scripts/value_portfolio.py --db %s" % out)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the fabricated sample database.")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="where to write the database (default: sample_data/sample.duckdb)")
    sys.exit(main(ap.parse_args().out))
