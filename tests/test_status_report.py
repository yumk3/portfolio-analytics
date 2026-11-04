"""status_report.py: the combined PORTFOLIO_STATUS.txt, built from fabricated job_runs rows."""
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys

import duckdb
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "status_report.py"
DAY = dt.date(2026, 1, 6)
BACKUP = "portfolio_daily_20260105_230000.duckdb"
VAL_OK = ("15 checks: 11 PASS, 3 WARN, 1 INFO, 0 FAIL | WARN 1 Opening balances"
          " | WARN 5 Crypto sleeve | WARN 8 Price coverage")
VAL_FAIL = "15 checks: 10 PASS, 3 WARN, 1 INFO, 1 FAIL | WARN 1 Opening balances | FAIL 9c Prices"
RET_OK = "7 checks: 5 PASS, 1 WARN, 1 INFO, 0 FAIL | WARN 3b Statement checkpoint"
GOOD_FUNDS = [("AAA", "captured", "AAA_generic.csv", ""), ("BBB", "unchanged", "", ""),
              ("MMM", "manual", "", "")]


def at(day, hhmm):
    return dt.datetime.combine(day, dt.time.fromisoformat(hhmm))


def row(job, run_id, start, item, status, detail="", error="", minutes=1):
    return (job, run_id, start, start + dt.timedelta(minutes=minutes), item, status, detail, error)


def archiver_run(start, funds=GOOD_FUNDS, status=None, error=""):
    rid = "arc-%s" % start.strftime("%Y%m%dT%H%M")
    rows = [row("etf_archiver", rid, start, t, s, d, e) for t, s, d, e in funds]
    bad = any(f[1] in ("failed", "blocked") for f in funds)
    rows.append(row("etf_archiver", rid, start, "_run",
                    status or ("problems" if bad else "ok"), "", error))
    return rows


def nightly(start, overrides=None, run_status="ok", summary="all steps ok", backup=BACKUP):
    steps = {"ingest": ("ok", ""), "splits": ("ok", ""), "rebuild": ("ok", "0 new exceptions"),
             "prices": ("ok", "positions over 1%: 0"), "value": ("ok", ""),
             "validate_valuation": ("ok", VAL_OK), "compute_returns": ("ok", ""),
             "validate_returns": ("ok", RET_OK)}
    steps.update(overrides or {})
    rid = "dr-%s" % start.strftime("%Y%m%dT%H%M")
    rows = [row("daily_run", rid, start + dt.timedelta(minutes=i), name, s, d)
            for i, (name, (s, d)) in enumerate(steps.items())]
    rows.append(row("daily_run", rid, start, "_run", run_status,
                    "%s; backup %s" % (summary, backup), minutes=12))
    return rows


def returns_run(finish, asof, warnings=""):
    rid = "cr-%s" % finish.strftime("%Y%m%dT%H%M")
    return [row("compute_returns", rid, finish - dt.timedelta(minutes=1), "_run",
                "warnings" if warnings else "ok",
                "as of %s; excluded ZZZ; backup %s, taken by daily_run" % (asof, BACKUP),
                warnings)]


def good_day():
    """A normal day: last night's run, then all three archiver runs on time."""
    prev = DAY - dt.timedelta(days=1)
    return (nightly(at(prev, "23:00")) + returns_run(at(prev, "23:08"), prev)
            + archiver_run(at(DAY, "07:15")) + archiver_run(at(DAY, "13:00"))
            + archiver_run(at(DAY, "18:00")))


JOB_RUNS_DDL = """CREATE TABLE job_runs (job_name VARCHAR NOT NULL, run_id VARCHAR NOT NULL,
    started_at TIMESTAMP, finished_at TIMESTAMP, item VARCHAR, status VARCHAR,
    detail VARCHAR, error VARCHAR)"""


def make_db(tmp_path, rows):
    db = tmp_path / "data" / "portfolio.duckdb"
    db.parent.mkdir(exist_ok=True)
    con = duckdb.connect(str(db))
    con.execute(JOB_RUNS_DDL)
    if rows:
        con.executemany("INSERT INTO job_runs VALUES (?,?,?,?,?,?,?,?)", rows)
    con.close()
    return db


