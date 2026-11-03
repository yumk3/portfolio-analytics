"""Tests for the ETF archiver's safety properties, using fabricated data only.

Covers raw-save-first, the HTML/bot-block guard and quarantine, dedupe, never
overwriting saved files, manual-inbox validation, retry rules and start-up
without config.ini. Every test runs in a sandbox folder; downloads are faked.
"""
import importlib.util
import io
import json
import pathlib
import shutil
import subprocess
import sys

import openpyxl
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
ARCHIVER = REPO / "archiver" / "etf_archiver.py"

GOOD_CSV = (b"Ticker,Name,Identifier,Weight,Shares,Market Value\n"
            b"XYZ,Example Corp,000000001,60.00%,100,1000\n"
            b"ABC,Sample Inc,000000002,40.00%,50,700\n")
BOT_BLOCK_HTML = (b"<!DOCTYPE html><html><head><title>Access Denied</title></head>"
                  b"<body>You don't have permission to access this server.</body></html>")


def json_holdings(extra=None):
    payload = {"effectiveDate": "2026-01-02",
               "holdings": [{"ticker": "XYZ", "weight": 60.0},
                            {"ticker": "ABC", "weight": 40.0}]}
    payload.update(extra or {})
    return json.dumps(payload).encode()


def xlsx_bytes(rows):
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, content, status_code=200):
        self.content, self.status_code = content, status_code


