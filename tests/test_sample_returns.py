"""End-to-end returns on the fabricated sample, pinned to its fixed as-of date.

The sample's last price is 2026-09-25, so rebuilding --through that date makes
every result reproducible regardless of when the tests run.
"""
import datetime as dt
import os
import pathlib
import subprocess
import sys

import duckdb
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SAMPLE_AS_OF = "2026-09-25"


def run(script, *args, env):
    return subprocess.run([sys.executable, str(REPO / "scripts" / script), *args],
                          capture_output=True, text=True, env=env, timeout=600)


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    folder = tmp_path_factory.mktemp("sample_returns")
    db = folder / "sample.duckdb"
    env = {k: v for k, v in os.environ.items() if k not in ("DB_PATH", "PORTFOLIO_DB")}
    env.update(DB_PATH=str(db),
               RETURNS_BROKER_CSV=str(folder / "none.csv"),
               RETURNS_STATEMENT_CSV=str(folder / "none.csv"))
    out = {}
    for name, script, args in (
            ("make", "make_sample_data.py", ["--out", str(db)]),
            ("rebuild", "rebuild_holdings.py", ["--db", str(db), "--through", SAMPLE_AS_OF]),
            ("value", "value_portfolio.py", ["--db", str(db)]),
            ("compute", "compute_returns.py", []),
            ("validate", "validate_returns.py", [])):
        out[name] = run(script, *args, env=env)
    return db, env, out


def test_every_step_succeeds(pipeline):
    _, _, out = pipeline
    for name, r in out.items():
        assert r.returncode == 0, f"{name}:\n{r.stdout}\n{r.stderr}"


def test_rebuild_and_returns_stop_at_the_sample_as_of(pipeline):
    db, _, out = pipeline
    assert f"date range          : 2020-01-02 .. {SAMPLE_AS_OF}" in out["rebuild"].stdout
    assert f"Returns as of {SAMPLE_AS_OF}" in out["compute"].stdout
    assert "not current" not in out["compute"].stdout
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("SELECT max(as_of_date) FROM holdings_daily").fetchone()[0] \
            == dt.date.fromisoformat(SAMPLE_AS_OF)
        assert con.execute("SELECT count(*) FROM positions_value_daily "
                           "WHERE price_status = 'missing'").fetchone()[0] == 0
    finally:
        con.close()


def test_results_are_reproducible(pipeline):
    db, _, _ = pipeline
    con = duckdb.connect(str(db), read_only=True)
    try:
        si = dict((a, (twr, gain)) for a, twr, gain in con.execute(
            "SELECT account, twr, dollar_gain FROM returns_summary WHERE period = 'SI'"
        ).fetchall())
        periods = {r[0] for r in con.execute("SELECT DISTINCT period FROM returns_summary").fetchall()}
        excluded = con.execute("SELECT count(*) FROM returns_exclusions").fetchone()[0]
    finally:
        con.close()
    assert periods == {"1D", "MTD", "QTD", "YTD", "1Y", "SI"}
    assert set(si) == {"Example Individual", "TOTAL"}
    twr, gain = si["TOTAL"]
    assert twr == pytest.approx(1.7980909367, abs=1e-9)
    assert gain == pytest.approx(3099.1584, abs=1e-3)
    assert excluded == 0


def test_validator_passes_every_internal_check(pipeline):
    _, _, out = pipeline
    v = out["validate"].stdout
    for check in ("0 ", "1a", "1b", "1c", "2 "):
        assert f"[PASS] {check}" in v, v
    assert "[INFO] 3a" in v and "[INFO] 3b" in v          # no checkpoint files given
    assert "0 FAIL" in v


def test_through_rejects_future_and_malformed_dates(pipeline):
    db, env, _ = pipeline
    future = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    r = run("rebuild_holdings.py", "--db", str(db), "--through", future, env=env)
    assert r.returncode == 2 and f"--through {future} is in the future" in r.stdout
    r = run("rebuild_holdings.py", "--db", str(db), "--through", "25/09/2026", env=env)
    assert r.returncode == 2 and "invalid fromisoformat value" in r.stderr