@pytest.fixture
def sr():
    spec = importlib.util.spec_from_file_location("status_report_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def report(sr, tmp_path, rows, now, warn_gb="2", **kw):
    db = make_db(tmp_path, rows)
    return sr.report(db, tmp_path / "logs", now, by="test", warn_gb_raw=warn_gb, **kw)


def attention(text):
    m = re.search(r"^OVERALL: (.*)$", text, re.M)
    assert m, text
    return m.group(1)


def items(text):
    """The attention items listed under the OVERALL line."""
    block = text.split("OVERALL: ", 1)[1].split("\n\n", 1)[0].splitlines()[1:]
    return [l.strip()[2:] for l in block if l.strip().startswith("- ")]


# ------------------------------------------------------------ normal day
def test_normal_day_is_ok_and_shows_each_part(sr, tmp_path):
    text = report(sr, tmp_path, good_day(), at(DAY, "23:30"))
    assert attention(text).startswith("OK"), text
    for slot in ("07:15  ran 07:15  ok", "13:00  ran 13:00  ok", "18:00  ran 18:00  ok"):
        assert slot in text
    assert re.search(r"AAA\s+07:15 captured", text) and "MMM" in text
    assert "NIGHTLY PIPELINE - last run 2026-01-05 23:00" in text
    assert "15 checks: 11 PASS, 3 WARN, 1 INFO, 0 FAIL" in text
    assert "returns as of 2026-01-05" in text and "excluded ZZZ" in text
    assert "ETF_ARCHIVER_STATUS.txt" in text and "BACKUPS" in text


# ------------------------------------------------------------ archiver schedule
def test_a_passed_slot_with_no_run_is_missed(sr, tmp_path):
    rows = good_day()[:-8]                       # drop the 13:00 and 18:00 runs
    text = report(sr, tmp_path, rows, at(DAY, "13:40"))
    assert "13:00  MISSED" in text and "18:00  not yet due" in text
    assert attention(text).startswith("ATTENTION") and "13:00 run MISSED" in text


def test_a_slot_is_not_missed_until_its_grace_period_ends(sr, tmp_path):
    rows = good_day()[:-8]
    text = report(sr, tmp_path, rows, at(DAY, "13:20"))
    assert "13:00  not yet due" in text and "MISSED" not in text
    assert attention(text).startswith("OK")


def test_a_late_catch_up_run_counts_as_ran_late(sr, tmp_path):
    prev = DAY - dt.timedelta(days=1)
    rows = (nightly(at(prev, "23:00")) + returns_run(at(prev, "23:08"), prev)
            + archiver_run(at(DAY, "09:02")))
    text = report(sr, tmp_path, rows, at(DAY, "10:00"))
    assert "07:15  ran late 09:02  ok" in text and "MISSED" not in text


def test_a_fund_that_failed_then_recovered_is_not_flagged(sr, tmp_path):
    prev = DAY - dt.timedelta(days=1)
    rows = (nightly(at(prev, "23:00")) + returns_run(at(prev, "23:08"), prev)
            + archiver_run(at(DAY, "07:15"), [("AAA", "failed", "", "HTTP 503"),
                                              ("BBB", "unchanged", "", "")])
            + archiver_run(at(DAY, "13:00"), [("AAA", "captured", "AAA_generic.csv", ""),
                                              ("BBB", "blocked", "", "HTML page")]))
    text = report(sr, tmp_path, rows, at(DAY, "14:00"))
    assert re.search(r"AAA .*failed.*HTTP 503.*captured.*recovered", text)
    assert items(text) == ["ETF archiver: BBB blocked at 13:00: HTML page"]


def test_an_archiver_run_that_could_not_start_is_flagged(sr, tmp_path):
    rows = good_day()[:-12] + archiver_run(at(DAY, "07:15"), [], status="not_started",
                                           error="Missing Python libraries: example")
    text = report(sr, tmp_path, rows, at(DAY, "08:00"))
    assert "could not start at 07:15: Missing Python libraries: example" in text
    assert attention(text).startswith("ATTENTION")


# ------------------------------------------------------------ nightly pipeline
def test_valuation_fail_names_tables_returns_and_backup(sr, tmp_path):
    prev = DAY - dt.timedelta(days=1)
    night = at(DAY, "23:00")
    rows = (returns_run(at(prev, "23:08"), prev)
            + nightly(night, {"validate_valuation": ("FAIL", VAL_FAIL),
                              "compute_returns": ("skipped", "not run: valuation FAIL"),
                              "validate_returns": ("skipped", "not run: valuation FAIL")},
                      run_status="FAIL", summary="valuation FAIL: ...",
                      backup="portfolio_daily_20260106_230000.duckdb"))
    text = report(sr, tmp_path, rows, at(DAY, "23:30"))
    assert "Nightly pipeline: valuation FAIL: ..." in items(text)
    text = text.lower()
    assert "valuation tables are from today's failed run" in text
    assert "returns are from the previous good run (computed 2026-01-05 23:08)" in text
    assert "last good state: the start-of-run backup portfolio_daily_20260106_230000.duckdb" in text
    assert "fail 9c prices" in text
    assert "returns as of 2026-01-05" in text and "from the previous good run" in text


def test_a_stopped_run_names_the_step_and_shows_skipped_steps(sr, tmp_path):
    skipped = ("skipped", "not run: rebuild failed")
    rows = nightly(at(DAY - dt.timedelta(days=1), "23:00"),
                   {"rebuild": ("failed", "1 NEW unexplained residual(s) need a decision"),
                    "prices": skipped, "value": skipped, "validate_valuation": skipped,
                    "compute_returns": skipped, "validate_returns": skipped},
                   run_status="failed", summary="stopped at rebuild failed")
    text = report(sr, tmp_path, rows, at(DAY, "08:00"))
    assert "Nightly pipeline: stopped at rebuild failed" in items(text)
    assert re.search(r"prices\s+skipped\s+not run: rebuild failed", text)


def test_no_nightly_run_is_flagged(sr, tmp_path):
    text = report(sr, tmp_path, archiver_run(at(DAY, "07:15")), at(DAY, "08:00"))
    assert "no nightly run recorded yet" in text and attention(text).startswith("ATTENTION")


def test_a_nightly_run_older_than_26_hours_is_flagged(sr, tmp_path):
    rows = nightly(at(DAY - dt.timedelta(days=2), "23:00")) + archiver_run(at(DAY, "07:15"))
    text = report(sr, tmp_path, rows, at(DAY, "08:00"))
    assert "no nightly run since 2026-01-04 23:00" in text


def test_every_warning_is_listed_with_its_check_number(sr, tmp_path):
    prev = DAY - dt.timedelta(days=1)
    rows = (nightly(at(prev, "23:00")) + archiver_run(at(DAY, "07:15"))
            + returns_run(at(prev, "23:08"), prev, warnings="opening balance has no value"))
    text = report(sr, tmp_path, rows, at(DAY, "08:00"))
    for item in ("WARN 1 Opening balances", "WARN 5 Crypto sleeve", "WARN 8 Price coverage",
                 "WARN 3b Statement checkpoint", "opening balance has no value"):
        assert item in text
    assert attention(text).startswith("OK") and "5 warning(s)" in text
    assert "expected" not in text.lower()


# ------------------------------------------------------------ backups
def backups(tmp_path, size):
    folder = tmp_path / "data" / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "portfolio_daily_20260105_230000.duckdb").write_bytes(b"x" * size)


