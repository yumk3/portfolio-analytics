"""Run the nightly pipeline in order, each script as its own process, and record the run.

Order: ingest, splits, rebuild, prices, value, validate valuation, compute returns,
validate returns. Prices come after the rebuild because their date windows are read
from holdings_daily. One database backup is taken at the start; ingest and
compute_returns reuse it rather than taking their own (see run_backup.py).

Stop rules: any failing step stops the run, including a partial ingest (exit 1) and a
rebuild that reports a NEW unexplained residual. A valuation FAIL skips both returns
steps, so the returns tables stay as the previous good run left them. A database
locked by another job is retried. Validators are run, never imported.

Exit codes: 0 ok, 1 a step failed or a validator reported FAIL, 2 could not start,
3 already running. Usage: python scripts\\daily_run.py [--db PATH] [--dry-run]
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import uuid

from dotenv import load_dotenv

SCRIPTS = pathlib.Path(__file__).resolve().parent
REPO = SCRIPTS.parent
LOG_DIR = REPO / "logs"
JOB_NAME = "daily_run"
RUN_BACKUP_ENV = "PORTFOLIO_RUN_BACKUP"      # read by run_backup.py in the child scripts
STEP_TIMEOUT = 45 * 60                       # seconds
BUSY_TRIES = 3
BUSY_WAIT = 60                               # seconds between tries on a locked database
BUSY_RE = re.compile(r"Could not set lock|Conflicting lock|database is in use", re.I)
CHECKS_RE = re.compile(r"(\d+) checks: (\d+) PASS, (\d+) WARN, (\d+) INFO, (\d+) FAIL")
REBUILD_NEW_RE = re.compile(r"NEW - not previously seen, needs a decision \((\d+)\)")
PRICES_OVER_RE = re.compile(r"over 1%: (\d+)")

# (step, script). The scripts always come from SCRIPTS; nothing at run time
# (option, environment, config) can point a step at another file.
STEPS = (
    ("ingest", "ingest_snaptrade.py"),
    ("splits", "load_splits.py"),
    ("rebuild", "rebuild_holdings.py"),
    ("prices", "load_prices.py"),
    ("value", "value_portfolio.py"),
    ("validate_valuation", "validate_valuation.py"),
    ("compute_returns", "compute_returns.py"),
    ("validate_returns", "validate_returns.py"),
)
RETURNS_STEPS = ("compute_returns", "validate_returns")
TAKES_DB_ARG = ("rebuild", "value")

JOB_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS job_runs (
    job_name    VARCHAR NOT NULL,
    run_id      VARCHAR NOT NULL,
    started_at  TIMESTAMP,
    finished_at TIMESTAMP,
    item        VARCHAR,
    status      VARCHAR,
    detail      VARCHAR,
    error       VARCHAR
)"""


def default_db():
    load_dotenv(REPO / ".env")
    db = pathlib.Path(os.getenv("DB_PATH") or "data/portfolio.duckdb")
    return db if db.is_absolute() else REPO / db


def script_path(script):
    return SCRIPTS / script