@pytest.fixture
def arc(tmp_path, monkeypatch):
    """A fresh archiver module whose every output is redirected under tmp_path."""
    spec = importlib.util.spec_from_file_location("etf_archiver_under_test", ARCHIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.configure(tmp_path / "sandbox")
    assert tmp_path in mod.RAW_ROOT.parents          # never the real archive
    monkeypatch.setattr(mod, "RETRY_WAITS", (0, 0))
    return mod


def fake_download(arc, monkeypatch, content, status=200):
    calls = []

    def fetch(url, ticker):
        calls.append(url)
        return FakeResponse(content, status)
    monkeypatch.setattr(arc, "fetch", fetch)
    return calls


def fund(ext):
    return {"product_id": "", "url_template": "https://example.invalid/{TICKER}." + ext,
            "file_type": ext}


def filed(arc, ticker="XYZ"):
    day = arc.RAW_ROOT / arc.TODAY
    return sorted(day.glob(f"{ticker}_*")) if day.exists() else []


def rejected(arc):
    q = arc.RAW_ROOT / "_rejected" / arc.TODAY
    return sorted(q.glob("*.rejected")) if q.exists() else []


def incoming(arc):
    d = arc.RAW_ROOT / "_incoming"
    return [p for p in d.rglob("*") if p.is_file()] if d.exists() else []


# ----------------------------------------------------------- raw-save-first
def test_response_is_on_disk_before_it_is_parsed(arc, monkeypatch):
    fake_download(arc, monkeypatch, GOOD_CSV)
    seen = []
    real_validate = arc.validate

    def spy(path, ext, issuer):
        seen.append(path)
        assert path.exists() and path.read_bytes() == GOOD_CSV
        assert (arc.RAW_ROOT / "_incoming") in path.parents
        return real_validate(path, ext, issuer)
    monkeypatch.setattr(arc, "validate", spy)

    r = arc.capture_automated("XYZ", "generic", fund("csv"))

    assert r["status"] == "captured" and len(seen) == 1
    assert [p.read_bytes() for p in filed(arc)] == [GOOD_CSV]
    assert incoming(arc) == []


# --------------------------------------------------------- HTML / bot guard
@pytest.mark.parametrize("ext", ["csv", "xlsx"])
def test_html_disguised_as_data_is_quarantined_not_filed(arc, monkeypatch, ext):
    fake_download(arc, monkeypatch, BOT_BLOCK_HTML)

    r = arc.capture_automated("XYZ", "generic", fund(ext))

    assert r["status"] == "blocked"
    assert filed(arc) == []
    q = rejected(arc)
    assert len(q) == 1 and q[0].name.endswith(f".{ext}.rejected")
    assert q[0].read_bytes() == BOT_BLOCK_HTML
    reason = q[0].with_name(q[0].name + ".reason.txt")
    assert reason.exists() and "HTML" in reason.read_text(encoding="utf-8")
    assert incoming(arc) == []


@pytest.mark.parametrize("ext, content, status", [
    ("xlsx", b"Service unavailable, please try later. " * 60, 200),   # not a zip
    ("json", b'{"error": "not found", "status": 404}' + b" " * 2000, 200),
    ("csv", b"", 200),
    ("csv", BOT_BLOCK_HTML, 403),
], ids=["xlsx-not-a-zip", "json-error-blob", "csv-empty", "http-403"])
def test_other_bad_responses_are_quarantined(arc, monkeypatch, ext, content, status):
    fake_download(arc, monkeypatch, content, status)

    r = arc.capture_automated("XYZ", "generic", fund(ext))

    assert r["status"] in ("blocked", "failed")
    assert filed(arc) == []
    q = rejected(arc)
    assert len(q) == 1 and q[0].with_name(q[0].name + ".reason.txt").exists()


def test_xlsx_without_a_holdings_header_is_rejected(arc, monkeypatch):
    fake_download(arc, monkeypatch, xlsx_bytes([["Fund", "Report"], ["Name", "Ticker"]]))

    r = arc.capture_automated("XYZ", "generic", fund("xlsx"))

    assert r["status"] == "failed" and "No header row found" in r["error"]
    assert filed(arc) == [] and len(rejected(arc)) == 1


def test_real_xlsx_holdings_are_accepted(arc, monkeypatch):
    content = xlsx_bytes([["Holdings:", "As of 02-Jan-2026"],
                          ["Name", "Ticker", "Weight"],
                          ["Example Corp", "XYZ", 60.0],
                          ["Sample Inc", "ABC", 40.0]])
    fake_download(arc, monkeypatch, content)

    r = arc.capture_automated("XYZ", "generic", fund("xlsx"))

    assert r["status"] == "captured" and r["as_of"] == "2026-01-02"
    assert [p.read_bytes() for p in filed(arc)] == [content]


# ------------------------------------------------------------------- dedupe
def test_byte_identical_capture_is_not_stored_twice(arc, monkeypatch):
    earlier = arc.RAW_ROOT / "2026-01-01" / "XYZ_generic.csv"
    earlier.parent.mkdir(parents=True)
    earlier.write_bytes(GOOD_CSV)
    fake_download(arc, monkeypatch, GOOD_CSV)

    r = arc.capture_automated("XYZ", "generic", fund("csv"))

    assert r["status"] == "unchanged"
    assert filed(arc) == [] and incoming(arc) == []
    assert earlier.read_bytes() == GOOD_CSV


def test_same_as_of_with_different_bytes_is_saved(arc, monkeypatch):
    earlier = arc.RAW_ROOT / "2026-01-01" / "XYZ_generic.json"
    earlier.parent.mkdir(parents=True)
    earlier.write_bytes(json_holdings())
    newer = json_holdings({"generated": "later"})            # same effectiveDate
    fake_download(arc, monkeypatch, newer)

    r = arc.capture_automated("XYZ", "generic", fund("json"))

    assert r["status"] == "captured" and "same as-of, content differs" in r["detail"]
    assert [p.read_bytes() for p in filed(arc)] == [newer]
    assert earlier.read_bytes() == json_holdings()


# --------------------------------------------------------- never overwrite
def test_existing_capture_for_today_is_never_overwritten(arc, monkeypatch):
    today_file = arc.RAW_ROOT / arc.TODAY / "XYZ_generic.csv"
    today_file.parent.mkdir(parents=True)
    today_file.write_bytes(b"previously saved bytes")
    calls = fake_download(arc, monkeypatch, GOOD_CSV)

    r = arc.capture_automated("XYZ", "generic", fund("csv"))

    assert r["status"] == "already_captured" and calls == []
    assert today_file.read_bytes() == b"previously saved bytes"


def test_unique_never_reuses_an_existing_name(arc, tmp_path):
    p = tmp_path / "XYZ_generic.csv"
    p.write_bytes(b"one")
    assert arc.unique(p) == tmp_path / "XYZ_generic-2.csv"
    (tmp_path / "XYZ_generic-2.csv").write_bytes(b"two")
    assert arc.unique(p) == tmp_path / "XYZ_generic-3.csv"


def test_repeated_rejections_keep_every_quarantined_copy(arc, monkeypatch):
    fake_download(arc, monkeypatch, BOT_BLOCK_HTML)
    arc.capture_automated("XYZ", "generic", fund("csv"))
    arc.capture_automated("XYZ", "generic", fund("csv"))

    q = rejected(arc)
    assert len(q) == 2 and len({p.name for p in q}) == 2


# ------------------------------------------------------------- manual inbox
def test_manual_inbox_html_is_rejected_and_inbox_left_untouched(arc):
    arc.INBOX.mkdir(parents=True)
    dropped = arc.INBOX / "XYZ_holdings.csv"
    dropped.write_bytes(BOT_BLOCK_HTML)

    r = arc.capture_manual("XYZ")

    assert r["status"] == "failed"
    assert filed(arc) == [] and len(rejected(arc)) == 1
    assert dropped.read_bytes() == BOT_BLOCK_HTML


def test_manual_inbox_valid_file_is_filed_as_a_copy(arc):
    arc.INBOX.mkdir(parents=True)
    dropped = arc.INBOX / "XYZ_holdings.csv"
    dropped.write_bytes(GOOD_CSV)

    r = arc.capture_manual("XYZ")

    assert r["status"] == "captured"
    assert [p.read_bytes() for p in filed(arc)] == [GOOD_CSV]
    assert dropped.read_bytes() == GOOD_CSV


# ---------------------------------------------------------------- retries
class _Resp:
    def __init__(self, status):
        self.status_code, self.content = status, b""


@pytest.mark.parametrize("status, expected_calls", [(403, 1), (200, 1), (503, 3)])
def test_block_pages_are_never_retried(arc, monkeypatch, status, expected_calls):
    calls = []

    def get(url, **kw):
        calls.append(url)
        return _Resp(status)
    monkeypatch.setattr(arc.cffi_requests, "get", get)

    arc.fetch("https://example.invalid/x.csv", "XYZ")

    assert len(calls) == expected_calls


# --------------------------------------------------- command-line behaviour
def test_check_file_rejects_a_bot_block_page(tmp_path):
    page = tmp_path / "XYZ_holdings.csv"
    page.write_bytes(BOT_BLOCK_HTML)

    r = subprocess.run([sys.executable, str(ARCHIVER), "--check-file", str(page)],
                       capture_output=True, text=True, timeout=120)

    assert r.returncode == 1 and r.stdout.startswith("REJECTED")


def _copy_without_config(tmp_path):
    folder = tmp_path / "archiver"
    folder.mkdir()
    shutil.copy2(ARCHIVER, folder / "etf_archiver.py")
    return folder


def test_import_does_not_require_config(tmp_path):
    folder = _copy_without_config(tmp_path)
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); import etf_archiver",
                        str(folder)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr


def test_real_run_without_config_stops_with_copy_message(tmp_path):
    folder = _copy_without_config(tmp_path)

    r = subprocess.run([sys.executable, str(folder / "etf_archiver.py")],
                       capture_output=True, text=True, timeout=120, cwd=str(tmp_path))

    assert r.returncode == 1
    expected = (f"ERROR: config file not found: {folder / 'config.ini'}. "
                f"Copy {folder / 'config.example.ini'} to config.ini and fill it in.")
    assert r.stderr.strip() == expected
    assert not (tmp_path / "data").exists()                 # nothing was created