def test_backups_above_the_limit_are_flagged(sr, tmp_path):
    backups(tmp_path, 3000)
    text = report(sr, tmp_path, good_day(), at(DAY, "23:30"), warn_gb="0.000001")
    assert "1 file(s)" in text and attention(text).startswith("ATTENTION")
    assert "above the 1e-06 GB warning level" in text


def test_backups_below_the_limit_are_not_flagged(sr, tmp_path):
    backups(tmp_path, 3000)
    text = report(sr, tmp_path, good_day(), at(DAY, "23:30"), warn_gb="100")
    assert "1 file(s)" in text and attention(text).startswith("OK")


@pytest.mark.parametrize("raw", ["abc", "-1", "0"])
def test_a_bad_limit_falls_back_to_2_gb_and_says_so(sr, tmp_path, raw):
    text = report(sr, tmp_path, good_day(), at(DAY, "23:30"), warn_gb=raw)
    assert "BACKUP_WARN_GB=%r is not a positive number; using 2 GB" % raw in text
    assert "warning above 2 GB" in text


def test_an_unset_limit_is_2_gb(sr, tmp_path):
    text = report(sr, tmp_path, good_day(), at(DAY, "23:30"), warn_gb=None)
    assert "warning above 2 GB" in text and "not a positive number" not in text


