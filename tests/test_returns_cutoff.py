"""Tests for the returns as-of date and exclusion rules, with fabricated data.

Unit tests drive compute_returns.returns_cutoff directly; end-to-end tests run
the sample pipeline and validate_returns.py, including tampering detection.
"""
import datetime as dt
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

import duckdb
import pandas as pd
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
STALE_THROUGH = "2026-10-15"      # past the sample's last price, for a fixed stale tail
spec = importlib.util.spec_from_file_location("compute_returns_under_test",
                                              REPO / "scripts" / "compute_returns.py")
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)

START = dt.date(2025, 1, 1)
NO_TXNS = pd.DataFrame({"symbol": pd.Series([], dtype=object)})


def day(i):
    return START + dt.timedelta(days=i)


def pvd_frame(missing, n=30):
    """missing: {symbol: set of day indexes without a usable price, or 'all'}.

    Rows without a price stand for valuation's 'missing' status, i.e. more than
    the 5-day carry-forward has elapsed, so any gap here is a gap over 5 days.
    """
    rows = []
    for i in range(n):
        for sym, gaps in missing.items():
            unpriced = gaps == "all" or i in gaps
            rows.append({"date": day(i), "account_id": "ACCT-0001", "symbol": sym,
                         "price": None if unpriced else 10.0,
                         "market_value": None if unpriced else 100.0})
    return pd.DataFrame(rows)


# ------------------------------------------------------------ unit rules
def test_never_priced_symbol_is_excluded_and_does_not_move_as_of():
    as_of, excluded, back = cr.returns_cutoff(pvd_frame({"AAA": set(), "NEVER": "all"}), NO_TXNS)
    assert as_of == day(29)
    assert excluded == {"NEVER": "never priced while held"}
    assert back == []


def test_traded_but_never_valued_symbol_is_excluded():
    txns = pd.DataFrame({"symbol": ["AAA", "SAMEDAY"]})
    _, excluded, _ = cr.returns_cutoff(pvd_frame({"AAA": set()}), txns)
    assert excluded["SAMEDAY"].startswith("traded but never valued")


def test_mid_history_gap_over_five_days_excludes_the_symbol():
    as_of, excluded, back = cr.returns_cutoff(
        pvd_frame({"AAA": set(), "GAPPY": set(range(10, 17))}), NO_TXNS)
    assert as_of == day(29) and back == []
    assert set(excluded) == {"GAPPY"}
    assert "7 held day(s)" in excluded["GAPPY"] and str(day(10)) in excluded["GAPPY"]


def test_symbol_that_stops_being_priced_holds_the_as_of_back():
    as_of, excluded, back = cr.returns_cutoff(
        pvd_frame({"AAA": set(), "STOP": set(range(20, 30))}), NO_TXNS)
    assert as_of == day(19)
    assert excluded == {}
    assert back == ["STOP"]


def test_excluding_a_symbol_moves_the_as_of_and_the_loop_terminates():
    # BOTH has an old gap and a stale tail: the first pass stops at day 24, the
    # old gap then excludes BOTH, and the second pass moves the date to day 29.
    as_of, excluded, back = cr.returns_cutoff(
        pvd_frame({"AAA": set(), "BOTH": {5, 6} | set(range(25, 30))}), NO_TXNS)
    assert as_of == day(29) and set(excluded) == {"BOTH"} and back == []


def test_cascading_exclusions_terminate():
    # X: old gap + tail; Y: a gap that only falls before the as-of once X is gone.
    as_of, excluded, back = cr.returns_cutoff(
        pvd_frame({"AAA": set(), "X": {3, 27, 28, 29}, "Y": {26}}), NO_TXNS)
    assert as_of == day(29) and set(excluded) == {"X", "Y"} and back == []


def test_no_complete_date_returns_none():
    as_of, _, back = cr.returns_cutoff(pvd_frame({"AAA": {0}, "BBB": set(range(1, 30))}), NO_TXNS)
    assert as_of is None and back == ["AAA", "BBB"]


# ------------------------------------------------------------ end to end
def run(script, *args, env):
    r = subprocess.run([sys.executable, str(REPO / "scripts" / script), *args],
                       capture_output=True, text=True, env=env, timeout=600)
    return r


