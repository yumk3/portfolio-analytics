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


def test_sample_pipeline_builds_and_values(tmp_path):
    db = tmp_path / "sample.duckdb"
    env = {k: v for k, v in os.environ.items() if k not in ("DB_PATH", "PORTFOLIO_DB")}

    run("make_sample_data.py", "--out", str(db), env=env)
    rebuild_out = run("rebuild_holdings.py", "--db", str(db), env=env)
    run("value_portfolio.py", "--db", str(db), env=env)

    assert "mismatch: 0" in rebuild_out
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
