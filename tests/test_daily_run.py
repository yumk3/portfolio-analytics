"""daily_run.py: order, stop rules, single backup, locking and the run record. Fabricated data.

Stand-in scripts are passed to run() as a Python function, which only the test
suite can do; the last tests prove a real invocation always runs the real scripts.
"""
import datetime as dt
import hashlib
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

import duckdb
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
CHECKS_OK = "15 checks: 11 PASS, 3 WARN, 1 INFO, 0 FAIL"
CHECKS_FAIL = "15 checks: 10 PASS, 3 WARN, 1 INFO, 1 FAIL"
REBUILD_OK = "  NEW - not previously seen, needs a decision (0)\n     none\nDone."
BUSY = "IO Error: Could not set lock on file: Conflicting lock is held"


def load(name):
    spec = importlib.util.spec_from_file_location(name + "_under_test", SCRIPTS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dr():
    return load("daily_run")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data" / "portfolio.duckdb"
    path.parent.mkdir()
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE returns_summary (account VARCHAR, computed_at TIMESTAMP)")
    con.execute("INSERT INTO returns_summary VALUES ('TOTAL', TIMESTAMP '2026-01-05 23:10:00')")
    con.close()
    return path


class Stand:
    """Stand-in pipeline: every script succeeds unless told otherwise."""

    def __init__(self, **results):
        self.results = {k: list(v) if isinstance(v, list) else [v] for k, v in results.items()}
        self.calls = []

    def __call__(self, script, args, env):
        step = script[:-3]
        self.calls.append((step, list(args), dict(env)))
        queue = self.results.get(step)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if step == "rebuild_holdings":
            return 0, REBUILD_OK
        if step == "load_prices":
            return 0, "positions checked: 6   within 1%: 6   over 1%: 0"
        if step.startswith("validate_"):
            return 0, CHECKS_OK
        return 0, "ok"

    def ran(self):
        return [c[0] for c in self.calls]


ALL = ["ingest_snaptrade", "load_splits", "rebuild_holdings", "load_prices",
       "value_portfolio", "validate_valuation", "compute_returns", "validate_returns"]


def go(dr, db, tmp_path, stand, **kw):
    sleeps = []
    code = dr.run(db, runner=stand, log_dir=tmp_path / "logs", sleep=sleeps.append, **kw)
    return code, sleeps


def job_rows(db):
    con = duckdb.connect(str(db), read_only=True)
    try:
        return {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT item, status, detail FROM job_runs WHERE job_name = 'daily_run'").fetchall()}
    finally:
        con.close()


# ------------------------------------------------------------ order
def test_steps_run_in_order_with_rebuild_before_prices(dr, db, tmp_path):
    stand = Stand()
    code, _ = go(dr, db, tmp_path, stand)
    assert code == 0 and stand.ran() == ALL
    assert ALL.index("rebuild_holdings") < ALL.index("load_prices")
    rows = job_rows(db)
    assert rows["_run"][0] == "ok" and "all steps ok" in rows["_run"][1]


# ------------------------------------------------------------ stop rules
def test_a_failing_data_step_stops_the_run(dr, db, tmp_path):
    stand = Stand(load_splits=(1, "boom"))
    code, _ = go(dr, db, tmp_path, stand)
    assert code == 1 and stand.ran() == ALL[:2]
    rows = job_rows(db)
    assert rows["splits"][0] == "failed" and rows["prices"] == ("skipped", "not run: splits failed")
    assert rows["_run"][0] == "failed"


def test_partial_ingest_stops_the_run(dr, db, tmp_path):
    stand = Stand(ingest_snaptrade=(1, "accounts failed: 1"))
    code, _ = go(dr, db, tmp_path, stand)
    assert code == 1 and stand.ran() == ALL[:1]


def test_new_rebuild_residual_stops_before_prices(dr, db, tmp_path):
    stand = Stand(rebuild_holdings=(0, "  NEW - not previously seen, needs a decision (1)\n"))
    code, _ = go(dr, db, tmp_path, stand)
    assert code == 1 and stand.ran() == ALL[:3]
    assert "1 NEW unexplained residual" in job_rows(db)["rebuild"][1]


def test_unrecognised_rebuild_output_stops_the_run(dr, db, tmp_path):
    stand = Stand(rebuild_holdings=(0, "something else entirely"))
    code, _ = go(dr, db, tmp_path, stand)
    assert code == 1 and stand.ran() == ALL[:3]


def test_prices_over_one_percent_is_recorded_not_a_stop(dr, db, tmp_path):
    stand = Stand(load_prices=(0, "positions checked: 6   within 1%: 5   over 1%: 1"))
    code, _ = go(dr, db, tmp_path, stand)
    assert code == 0 and stand.ran() == ALL
    assert job_rows(db)["prices"] == ("ok", "positions over 1%: 1")


def test_valuation_fail_skips_returns_and_names_the_last_good_state(dr, db, tmp_path):
    stand = Stand(validate_valuation=(1, CHECKS_FAIL))
    code, _ = go(dr, db, tmp_path, stand)
    assert code == 1 and stand.ran() == ALL[:6]
    rows = job_rows(db)
    assert rows["validate_valuation"][0] == "FAIL"
    for step in ("compute_returns", "validate_returns"):
        assert rows[step][0] == "skipped" and "valuation FAIL" in rows[step][1]
    summary = rows["_run"][1]
    backup = next((db.parent / "backups").glob("portfolio_daily_*.duckdb")).name
    assert "valuation tables are from today's failed run" in summary
    assert "returns are from the previous good run (computed 2026-01-05 23:10:00)" in summary
    assert "last good state is the start-of-run backup %s" % backup in summary
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT count(*) FROM returns_summary").fetchone()[0] == 1
    con.close()


def test_returns_fail_is_recorded_and_fails_the_run(dr, db, tmp_path):
    stand = Stand(validate_returns=(1, "7 checks: 5 PASS, 1 WARN, 0 INFO, 1 FAIL"))
    code, _ = go(dr, db, tmp_path, stand)
    assert code == 1 and stand.ran() == ALL
    rows = job_rows(db)
    assert rows["validate_returns"][0] == "FAIL" and rows["_run"][0] == "FAIL"


def test_validator_without_a_summary_fails_closed(dr, db, tmp_path):
    stand = Stand(validate_valuation=(0, "Traceback (most recent call last): ..."))
    code, _ = go(dr, db, tmp_path, stand)
    assert code == 1 and stand.ran() == ALL[:6]
    assert job_rows(db)["validate_valuation"][0] == "failed"


def test_busy_database_is_retried(dr, db, tmp_path):
    stand = Stand(value_portfolio=[(1, BUSY), (0, "ok")])
    code, sleeps = go(dr, db, tmp_path, stand)
    assert code == 0 and stand.ran().count("value_portfolio") == 2
    assert sleeps[:1] == [dr.BUSY_WAIT]


def test_database_busy_on_every_try_stops_the_run(dr, db, tmp_path):
    stand = Stand(value_portfolio=(1, BUSY))
    code, _ = go(dr, db, tmp_path, stand)
    assert code == 1 and stand.ran().count("value_portfolio") == dr.BUSY_TRIES
    assert job_rows(db)["value"] == ("failed", "database busy after %d tries" % dr.BUSY_TRIES)
    assert "validate_valuation" not in stand.ran()


# ------------------------------------------------------------ lock and backup
def test_second_run_exits_without_running_anything(dr, db, tmp_path):
    held = dr.acquire_lock(tmp_path / "logs")
    try:
        stand = Stand()
        code, _ = go(dr, db, tmp_path, stand)
    finally:
        held.close()
    assert code == 3 and stand.calls == []
    assert not (db.parent / "backups").exists()


def test_one_verified_backup_is_taken_and_passed_to_every_step(dr, db, tmp_path):
    stand = Stand()
    go(dr, db, tmp_path, stand)
    backups = list((db.parent / "backups").glob("*.duckdb"))
    assert len(backups) == 1 and backups[0].name.startswith("portfolio_daily_")
    assert {c[2]["PORTFOLIO_RUN_BACKUP"] for c in stand.calls} == {str(backups[0])}
    assert {c[2]["DB_PATH"] for c in stand.calls} == {str(db)}


def test_missing_database_does_not_start(dr, tmp_path):
    stand = Stand()
    code, _ = go(dr, tmp_path / "nope.duckdb", tmp_path, stand)
    assert code == 2 and stand.calls == []


# ------------------------------------------------------------ run_backup helper
@pytest.fixture
def rb():
    return load("run_backup")


def test_run_backup_is_reused_when_valid(rb, tmp_path, monkeypatch):
    path = tmp_path / ("portfolio_daily_%s.duckdb" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    path.write_bytes(b"x")
    monkeypatch.setenv("PORTFOLIO_RUN_BACKUP", str(path))
    assert rb.backup_before_write(tmp_path / "db", tmp_path, "own.duckdb") == (path, True)
    assert not (tmp_path / "own.duckdb").exists()


@pytest.mark.parametrize("case", ["missing", "stale", "elsewhere", "other_name", "unset"])
def test_run_backup_falls_back_to_its_own_copy(rb, tmp_path, monkeypatch, case):
    db = tmp_path / "db.duckdb"
    db.write_bytes(b"database")
    backups = tmp_path / "backups"
    backups.mkdir()
    stamp = dt.datetime.now()
    if case == "stale":
        stamp -= dt.timedelta(hours=25)
    folder = tmp_path if case == "elsewhere" else backups
    name = ("portfolio_%s.duckdb" if case == "other_name" else "portfolio_daily_%s.duckdb")
    path = folder / (name % stamp.strftime("%Y%m%d_%H%M%S"))
    if case != "missing":
        path.write_bytes(b"x")
    if case == "unset":
        monkeypatch.delenv("PORTFOLIO_RUN_BACKUP", raising=False)
    else:
        monkeypatch.setenv("PORTFOLIO_RUN_BACKUP", str(path))
    got, reused = rb.backup_before_write(db, backups, "own.duckdb")
    assert not reused and got == backups / "own.duckdb" and got.read_bytes() == b"database"


# ------------------------------------------------------------ real scripts
def isolated_env(tmp_path, db, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("DB_PATH", "PORTFOLIO_DB", "PORTFOLIO_RUN_BACKUP")}
    env.update(DB_PATH=str(db), RETURNS_BROKER_CSV=str(tmp_path / "none.csv"),
               RETURNS_STATEMENT_CSV=str(tmp_path / "none.csv"), **extra)
    return env


@pytest.fixture
def sample_db(tmp_path):
    db = tmp_path / "data" / "sample.duckdb"
    db.parent.mkdir()
    r = subprocess.run([sys.executable, str(SCRIPTS / "make_sample_data.py"), "--out", str(db)],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    return db


def run_script(name, *args, env):
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], capture_output=True,
                          text=True, env=env, timeout=600)


def test_compute_returns_backs_up_standalone_and_reuses_under_daily_run(sample_db, tmp_path):
    env = isolated_env(tmp_path, sample_db)
    for script, args in (("rebuild_holdings.py", ["--db", str(sample_db), "--through", "2026-09-25"]),
                         ("value_portfolio.py", ["--db", str(sample_db)])):
        assert run_script(script, *args, env=env).returncode == 0
    backups = sample_db.parent / "backups"

    r = run_script("compute_returns.py", env=env)                   # standalone
    assert r.returncode == 0, r.stdout + r.stderr
    own = list(backups.glob("portfolio_before_returns_*.duckdb"))
    assert len(own) == 1 and "taken by daily_run" not in r.stdout

    run_copy = backups / ("portfolio_daily_%s.duckdb" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(sample_db, run_copy)
    r = run_script("compute_returns.py", env=dict(env, PORTFOLIO_RUN_BACKUP=str(run_copy)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "taken by daily_run" in r.stdout
    assert len(list(backups.glob("*.duckdb"))) == 2                 # nothing new


def test_end_to_end_on_the_sample_with_real_scripts(dr, sample_db, tmp_path, monkeypatch):
    for k, v in isolated_env(tmp_path, sample_db).items():
        monkeypatch.setenv(k, v)
    steps = [s for s, _ in dr.STEPS if s not in ("ingest", "splits", "prices")]  # offline
    code = dr.run(sample_db, steps=steps, through="2026-09-25", log_dir=tmp_path / "logs")
    rows = job_rows(sample_db)
    assert code == 0, rows
    assert {s: rows[s][0] for s in steps} == {s: "ok" for s in steps}
    assert rows["validate_valuation"][1].endswith("0 FAIL")
    assert rows["validate_returns"][1].endswith("0 FAIL")
    backups = list((sample_db.parent / "backups").glob("*.duckdb"))
    assert len(backups) == 1 and backups[0].name.startswith("portfolio_daily_")
    log = (tmp_path / "logs").glob("daily_run_*.log")
    assert "REBUILD" in next(log).read_text(encoding="utf-8")       # the real script's output


def test_real_invocation_always_runs_the_real_scripts(tmp_path):
    # Decoys a test-only override might have used; none may change the scripts run.
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    for name in ("ingest_snaptrade.py", "daily_run.py", "run_backup.py"):
        (decoy / name).write_text("raise SystemExit('decoy ran')\n", encoding="utf-8")
    env = dict(os.environ, DAILY_RUN_SCRIPTS=str(decoy), DAILY_RUN_RUNNER="decoy",
               PYTHONPATH=str(decoy), PATH=str(decoy) + os.pathsep + os.environ["PATH"])
    r = subprocess.run([sys.executable, str(SCRIPTS / "daily_run.py"), "--db",
                        str(tmp_path / "x.duckdb"), "--dry-run"], capture_output=True,
                       text=True, env=env, cwd=decoy, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    listed = [l.split()[-1] for l in r.stdout.splitlines() if l.startswith("  ")]
    assert [pathlib.Path(p) for p in listed] == [SCRIPTS / s for s in
                                                 ("ingest_snaptrade.py", "load_splits.py",
                                                  "rebuild_holdings.py", "load_prices.py",
                                                  "value_portfolio.py", "validate_valuation.py",
                                                  "compute_returns.py", "validate_returns.py")]
    assert all(pathlib.Path(p).is_file() for p in listed)


def test_command_line_offers_no_way_to_change_steps_or_scripts():
    r = subprocess.run([sys.executable, str(SCRIPTS / "daily_run.py"), "--help"],
                       capture_output=True, text=True, timeout=120)
    options = set(__import__("re").findall(r"--[a-z-]+", r.stdout))
    assert options == {"--help", "--db", "--dry-run"}


def test_main_calls_run_without_test_hooks(dr, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(dr, "run", lambda db, **kw: seen.update(db=db, kw=kw) or 0)
    monkeypatch.setenv("DAILY_RUN_RUNNER", "decoy")
    assert dr.main(["--db", str(tmp_path / "x.duckdb")]) == 0
    assert seen["kw"] == {}
    assert dr.run_script.__module__ == dr.__name__ and dr.SCRIPTS == SCRIPTS
