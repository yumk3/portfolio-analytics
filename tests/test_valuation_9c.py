"""Check 9c: a price gap over 2% is timing (WARN) only for a carried-forward price
compared with a newer snapshot; every other gap over 2% is a FAIL. Fabricated data.
"""
import datetime as dt
import importlib.util
import pathlib

import duckdb
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SNAP_DAY = dt.date(2026, 1, 6)
EVENING = dt.datetime(2026, 1, 6, 19, 57)


@pytest.fixture
def vv():
    """A fresh validator module, so each test starts with an empty results list."""
    spec = importlib.util.spec_from_file_location("validate_valuation_under_test",
                                                  REPO / "scripts" / "validate_valuation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def db(*positions):
    """positions: (symbol, our_price, price_date, price_status, their_value, loaded_at),
    each 1 unit in one account, so their_value is also their price."""
    con = duckdb.connect()
    con.execute("CREATE TABLE accounts (account_id VARCHAR, account_name VARCHAR)")
    con.execute("INSERT INTO accounts VALUES ('ACCT-0001', 'Example Individual')")
    con.execute("""CREATE TABLE positions (account_id VARCHAR, symbol VARCHAR,
                   as_of_date DATE, quantity DOUBLE, market_value DOUBLE, loaded_at TIMESTAMP)""")
    con.execute("""CREATE TABLE positions_value_daily (date DATE, account_id VARCHAR,
                   symbol VARCHAR, quantity DOUBLE, price DOUBLE, price_date DATE,
                   market_value DOUBLE, price_status VARCHAR)""")
    for sym, price, pdate, status, their, loaded in positions:
        con.execute("INSERT INTO positions VALUES ('ACCT-0001', ?, ?, 1, ?, ?)",
                    [sym, SNAP_DAY, their, loaded])
        con.execute("INSERT INTO positions_value_daily VALUES (?, 'ACCT-0001', ?, 1, ?, ?, ?, ?)",
                    [SNAP_DAY, sym, price, pdate, price, status])
    return con


def run_9c(vv, capsys, con):
    vv.check9c_value(con, SNAP_DAY)
    (num, _, status), = vv.results
    assert num == "9c"
    return status, capsys.readouterr().out


YESTERDAY = SNAP_DAY - dt.timedelta(days=1)


def test_carried_price_with_newer_snapshot_is_timing_warn(vv, capsys):
    status, out = run_9c(vv, capsys, db(("XYZ", 100.0, YESTERDAY, "forward_filled", 103.0, EVENING)))
    assert status == "WARN"
    assert "TIMING: our close 2026-01-05 carried forward; snapshot 2026-01-06 19:57" in out


def test_actual_close_over_limit_fails(vv, capsys):
    status, out = run_9c(vv, capsys, db(("XYZ", 100.0, SNAP_DAY, "actual", 103.0, EVENING)))
    assert status == "FAIL" and "OVER 2%: actual price" in out


def test_actual_close_fails_even_with_a_later_snapshot(vv, capsys):
    # A snapshot loaded after midnight is newer than our close, but our price is
    # that day's actual close, so the gap is a wrong price, not timing.
    after_midnight = dt.datetime(2026, 1, 7, 0, 39)
    status, out = run_9c(vv, capsys, db(("XYZ", 100.0, SNAP_DAY, "actual", 103.0, after_midnight)))
    assert status == "FAIL" and "OVER 2%: actual price, snapshot 2026-01-07 00:39" in out


def test_carried_price_with_older_snapshot_fails(vv, capsys):
    # The snapshot predates our close: the gap cannot be the market moving since then.
    stale = dt.datetime(2026, 1, 5, 9, 30)
    status, out = run_9c(vv, capsys, db(("XYZ", 100.0, YESTERDAY, "forward_filled", 103.0, stale)))
    assert status == "FAIL" and "OVER 2%: forward_filled price, snapshot 2026-01-05 09:30" in out


def test_carried_price_without_snapshot_time_fails(vv, capsys):
    status, out = run_9c(vv, capsys, db(("XYZ", 100.0, YESTERDAY, "forward_filled", 103.0, None)))
    assert status == "FAIL" and "snapshot time unknown" in out


@pytest.mark.parametrize("pdate, pstatus", [(YESTERDAY, "forward_filled"), (SNAP_DAY, "actual")])
def test_gaps_within_limit_stay_info(vv, capsys, pdate, pstatus):
    status, out = run_9c(vv, capsys, db(("XYZ", 100.0, pdate, pstatus, 101.5, EVENING)))
    assert status == "INFO" and "TIMING:" not in out and "OVER" not in out


def test_a_real_gap_fails_even_alongside_a_timing_gap(vv, capsys):
    status, out = run_9c(vv, capsys, db(
        ("XYZ", 100.0, YESTERDAY, "forward_filled", 103.0, EVENING),
        ("ABC", 50.0, SNAP_DAY, "actual", 53.0, EVENING)))
    assert status == "FAIL"
    assert "TIMING: our close 2026-01-05" in out and "OVER 2%: actual price" in out


def test_timing_is_never_reported_as_pass(vv, capsys):
    statuses = set()
    for their in (100.0, 101.0, 103.0, 150.0):
        vv.results.clear()
        status, _ = run_9c(vv, capsys, db(("XYZ", 100.0, YESTERDAY, "forward_filled", their, EVENING)))
        statuses.add(status)
    assert "PASS" not in statuses and statuses == {"INFO", "WARN"}
