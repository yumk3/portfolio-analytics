"""Smoke test: build the fabricated sample database, rebuild holdings and value it.

Runs fully offline in a temporary folder; the committed sample_data/ is not modified.
"""
import os
import pathlib
import subprocess
import sys

import duckdb

REPO = pathlib.Path(__file__).resolve().parents[1]


def run(script, *args, env):
    r = subprocess.run([sys.executable, str(REPO / "scripts" / script), *args],
                       capture_output=True, text=True, env=env, timeout=600)
    assert r.returncode == 0, f"{script} failed:\n{r.stdout}\n{r.stderr}"
    return r.stdout


def test_sample_pipeline_builds_values_and_validates(tmp_path):
    db = tmp_path / "sample.duckdb"
    env = {k: v for k, v in os.environ.items() if k not in ("DB_PATH", "PORTFOLIO_DB")}

    run("make_sample_data.py", "--out", str(db), env=env)
    rebuild_out = run("rebuild_holdings.py", "--db", str(db), "--through", "2026-09-25", env=env)
    run("value_portfolio.py", "--db", str(db), env=env)
    check = run("validate_valuation.py", env=dict(env, DB_PATH=str(db)))

    assert "mismatch: 0" in rebuild_out
    assert "0 FAIL" in check
    # Check 10 looked for its file in the test's empty inputs folder, not inputs/.
    expected = os.path.join(os.environ["INPUTS_DIR"], "statement_checkpoints.csv")
    assert f"{expected} not found" in check
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = con.execute("SELECT count(*) FROM positions_value_daily").fetchone()[0]
        total = con.execute("""SELECT market_value FROM portfolio_value_daily
                               WHERE account_id = 'ALL' AND market_value IS NOT NULL
                               ORDER BY date DESC LIMIT 1""").fetchone()[0]
    finally:
        con.close()
    assert rows > 0
    assert total > 0