@pytest.fixture(scope="module")
def sample_db(tmp_path_factory):
    base = tmp_path_factory.mktemp("sample")
    db = base / "sample.duckdb"
    env = isolated_env(base, db)
    for script, args in (("make_sample_data.py", ["--out", str(db)]),
                         ("rebuild_holdings.py", ["--db", str(db), "--through", STALE_THROUGH]),
                         ("value_portfolio.py", ["--db", str(db)])):
        r = run(script, *args, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
    return db


def isolated_env(folder, db):
    env = {k: v for k, v in os.environ.items() if k not in ("DB_PATH", "PORTFOLIO_DB")}
    env["DB_PATH"] = str(db)
    env["RETURNS_BROKER_CSV"] = str(folder / "no_broker_checkpoints.csv")
    env["RETURNS_STATEMENT_CSV"] = str(folder / "no_statement_checkpoints.csv")
    return env


def copy_db(sample_db, tmp_path):
    db = tmp_path / "sample.duckdb"
    shutil.copy2(sample_db, db)
    return db, isolated_env(tmp_path, db)


def test_stale_tail_returns_end_at_as_of_and_validator_passes(sample_db, tmp_path):
    db, env = copy_db(sample_db, tmp_path)
    r = run("compute_returns.py", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "valuation not current since 2026-10-01; no usable price for: " \
           "AAPL, JNJ, MSFT, SPY, XYZ" in r.stdout
    con = duckdb.connect(str(db), read_only=True)
    end = con.execute("SELECT max(date) FROM returns_daily WHERE account = 'TOTAL'").fetchone()[0]
    con.close()
    assert end == dt.date(2026, 9, 30)
    v = run("validate_returns.py", env=env)
    assert v.returncode == 0, v.stdout
    assert "[PASS] 0 " in v.stdout and "[PASS] 1c" in v.stdout


def test_tampered_daily_return_makes_validator_fail(sample_db, tmp_path):
    db, env = copy_db(sample_db, tmp_path)
    assert run("compute_returns.py", env=env).returncode == 0
    con = duckdb.connect(str(db))
    con.execute("""UPDATE returns_daily SET daily_return = daily_return + 0.01
                   WHERE account = 'TOTAL' AND date = DATE '2025-06-02'""")
    con.close()
    v = run("validate_returns.py", env=env)
    assert v.returncode == 1 and "[FAIL] 1c" in v.stdout


def test_tampered_exclusion_list_makes_validator_fail(sample_db, tmp_path):
    db, env = copy_db(sample_db, tmp_path)
    assert run("compute_returns.py", env=env).returncode == 0
    con = duckdb.connect(str(db))
    con.execute("INSERT INTO returns_exclusions (symbol, reason) VALUES ('FAKE', 'tampered')")
    con.close()
    v = run("validate_returns.py", env=env)
    assert v.returncode == 1 and "[FAIL] 0 " in v.stdout


def test_excluded_symbols_are_reported_with_reason_and_last_value(sample_db, tmp_path):
    db, env = copy_db(sample_db, tmp_path)
    con = duckdb.connect(str(db))
    # OLDCO: held for a month but never priced. JNJ: a 3-week price gap mid-history.
    con.execute("""INSERT INTO transactions (transaction_id, account_id, symbol, txn_type,
                   quantity, price, amount, fee, currency, trade_date, settle_date) VALUES
                   ('TEST-1', 'ACCT-0001', 'OLDCO', 'BUY', 5, 20, -100, 0, 'USD',
                    DATE '2025-01-06', DATE '2025-01-06'),
                   ('TEST-2', 'ACCT-0001', 'OLDCO', 'SELL', -5, 22, 110, 0, 'USD',
                    DATE '2025-02-03', DATE '2025-02-03')""")
    con.execute("""DELETE FROM prices WHERE symbol = 'JNJ'
                   AND price_date BETWEEN DATE '2025-05-01' AND DATE '2025-05-21'""")
    con.close()
    assert run("rebuild_holdings.py", "--db", str(db), "--through", STALE_THROUGH,
               env=env).returncode == 0
    assert run("value_portfolio.py", "--db", str(db), env=env).returncode == 0
    r = run("compute_returns.py", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OLDCO    never priced while held; never priced" in r.stdout
    assert "JNJ      no usable price on" in r.stdout and "last priced 2026-09-30" in r.stdout

    con = duckdb.connect(str(db), read_only=True)
    rows = {s: (reason, d, v) for s, reason, d, v in con.execute(
        "SELECT symbol, reason, last_priced_date, last_priced_value FROM returns_exclusions"
    ).fetchall()}
    con.close()
    assert set(rows) == {"JNJ", "OLDCO"}
    assert rows["OLDCO"] == ("never priced while held", None, None)
    assert rows["JNJ"][1] == dt.date(2026, 9, 30) and rows["JNJ"][2] > 0
    con = duckdb.connect(str(db), read_only=True)
    notes = con.execute("SELECT notes FROM returns_summary WHERE account = 'TOTAL' "
                        "AND period = 'SI'").fetchone()[0]
    con.close()
    assert "excluded but still held (unrealized gain not in all-in): JNJ" in notes

    v = run("validate_returns.py", env=env)
    assert v.returncode == 0, v.stdout
    assert "[PASS] 0 " in v.stdout