# ------------------------------------------------------------ parked run records
def test_parked_rows_are_shown_once_and_flagged(sr, tmp_path):
    rows = good_day()
    parked = archiver_run(at(DAY, "18:00"))      # also in the database: must not double up
    late = nightly(at(DAY, "23:00"), {"prices": ("ok", "positions over 1%: 4")})
    a = tmp_path / "logs" / "job_runs_pending"
    d = tmp_path / "logs" / "daily_run_pending"
    a.mkdir(parents=True)
    d.mkdir(parents=True)
    keys = ("job_name", "run_id", "started_at", "finished_at", "item", "status", "detail", "error")
    (a / "x.json").write_text(json.dumps([dict(zip(keys, r)) for r in parked], default=str))
    (d / "y.json").write_text(json.dumps(late, default=str))
    text = report(sr, tmp_path, rows, at(DAY, "23:30"))
    assert "NIGHTLY PIPELINE - last run 2026-01-06 23:00" in text
    assert "positions over 1%: 4" in text
    assert text.count("18:00  ran 18:00") == 1 and len(re.findall(r"AAA\s", text)) == 1
    assert text.count("18:00 unchanged") == 1
    assert "2 run record file(s) waiting" in text and attention(text).startswith("ATTENTION")


# ------------------------------------------------------------ command line
def cli(tmp_path, db, **env):
    out = tmp_path / "PORTFOLIO_STATUS.txt"
    r = subprocess.run([sys.executable, str(SCRIPT), "--db", str(db), "--out", str(out),
                        "--log-dir", str(tmp_path / "logs"), "--by", "test"],
                       capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, BACKUP_WARN_GB="2", **env))
    return r, out


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("with_table", [True, False])
def test_the_database_is_never_changed(tmp_path, with_table):
    db = make_db(tmp_path, good_day())
    if not with_table:
        con = duckdb.connect(str(db))
        con.execute("DROP TABLE job_runs")
        con.close()
    before = digest(db)
    r, out = cli(tmp_path, db)
    assert r.returncode == 0, r.stdout + r.stderr
    assert digest(db) == before and not db.with_name(db.name + ".wal").exists()
    assert out.read_text(encoding="utf-8").startswith("PORTFOLIO STATUS - written ")
    assert not list(tmp_path.glob("*.tmp"))


@contextlib.contextmanager
def held(db):
    """Hold a write connection to db from another process."""
    p = subprocess.Popen([sys.executable, "-c", "import duckdb, sys; c = duckdb.connect(sys.argv[1]);"
                          " print('held', flush=True); sys.stdin.read()", str(db)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        yield
    finally:
        p.stdin.close()
        p.wait(timeout=60)


def test_another_reader_does_not_block_the_report(tmp_path):
    # Only a read-only connection can share the file with another reader.
    db = make_db(tmp_path, good_day())
    p = subprocess.Popen([sys.executable, "-c", "import duckdb, sys;"
                          " c = duckdb.connect(sys.argv[1], read_only=True);"
                          " print('held', flush=True); sys.stdin.read()", str(db)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        r, out = cli(tmp_path, db)
    finally:
        p.stdin.close()
        p.wait(timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    text = out.read_text(encoding="utf-8")
    assert "busy" not in text and "NIGHTLY PIPELINE - last run 2026-01-05 23:00" in text


def test_a_locked_database_still_writes_the_file(tmp_path):
    db = make_db(tmp_path, good_day())
    with held(db):
        r, out = cli(tmp_path, db)
    assert r.returncode == 0, r.stdout + r.stderr
    text = out.read_text(encoding="utf-8")
    assert "database busy; retry after the nightly run" in text
    assert attention(text).startswith("ATTENTION")


def test_schedule_matches_the_archiver_task_registration(sr):
    ps1 = (REPO / "scripts" / "register_archiver_task.ps1").read_text(encoding="utf-8")
    times = re.findall(r'^\$(?:Morning|Retry|Evening)Run\s*=\s*"([^"]+)"', ps1, re.M)
    as_hhmm = tuple(dt.datetime.strptime(t, "%I:%M%p").strftime("%H:%M") for t in times)
    assert len(times) == 3 and as_hhmm == sr.ARCHIVER_TIMES


def test_the_status_file_is_gitignored():
    for name in ("PORTFOLIO_STATUS.txt", "PORTFOLIO_STATUS.txt.tmp"):
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO)
        assert r.returncode == 0, name