def run_script(script, args, env):
    """Run one pipeline script; return (exit code, combined output)."""
    try:
        r = subprocess.run([sys.executable, str(script_path(script)), *args], cwd=REPO,
                           env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=STEP_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
        return 124, out + "\nTIMED OUT after %d s" % STEP_TIMEOUT
    return r.returncode, r.stdout + r.stderr


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def take_backup(db, started):
    """Copy the database to backups/; return the path, or raise RuntimeError."""
    folder = db.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("portfolio_daily_%s.duckdb" % started.strftime("%Y%m%d_%H%M%S"))
    for _ in range(BUSY_TRIES):
        shutil.copy2(db, path)
        if sha256(path) == sha256(db):
            return path
        time.sleep(1)              # the database changed during the copy; try again
    path.unlink()
    raise RuntimeError("database kept changing while being backed up")


def acquire_lock(log_dir):
    """Return an open, locked file handle, or None if another run holds the lock."""
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = open(log_dir / "daily_run.lock", "a+b")
    fh.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def previous_returns(db):
    """Return when the returns tables were last computed, or None."""
    import duckdb
    try:
        con = duckdb.connect(str(db), read_only=True)
    except duckdb.Error:
        return None
    try:
        return con.execute("SELECT max(computed_at) FROM returns_summary").fetchone()[0]
    except duckdb.Error:
        return None
    finally:
        con.close()


def assess(step, code, out):
    """Return (status, detail, stop) for a finished step."""
    if step in ("validate_valuation", "validate_returns"):
        m = CHECKS_RE.search(out)
        if not m:
            return "failed", "no check summary in the output (exit %d)" % code, True
        n, p, w, i, f = m.groups()
        detail = "%s checks: %s PASS, %s WARN, %s INFO, %s FAIL" % (n, p, w, i, f)
        if int(f) or code:
            return "FAIL", detail, False
        return "ok", detail, False
    if code:
        return "failed", "exit code %d" % code, True
    if step == "rebuild":
        m = REBUILD_NEW_RE.search(out)
        if not m:
            return "failed", "rebuild output not recognised (no NEW exceptions line)", True
        if int(m.group(1)):
            return ("failed", "%s NEW unexplained residual(s) need a decision; "
                    "see the rebuild output in the log" % m.group(1), True)
        return "ok", "0 new exceptions", False
    if step == "prices":
        m = PRICES_OVER_RE.search(out)
        return "ok", "positions over 1%%: %s" % (m.group(1) if m else "n/a"), False
    return "ok", "", False


def write_job_runs(db, rows, log_dir, sleep):
    """Append rows to job_runs; park them in a file if the database stays locked."""
    import duckdb
    pending = log_dir / "daily_run_pending"
    backlog = sorted(pending.glob("*.json")) if pending.exists() else []
    all_rows = list(rows)
    for b in backlog:
        all_rows += [tuple(r) for r in json.loads(b.read_text(encoding="utf-8"))]
    for attempt in range(BUSY_TRIES):
        try:
            con = duckdb.connect(str(db))
        except duckdb.IOException:
            if attempt + 1 < BUSY_TRIES:
                sleep(BUSY_WAIT)
                continue
            break
        try:
            con.execute(JOB_RUNS_DDL)
            con.executemany("INSERT INTO job_runs (job_name, run_id, started_at, finished_at, "
                            "item, status, detail, error) VALUES (?,?,?,?,?,?,?,?)", all_rows)
        finally:
            con.close()
        for b in backlog:
            b.unlink()
        return None
    pending.mkdir(parents=True, exist_ok=True)
    (pending / ("%s.json" % rows[0][1])).write_text(json.dumps(rows, default=str), encoding="utf-8")
    return "job_runs not written (database locked); saved for the next run"


def run(db, *, steps=None, through=None, runner=None, log_dir=None, sleep=time.sleep):
    """Run the pipeline against db and return the exit code.

    steps, through and runner exist for the test suite only: main() never
    passes them, so a real run always executes every step's real script.
    """
    db = pathlib.Path(db)
    log_dir = pathlib.Path(log_dir or LOG_DIR)
    runner = runner or run_script
    names = [s for s, _ in STEPS] if steps is None else list(steps)
    started = datetime.datetime.now()
    run_id = "%s-%s" % (started.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:6])

    if not db.exists():
        print("ERROR: database not found at %s" % db)
        return 2
    lock = acquire_lock(log_dir)
    if lock is None:
        print("Another daily run is in progress. Exiting without changes.")
        return 3
    try:
        try:
            backup = take_backup(db, started)
        except (OSError, RuntimeError) as e:
            print("ERROR: could not back up the database (%s). Nothing run." % e)
            return 2
        print("Backup: %s" % backup.name)
        returns_before = previous_returns(db)

        env = {k: v for k, v in os.environ.items() if k != "PORTFOLIO_DB"}
        env.update(DB_PATH=str(db), PYTHONIOENCODING="utf-8")
        env[RUN_BACKUP_ENV] = str(backup)
        log_file = log_dir / ("daily_run_%s.log" % started.strftime("%Y-%m-%d"))

        rows, stopped, valuation_failed, any_fail = [], None, False, False
        for step, script in STEPS:
            if step not in names:
                continue
            t0 = datetime.datetime.now()
            if stopped:
                rows.append((JOB_NAME, run_id, t0, t0, step, "skipped",
                             "not run: %s" % stopped, ""))
                continue
            if valuation_failed and step in RETURNS_STEPS:
                rows.append((JOB_NAME, run_id, t0, t0, step, "skipped",
                             "not run: valuation FAIL; returns tables kept from the "
                             "previous good run", ""))
                continue
            args = (["--db", str(db)] if step in TAKES_DB_ARG else []) + \
                   (["--through", through] if step == "rebuild" and through else [])
            for attempt in range(BUSY_TRIES):
                code, out = runner(script, args, env)
                busy = code != 0 and BUSY_RE.search(out)
                if not busy or attempt + 1 == BUSY_TRIES:
                    break
                sleep(BUSY_WAIT)
            with open(log_file, "a", encoding="utf-8") as lf:
                lf.write("=" * 78 + "\n%s  %s (exit %d)\n" % (t0, step, code) + out + "\n")
            if busy:
                status, detail, stop = ("failed", "database busy after %d tries" % BUSY_TRIES,
                                        True)
            else:
                status, detail, stop = assess(step, code, out)
            rows.append((JOB_NAME, run_id, t0, datetime.datetime.now(), step, status,
                         detail, ""))
            print("  %-19s %-7s %s" % (step, status, detail))
            if status == "FAIL":
                any_fail = True
                valuation_failed = valuation_failed or step == "validate_valuation"
            if stop:
                stopped = "%s %s" % (step, status)

        finished = datetime.datetime.now()
        if stopped:
            summary = "stopped at %s" % stopped
        elif valuation_failed:
            summary = ("valuation FAIL: valuation tables are from today's failed run; returns "
                       "are from the previous good run (computed %s); last good state is the "
                       "start-of-run backup %s" % (returns_before or "never", backup.name))
        elif any_fail:
            summary = "returns validation FAIL"
        else:
            summary = "all steps ok"
        status = "failed" if stopped else "FAIL" if any_fail else "ok"
        rows.append((JOB_NAME, run_id, started, finished, "_run", status,
                     "%s; backup %s" % (summary, backup.name), ""))
        warn = write_job_runs(db, rows, log_dir, sleep)
        print("Result: %s - %s" % (status, summary))
        if warn:
            print("WARNING: %s" % warn)
        print("Log: %s" % log_file)
        return 0 if status == "ok" else 1
    finally:
        lock.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the nightly pipeline.")
    ap.add_argument("--db", help="DuckDB file (default: DB_PATH from .env)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the steps and the scripts they run; change nothing")
    args = ap.parse_args(argv)
    db = pathlib.Path(args.db).resolve() if args.db else default_db()
    if args.dry_run:
        print("database: %s" % db)
        for step, script in STEPS:
            print("  %-19s %s" % (step, script_path(script)))
        return 0
    return run(db)


if __name__ == "__main__":
    sys.exit(main())
