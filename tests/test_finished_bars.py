"""load_prices.py keeps only finished daily bars. Fabricated prices; downloads and clock faked.

Crypto bars are UTC days, final once that UTC day has ended. Stock bars are final only
after a conservative evening cutoff in exchange time (20:00 New York), so early-close
days are covered too. A stored row for a day not yet finished is replaced.
"""
import datetime as dt
import importlib.util
import os
import pathlib
import subprocess
import sys
import zoneinfo

import duckdb
import pandas as pd
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
UTC = dt.timezone.utc
NY = zoneinfo.ZoneInfo("America/New_York")
D = dt.date


def ny(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=NY)


def utc(y, m, d, hh, mm=0, ss=0):
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


def load(name):
    spec = importlib.util.spec_from_file_location(name + "_under_test", SCRIPTS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def lp(tmp_path, monkeypatch):
    db = tmp_path / "prices.duckdb"
    monkeypatch.setenv("DB_PATH", str(db))
    r = subprocess.run([sys.executable, str(SCRIPTS / "create_db.py")], capture_output=True,
                       text=True, env=dict(os.environ, DB_PATH=str(db)), timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    mod = load("load_prices")
    assert mod.db_file == db                        # never the real database
    monkeypatch.setattr(mod, "DELAY", 0)
    return mod


# ------------------------------------------------------------ the rule itself
@pytest.mark.parametrize("now, bar, done", [
    (utc(2026, 11, 3, 23, 59, 59), D(2026, 11, 3), False),   # 6:59 PM New York (EST)
    (utc(2026, 11, 4, 0, 0), D(2026, 11, 3), True),          # 7:00 PM: UTC day over
    (utc(2026, 11, 4, 13, 31), D(2026, 11, 4), False),       # morning: today in progress
    (utc(2026, 10, 30, 23, 59), D(2026, 10, 30), False),     # 7:59 PM (EDT)
    (utc(2026, 10, 31, 0, 0), D(2026, 10, 30), True),
])
def test_a_crypto_bar_is_finished_once_its_utc_day_ends(lp, now, bar, done):
    assert lp.is_finished("BTC", bar, now) is done


@pytest.mark.parametrize("now, bar, done", [
    (ny(2026, 11, 4, 16, 30), D(2026, 11, 4), False),        # after the close: still waits
    (ny(2026, 11, 4, 19, 59), D(2026, 11, 4), False),
    (ny(2026, 11, 4, 20, 0), D(2026, 11, 4), True),
    (ny(2026, 10, 30, 19, 59), D(2026, 10, 30), False),      # daylight time (EDT)
    (ny(2026, 10, 30, 20, 0), D(2026, 10, 30), True),
    (ny(2026, 11, 5, 8, 0), D(2026, 11, 4), True),           # next morning: yesterday is final
    (ny(2026, 11, 5, 8, 0), D(2026, 11, 5), False),          # ...today is not
])
def test_a_stock_bar_is_finished_after_the_evening_cutoff(lp, now, bar, done):
    assert lp.is_finished("XYZ", bar, now.astimezone(UTC)) is done


@pytest.mark.parametrize("hh, mm, done", [(13, 5, False), (16, 30, False), (19, 59, False),
                                          (20, 0, True)])
def test_an_early_close_day_waits_for_the_same_cutoff(lp, hh, mm, done):
    # 2026-11-27, the day after Thanksgiving, closes at 1:00 PM; the cutoff does not move.
    assert lp.is_finished("XYZ", D(2026, 11, 27), ny(2026, 11, 27, hh, mm).astimezone(UTC)) is done


# ------------------------------------------------------------ the loader
def seed(lp, days, symbols=("XYZ", "BTC")):
    con = duckdb.connect(str(lp.db_file))
    con.execute("INSERT INTO accounts (account_id, account_name, account_type) "
                "VALUES ('ACCT-0001', 'Example Individual', 'individual')")
    for s in symbols:
        for d in days:
            con.execute("INSERT INTO holdings_daily (account_id, symbol, as_of_date, units) "
                        "VALUES ('ACCT-0001', ?, ?, 1)", [s, d])
    con.close()


def fake_download(lp, monkeypatch, bars):
    """bars: {source ticker: {date: close}}; a missing ticker raises like a failed download."""
    def download(ticker, **kw):
        if ticker not in bars:
            raise ConnectionError("offline")
        idx = pd.DatetimeIndex([pd.Timestamp(d) for d in bars[ticker]])
        return pd.DataFrame({"Close": list(bars[ticker].values())}, index=idx)
    monkeypatch.setattr(lp.yf, "download", download)


def run_at(lp, monkeypatch, capsys, now):
    monkeypatch.setattr(lp, "utc_now", lambda: now.astimezone(UTC))
    assert lp.main() == 0
    return capsys.readouterr().out


def stored(lp):
    con = duckdb.connect(str(lp.db_file), read_only=True)
    try:
        return {(s, d): c for s, d, c in con.execute(
            "SELECT symbol, price_date, close_price FROM prices").fetchall()}
    finally:
        con.close()


NOV = [D(2026, 11, d) for d in (2, 3, 4)]


def test_a_morning_run_skips_both_bars_still_in_progress(lp, monkeypatch, capsys):
    seed(lp, NOV)
    fake_download(lp, monkeypatch, {
        "XYZ": {D(2026, 11, 2): 98.0, D(2026, 11, 3): 100.0, D(2026, 11, 4): 104.0},
        "BTC-USD": {D(2026, 11, 2): 900.0, D(2026, 11, 3): 1000.0, D(2026, 11, 4): 1040.0}})
    out = run_at(lp, monkeypatch, capsys, ny(2026, 11, 4, 8, 31))
    assert stored(lp) == {("XYZ", D(2026, 11, 2)): 98.0, ("XYZ", D(2026, 11, 3)): 100.0,
                          ("BTC", D(2026, 11, 2)): 900.0, ("BTC", D(2026, 11, 3)): 1000.0}
    assert "in-progress bars skipped: 2" in out


def test_a_late_evening_run_keeps_the_days_that_have_ended(lp, monkeypatch, capsys):
    seed(lp, NOV)
    fake_download(lp, monkeypatch, {
        "XYZ": {D(2026, 11, 3): 100.0, D(2026, 11, 4): 101.0},
        "BTC-USD": {D(2026, 11, 3): 1000.0, D(2026, 11, 4): 1010.0, D(2026, 11, 5): 1020.0}})
    out = run_at(lp, monkeypatch, capsys, ny(2026, 11, 4, 23, 0))
    got = stored(lp)
    assert got[("XYZ", D(2026, 11, 4))] == 101.0 and got[("BTC", D(2026, 11, 4))] == 1010.0
    assert ("BTC", D(2026, 11, 5)) not in got and "in-progress bars skipped: 1" in out


@pytest.mark.parametrize("hh, mm, kept", [(14, 0, False), (20, 5, True)])
def test_an_early_close_bar_is_kept_only_after_the_cutoff(lp, monkeypatch, capsys, hh, mm, kept):
    seed(lp, [D(2026, 11, 26), D(2026, 11, 27)], symbols=("XYZ",))
    fake_download(lp, monkeypatch, {"XYZ": {D(2026, 11, 26): 100.0, D(2026, 11, 27): 102.0}})
    run_at(lp, monkeypatch, capsys, ny(2026, 11, 27, hh, mm))
    assert (("XYZ", D(2026, 11, 27)) in stored(lp)) is kept


def put(lp, rows):
    con = duckdb.connect(str(lp.db_file))
    con.executemany("INSERT INTO prices (symbol, price_date, close_price, currency, source,"
                    " source_ticker, loaded_at) VALUES (?, ?, ?, 'USD', 'yfinance', ?,"
                    " TIMESTAMP '2026-11-04 08:00:00')", rows)
    con.close()


def test_a_stored_row_for_an_unfinished_day_is_replaced(lp, monkeypatch, capsys):
    seed(lp, NOV)
    # As if an earlier run had stored the in-progress bars.
    put(lp, [("XYZ", D(2026, 11, 3), 100.0, "XYZ"), ("XYZ", D(2026, 11, 4), 104.0, "XYZ"),
             ("BTC", D(2026, 11, 3), 1000.0, "BTC-USD"), ("BTC", D(2026, 11, 4), 1040.0, "BTC-USD")])
    fake_download(lp, monkeypatch, {"BTC-USD": {D(2026, 11, 3): 1000.0, D(2026, 11, 4): 1041.0}})
    out = run_at(lp, monkeypatch, capsys, ny(2026, 11, 4, 9, 0))   # XYZ download fails
    assert stored(lp) == {("XYZ", D(2026, 11, 3)): 100.0, ("BTC", D(2026, 11, 3)): 1000.0}
    assert "unfinished rows removed: 2" in out
    con = duckdb.connect(str(lp.db_file), read_only=True)
    loaded = con.execute("SELECT loaded_at FROM prices WHERE symbol = 'XYZ'").fetchone()[0]
    con.close()
    assert loaded == dt.datetime(2026, 11, 4, 8, 0)            # finished rows untouched


# ------------------------------------------------------------ valuation and check 9c
def snapshot(lp, day, prices, loaded):
    con = duckdb.connect(str(lp.db_file))
    con.execute("DELETE FROM positions")
    for sym, value in prices.items():
        con.execute("INSERT INTO positions (account_id, symbol, as_of_date, quantity, "
                    "market_value, loaded_at) VALUES ('ACCT-0001', ?, ?, 1, ?, ?)",
                    [sym, day, value, loaded])
    con.close()


def value_and_9c(lp, day, capsys):
    vp, vv = load("value_portfolio"), load("validate_valuation")
    con = duckdb.connect(str(lp.db_file))
    try:
        vp.build(con)
        rows = {s: (pd_, st) for s, pd_, st in con.execute(
            "SELECT symbol, price_date, price_status FROM positions_value_daily WHERE date = ?",
            [day]).fetchall()}
        capsys.readouterr()
        vv.check9c_value(con, day)
        (num, _, status), = vv.results
        return rows, status, capsys.readouterr().out
    finally:
        con.close()


def test_a_morning_run_carries_yesterday_forward_and_9c_calls_it_timing(lp, monkeypatch, capsys):
    day = D(2026, 11, 4)
    seed(lp, NOV)
    fake_download(lp, monkeypatch, {
        "XYZ": {D(2026, 11, 3): 100.0, day: 104.0},
        "BTC-USD": {D(2026, 11, 3): 1000.0, day: 1040.0}})
    run_at(lp, monkeypatch, capsys, ny(2026, 11, 4, 8, 31))
    snapshot(lp, day, {"XYZ": 103.0, "BTC": 1040.0}, dt.datetime(2026, 11, 4, 8, 20))
    rows, status, out = value_and_9c(lp, day, capsys)
    assert rows == {"XYZ": (D(2026, 11, 3), "forward_filled"),
                    "BTC": (D(2026, 11, 3), "forward_filled")}      # never "actual"
    assert status == "WARN" and "TIMING: our close 2026-11-03 carried forward" in out


def test_an_evening_run_uses_the_actual_close_and_9c_compares_it(lp, monkeypatch, capsys):
    day = D(2026, 11, 4)
    seed(lp, NOV)
    fake_download(lp, monkeypatch, {
        "XYZ": {D(2026, 11, 3): 100.0, day: 101.0},
        "BTC-USD": {D(2026, 11, 3): 1000.0, day: 1010.0}})
    run_at(lp, monkeypatch, capsys, ny(2026, 11, 4, 23, 0))
    snapshot(lp, day, {"XYZ": 101.5, "BTC": 1012.0}, dt.datetime(2026, 11, 4, 22, 55))
    rows, status, out = value_and_9c(lp, day, capsys)
    assert rows == {"XYZ": (day, "actual"), "BTC": (day, "actual")}
    assert status == "INFO" and "TIMING:" not in out and "OVER 2%" not in out
