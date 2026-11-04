"""ETF archiver: parked job_runs rows, and the PORTFOLIO_STATUS.txt refresh that must never
affect a capture. Every test runs in a sandbox folder; downloads are faked."""
import contextlib
import datetime as dt
import importlib.util
import json
import pathlib
import subprocess
import sys

import duckdb
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
ARCHIVER = REPO / "archiver" / "etf_archiver.py"
GOOD_CSV = (b"Ticker,Name,Identifier,Weight,Shares,Market Value\n"
            b"XYZ,Example Corp,000000001,60.00%,100,1000\n"
            b"ABC,Sample Inc,000000002,40.00%,50,700\n")


class FakeResponse:
    def __init__(self, content, status_code=200):
        self.content, self.status_code = content, status_code


@pytest.fixture
def arc(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("etf_archiver_under_test", ARCHIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.configure(tmp_path / "sandbox")
    assert tmp_path in mod.RAW_ROOT.parents          # never the real archive
    monkeypatch.setattr(mod, "RETRY_WAITS", (0, 0))
    monkeypatch.setattr(mod, "POLITE_DELAY", 0)
    monkeypatch.setattr(mod, "DB_LOCK_TRIES", 2)
    monkeypatch.setattr(mod, "DB_LOCK_WAIT", 0)
    return mod


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


T0 = dt.datetime(2026, 1, 6, 7, 15, 0, 250000)


def rows(run_id):
    return [{"job_name": "etf_archiver", "run_id": run_id, "started_at": T0,
             "finished_at": T0 + dt.timedelta(seconds=30), "item": item, "status": "ok",
             "detail": "", "error": ""} for item in ("XYZ", "_run")]


def recorded(arc):
    con = duckdb.connect(str(arc.DB_PATH), read_only=True)
    try:
        return con.execute("SELECT run_id, item, started_at FROM job_runs "
                           "ORDER BY run_id, item").fetchall()
    finally:
        con.close()


def pending(arc):
    d = arc.LOG_DIR / "job_runs_pending"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


# ------------------------------------------------------------ parked rows
def test_rows_are_parked_while_locked_and_written_once_later(arc):
    arc.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    duckdb.connect(str(arc.DB_PATH)).close()
    with held(arc.DB_PATH):
        warn = arc.write_job_runs(rows("r1"))
    assert warn.startswith("job_runs not written (database stayed locked")
    assert len(pending(arc)) == 1

    assert arc.write_job_runs(rows("r2")) is None and pending(arc) == []
    assert arc.write_job_runs(rows("r3")) is None
    got = recorded(arc)
    assert [(r[0], r[1]) for r in got] == [(rid, i) for rid in ("r1", "r2", "r3")
                                           for i in ("XYZ", "_run")]
    assert got[0][2] == T0                                      # still a timestamp


def test_a_parked_file_left_after_a_write_is_not_inserted_twice(arc):
    assert arc.write_job_runs(rows("r1")) is None
    d = arc.LOG_DIR / "job_runs_pending"                       # as if the delete never happened
    d.mkdir(parents=True)
    (d / "left.json").write_text(json.dumps(rows("r1"), default=str), encoding="utf-8")
    assert arc.write_job_runs(rows("r2")) is None
    assert [(r[0], r[1]) for r in recorded(arc)] == [
        ("r1", "XYZ"), ("r1", "_run"), ("r2", "XYZ"), ("r2", "_run")]
    assert pending(arc) == []


# ------------------------------------------------------------ status refresh
def run_main(arc, tmp_path, monkeypatch):
    monkeypatch.setattr(arc, "fetch", lambda url, ticker: FakeResponse(GOOD_CSV))
    funds = tmp_path / "funds.csv"
    funds.write_text("ticker,issuer,product_id,url_template,file_type,fetch_method\n"
                     "XYZ,generic,,https://example.invalid/{TICKER}.csv,csv,cffi\n",
                     encoding="utf-8")
    code = arc.main(funds)
    saved = sorted((arc.RAW_ROOT / arc.TODAY).glob("XYZ_*"))
    return code, saved


def stamp(path):
    return path.stat().st_mtime_ns if path.exists() else None


def test_each_run_refreshes_the_portfolio_status(arc, tmp_path, monkeypatch):
    real = REPO / "PORTFOLIO_STATUS.txt"
    before = stamp(real)
    code, saved = run_main(arc, tmp_path, monkeypatch)
    assert code == 0 and [p.read_bytes() for p in saved] == [GOOD_CSV]
    text = (arc.STATUS_FILE.parent / "PORTFOLIO_STATUS.txt").read_text(encoding="utf-8")
    assert "(by etf_archiver)" in text and "XYZ" in text and "captured" in text
    assert stamp(real) == before                                # the sandbox run left it alone


@pytest.mark.parametrize("body", ["raise SystemExit(5)\n", "import time\ntime.sleep(30)\n",
                                  "1/0\n"])
def test_a_status_failure_never_affects_the_capture(arc, tmp_path, monkeypatch, body):
    broken = tmp_path / "broken_status.py"
    broken.write_text(body, encoding="utf-8")
    monkeypatch.setattr(arc, "STATUS_SCRIPT", broken)
    monkeypatch.setattr(arc, "STATUS_TIMEOUT", 2)
    code, saved = run_main(arc, tmp_path, monkeypatch)
    assert code == 0 and [p.read_bytes() for p in saved] == [GOOD_CSV]
    assert {r[1] for r in recorded(arc)} == {"XYZ", "_run"}
    status = arc.STATUS_FILE.read_text(encoding="utf-8")
    assert "RESULT: OK" in status and "PORTFOLIO_STATUS.txt not updated" in status


def test_a_status_script_that_cannot_start_never_affects_the_capture(arc, tmp_path, monkeypatch):
    monkeypatch.setattr(arc, "STATUS_SCRIPT", tmp_path / "missing" / "status_report.py")
    monkeypatch.setattr(arc.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    code, saved = run_main(arc, tmp_path, monkeypatch)
    assert code == 0 and [p.read_bytes() for p in saved] == [GOOD_CSV]
    assert "PORTFOLIO_STATUS.txt not updated (OSError: x)" in arc.STATUS_FILE.read_text(encoding="utf-8")
