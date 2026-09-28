r"""Capture each tracked fund's daily holdings file into data\raw\etf_holdings: save raw first,
then validate (HTML/bot-block guard) and file, deduplicate or quarantine it.
Usage: <your venv>\Scripts\python.exe archiver\etf_archiver.py [--sandbox DIR | --check-file F]
"""

import argparse
import configparser
import csv
import datetime
import hashlib
import io
import json
import msvcrt
import os
import pathlib
import re
import shutil
import sys
import time
import traceback
import uuid

# Missing libraries are recorded, not raised, so preflight can explain the fix.
# curl_cffi impersonates a real browser's TLS fingerprint; several issuers
# reject plain HTTP clients.
_MISSING = []
try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    _MISSING.append("curl_cffi")
try:
    import pandas as pd
except ImportError:
    _MISSING.append("pandas")
for _name in ("openpyxl", "pyarrow", "duckdb"):   # xlsx, parquet, job_runs
    try:
        __import__(_name)
    except ImportError:
        _MISSING.append(_name)

# Some issuers block requests that don't look like a browser.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "text/csv,application/vnd.ms-excel,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# Retries are for network trouble only (timeouts, dropped connections,
# 429/5xx), and deliberately slow. A block page (403, or HTML) is never
# retried: rapid repeats are exactly what bot detection looks for.
RETRY_WAITS = (20, 60)          # seconds before retry 1 and retry 2
RETRYABLE_HTTP = {429, 500, 502, 503, 504}
TIMEOUT = 45
POLITE_DELAY = 2                # seconds between issuer requests
LOG_KEEP_DAYS = 90
DB_LOCK_TRIES, DB_LOCK_WAIT = 12, 10    # up to 2 minutes if the DB is busy
JOB_NAME = "etf_archiver"

BASE = pathlib.Path(__file__).parent          # archiver\
REPO = BASE.parent                            # repo root
TODAY = datetime.date.today().isoformat()
RUN_TIME = datetime.datetime.now().strftime("%H%M%S")


CONFIG_FILE = BASE / "config.ini"            # private; template config.example.ini
NO_EXPECTED = "(set expected_python in archiver\\config.ini)"


def read_config(path=CONFIG_FILE):
    """Return the parsed config.ini that sits next to this script."""
    cp = configparser.ConfigParser(interpolation=None)
    if not cp.read(path, encoding="utf-8"):
        raise SystemExit(f"ERROR: config file not found: {path}. Copy "
                         f"{BASE / 'config.example.ini'} to config.ini and fill it in.")
    return cp


def load_config(path=CONFIG_FILE):
    """Return [paths] from config.ini, resolved against the repo root; blank means None."""
    cp = read_config(path)
    def p(key):
        raw = cp["paths"].get(key, "").strip()
        if not raw:
            return None
        v = pathlib.Path(raw)
        return v if v.is_absolute() else REPO / v
    return {k: p(k) for k in cp["paths"]}


def expected_python():
    """Return [run] expected_python, named in the wrong-Python message."""
    try:
        return read_config()["run"].get("expected_python", "").strip() or NO_EXPECTED
    except (SystemExit, KeyError):
        return NO_EXPECTED


def funds_from_config(cp):
    """Return one fund dict per [fund TICKER] section, in file order."""
    funds = []
    for sec in cp.sections():
        if not sec.lower().startswith("fund "):
            continue
        f = cp[sec]
        funds.append({
            "ticker": sec[5:].strip().upper(),
            "issuer": f.get("issuer", "").strip().lower(),
            "product_id": f.get("product_id", "").strip(),
            "url_template": f.get("url_template", "").strip(),
            "file_type": f.get("file_type", "").strip(),
            "fetch_method": (f.get("fetch_method", "") or "cffi").strip().lower(),
        })
    return funds


def configure(sandbox=None):
    """Set every output location from config.ini, or under a sandbox folder for tests."""
    global RAW_ROOT, PARSED_ROOT, LOG_DIR, DB_PATH, BACKUP_ROOT, STATUS_FILE
    global LOG_FILE, LOCK_FILE, INBOX, SANDBOX
    SANDBOX = sandbox is not None
    if SANDBOX:
        s = pathlib.Path(sandbox).resolve()
        c = {"raw_root": s / "raw", "parsed_root": s / "parsed",
             "log_dir": s / "logs", "db_path": s / "test.duckdb",
             # backup only works if DIR\cloud_drive exists, so tests can
             # simulate the backup location being unavailable
             "backup_root": s / "cloud_drive" / "PortfolioBackup" / "etf_archive_raw"}
        STATUS_FILE = s / "ETF_ARCHIVER_STATUS.txt"
    else:
        c = load_config()
        STATUS_FILE = REPO / "ETF_ARCHIVER_STATUS.txt"
    RAW_ROOT, PARSED_ROOT = c["raw_root"], c["parsed_root"]
    LOG_DIR, DB_PATH, BACKUP_ROOT = c["log_dir"], c["db_path"], c.get("backup_root")
    LOG_FILE = LOG_DIR / f"etf_archiver_{TODAY}.log"
    LOCK_FILE = LOG_DIR / "etf_archiver.lock"
    INBOX = RAW_ROOT / "_manual_inbox"


configure()


def log(message):
    """Print a timestamped line and append it to today's log file."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path):
    try:
        return str(pathlib.Path(path).relative_to(RAW_ROOT))
    except ValueError:
        return str(path)


def build_url(ticker, issuer, product_id, url_template=None, file_type=None):
    """Return (url, file extension) for a fund.

    issuer: ishares (product_id), ssga (ticker), invesco (CUSIP in
    product_id) or generic (url_template). Manual funds are never fetched;
    they are filed from _manual_inbox.
    """
    t = ticker.strip().upper()

    if issuer == "ishares":
        if not product_id:
            raise ValueError(f"{t}: iShares funds need a product_id in funds.csv.")
        # The slug can literally be the word "fund" - only the numeric ID matters.
        return (
            f"https://www.ishares.com/us/products/{product_id}/fund/"
            f"1467271812596.ajax?fileType=csv&fileName={t}_holdings&dataType=fund"
        ), "csv"

    if issuer == "ssga":
        return (
            "https://www.ssga.com/us/en/institutional/library-content/products/"
            f"fund-data/etfs/us/holdings-daily-us-en-{t.lower()}.xlsx"
        ), "xlsx"

    if issuer == "invesco":
        # The old ?action=download page is a JavaScript shell that never
        # contains data. This is the JSON service that shell calls.
        # Keyed by CUSIP: idType=ticker works for some funds but 500s for others.
        if not product_id:
            raise ValueError(
                f"{t}: Invesco funds need the fund's CUSIP in product_id."
            )
        return (
            "https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/"
            f"{product_id}/holdings/fund?idType=cusip&productType=ETF"
        ), "json"

    if issuer == "generic":
        if not url_template:
            raise ValueError(f"{t}: issuer 'generic' needs a url_template.")
        url = url_template.replace("{TICKER}", t).replace("{ticker}", t.lower())
        return url, (file_type or "csv")

    raise ValueError(f"Unknown issuer '{issuer}' for {t}")


class Rejected(Exception):
    """A response that is not holdings data and must never be filed as such.

    blocked=True marks a bot-block or HTML page.
    """
    def __init__(self, reason, blocked=False):
        super().__init__(reason)
        self.blocked = blocked


def looks_like_html(content):
    """Return True for an HTML page served in place of a data file."""
    head = content[:1000].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") \
        or b"<head>" in head or b"<title>" in head


def looks_like_markup(content):
    """Return True if a text response starts with any HTML or XML markup."""
    head = content[:2000].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return looks_like_html(content) or head.startswith(b"<")


# Real Excel files always begin with these bytes (xlsx is a zip archive).
MAGIC = {"xlsx": b"PK\x03\x04", "xls": b"\xd0\xcf\x11\xe0"}


def check_json_holdings(content):
    """Return the holdings row count, or raise ValueError for a non-holdings payload.

    Complements the HTML guard, which cannot tell real holdings from a JSON
    error blob.
    """
    try:
        data = json.loads(content)
    except Exception as e:
        raise ValueError(f"Response is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ValueError(
            f"JSON parsed but is a {type(data).__name__}, expected an object."
        )
    holdings = data.get("holdings")
    if not isinstance(holdings, list) or not holdings:
        raise ValueError(
            "JSON parsed but carries no 'holdings' rows "
            f"(top-level keys: {sorted(data)[:8]})."
        )
    return len(holdings)


def find_header_row(lines):
    """Return the index of the column-header line (issuer files start with metadata)."""
    for i, line in enumerate(lines[:60]):
        low = line.lower()
        if "weight" in low and ("%" in line or "percent" in low):
            return i
        if low.startswith(("ticker,", '"ticker"', "name,", '"name"')) \
           and line.count(",") >= 3:
            return i
    preview = " | ".join(l[:60] for l in lines[:3])
    raise ValueError(f"No header row found. First lines look like: {preview}")


def find_excel_header_row(raw):
    """Excel counterpart of find_header_row, with the same diagnostic on failure."""
    for i in range(min(20, len(raw))):
        row = " ".join(str(x) for x in raw.iloc[i].tolist())
        if "Weight" in row:
            return i
    preview = " | ".join(
        " ".join(str(x) for x in raw.iloc[i].tolist() if pd.notna(x))[:60]
        for i in range(min(3, len(raw))))
    raise ValueError(
        f"No header row found in the first 20 rows. First rows look like: {preview}")


def read_holdings(path, ext, issuer):
    """Read a saved raw file into a holdings DataFrame; raises on failure."""
    if ext == "json":
        # Invesco's API: {"holdings": [{ticker, issuerName, units, ...}, ...]}
        payload = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        df = pd.DataFrame(payload["holdings"])
    elif ext in ("xlsx", "xls"):
        raw = pd.read_excel(path, header=None)
        df = pd.read_excel(path, skiprows=find_excel_header_row(raw))
    else:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        skip = 0 if issuer == "invesco" else find_header_row(text.splitlines())
        df = pd.read_csv(io.StringIO(text), skiprows=skip)

    # Drop the disclaimer junk that issuers append at the bottom of CSV/Excel.
    # JSON has no such junk, and a blank first field there (e.g. a cash line
    # with no ticker) is a real holding, so don't strip rows for JSON.
    if ext != "json":
        df = df.dropna(how="all")
        df = df[df[df.columns[0]].notna()]
    return df


_DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y")


def _to_iso(text):
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def read_as_of(path, ext):
    """Return the as-of date stated inside the file, or '' if none is found."""
    try:
        if ext == "json":
            return str(json.loads(path.read_bytes()).get("effectiveDate") or "")
        if ext == "xlsx":
            import openpyxl
            # Read from memory: a read-only workbook keeps the file open,
            # which would block moving it into the archive on Windows.
            ws = openpyxl.load_workbook(io.BytesIO(path.read_bytes()),
                                        read_only=True).active
            cells = [str(c) for r in ws.iter_rows(max_row=10, values_only=True)
                     for c in r if c is not None]
        else:
            cells = path.read_text(encoding="utf-8-sig",
                                   errors="replace").splitlines()[:15]
        for c in cells:
            m = re.search(r"as of[:\s]*([A-Za-z]{3,9} \d{1,2}, \d{4}|\d{1,2}-[A-Za-z]{3}-\d{4}"
                          r"|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})", c, re.I)
            if m:
                return _to_iso(m.group(1))
    except Exception:
        pass
    return ""


def validate(path, ext, issuer):
    """Return (holdings table, as-of date or ''), or raise Rejected with a reason.

    Every file, downloaded or manual, passes this guard before it can count
    as holdings: issuers can answer with an HTML bot-block or error page,
    HTTP 200 and a data-file extension, which would otherwise be archived as
    if it were holdings.
    """
    content = path.read_bytes()
    size = (f"{len(content) / 1024:.0f} KB" if len(content) >= 1024
            else f"{len(content)} bytes")
    if not content:
        raise Rejected("empty response (0 bytes)")
    if looks_like_html(content):
        raise Rejected(f"server returned an HTML page ({size}), not holdings "
                       "data - usually a bot-block or error page", blocked=True)
    if ext in MAGIC:
        if not content.startswith(MAGIC[ext]):
            raise Rejected(f"not a real .{ext} file (starts with "
                           f"{content[:12]!r}, expected {MAGIC[ext]!r})",
                           blocked=looks_like_markup(content))
    elif ext == "json":
        try:
            check_json_holdings(content)
        except ValueError as e:
            raise Rejected(str(e))
    elif looks_like_markup(content):
        raise Rejected(f"looks like HTML/XML markup ({size}), not a CSV "
                       "holdings file", blocked=True)
    try:
        df = read_holdings(path, ext, issuer)
    except Exception as e:
        raise Rejected(f"no readable holdings table: {type(e).__name__}: {e}")
    if len(df) < 1:
        raise Rejected("holdings table has no rows")
    return df, read_as_of(path, ext)


def raw_path(ticker, issuer, ext):
    return RAW_ROOT / TODAY / f"{ticker}_{issuer}.{ext}"


def todays_capture(ticker):
    day = RAW_ROOT / TODAY
    return sorted(day.glob(f"{ticker}_*.*")) if day.exists() else []


def last_saved(ticker):
    """Return the most recent saved raw file for a fund, or None."""
    files = [f for f in RAW_ROOT.glob(f"????-??-??/{ticker}_*.*") if f.is_file()]
    return max(files, key=lambda f: (f.parent.name, f.stat().st_mtime)) if files else None


def unique(path):
    """Return path, or path with -2, -3, ... appended, so nothing is overwritten."""
    n, p = 1, path
    while p.exists():
        n += 1
        p = path.with_name(f"{path.stem}-{n}{path.suffix}")
    return p


def stage(ticker, issuer, ext, content):
    """Write the raw response to _incoming before anything parses it.

    Raw-save-first: a parsing bug can be fixed later, a missed download cannot.
    """
    path = unique(RAW_ROOT / "_incoming" / TODAY / f"{ticker}_{issuer}_{RUN_TIME}.{ext}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:
        f.write(content)
    return path


def quarantine(path, reason, copy=False):
    """Move (or copy) a rejected file to _rejected, with its reason alongside."""
    dest = unique(RAW_ROOT / "_rejected" / TODAY / (path.name + ".rejected"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copy2(path, dest)
    else:
        os.rename(path, dest)
    dest.with_name(dest.name + ".reason.txt").write_text(
        f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\nsource: {path}\n"
        f"reason: {reason}\n", encoding="utf-8")
    return dest


def write_parsed(df, ticker, issuer, as_of):
    """Write a best-effort parquet copy; failure is logged, never fatal."""
    try:
        df = df.copy()
        df["source_ticker"] = ticker
        df["as_of_date"] = as_of or None
        df["download_date"] = TODAY
        out = PARSED_ROOT / TODAY / f"{ticker}_{issuer}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out, index=False)
        return f"parsed {len(df)} holdings"
    except Exception as e:
        log(f"  {ticker}: PARSE FAILED ({type(e).__name__}: {e}) - raw file kept.")
        return "parse failed, raw kept"


def file_away(src, ticker, issuer, ext, df, as_of, copy):
    """File a validated file unless it is byte-identical to the fund's last capture.

    Only an exact hash match counts as unchanged; the same as-of date with
    different bytes is saved.
    """
    fp = sha256(src)
    last = last_saved(ticker)
    if last is not None and sha256(last) == fp:
        if not copy:
            src.unlink()        # staged duplicate of an archived file (hash-verified)
        return "unchanged", f"as-of {as_of or '?'}; identical to {rel(last)}"

    dest = unique(raw_path(ticker, issuer, ext))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copy2(src, dest)
    else:
        os.rename(src, dest)            # raises rather than overwrite
    note = ""
    if last is not None and as_of and read_as_of(last, last.suffix.lstrip(".")) == as_of:
        note = "; same as-of, content differs"
        log(f"  {ticker}: same as-of ({as_of}) as {rel(last)}, content differs - saved.")
    parsed = write_parsed(df, ticker, issuer, as_of)
    return "captured", (f"as-of {as_of or '?'}; {rel(dest)}; "
                        f"{dest.stat().st_size / 1024:.0f} KB; sha {fp[:12]}; "
                        f"{parsed}{note}")


class NetworkError(Exception):
    pass


def fetch(url, ticker):
    """GET with up to two slow retries, for network errors and 429/5xx only."""
    err = None
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            resp = cffi_requests.get(url, headers=HEADERS, timeout=TIMEOUT,
                                     impersonate="chrome")
            if resp.status_code not in RETRYABLE_HTTP:
                return resp
            err = f"HTTP {resp.status_code}"
        except cffi_requests.RequestsError as e:
            resp, err = None, f"{type(e).__name__}: {e}"
        if attempt < len(RETRY_WAITS):
            log(f"  {ticker}: network problem ({err}); retry {attempt + 1} "
                f"in {RETRY_WAITS[attempt]}s")
            time.sleep(RETRY_WAITS[attempt])
    if resp is not None:
        return resp                     # last 429/5xx; the caller rejects it
    raise NetworkError(f"gave up after {len(RETRY_WAITS)} retries: {err}")


def result(status, detail="", error="", as_of=""):
    return {"status": status, "detail": detail, "error": error, "as_of": as_of}


def capture_automated(ticker, issuer, fund):
    """Download, stage, validate and file one automated fund."""
    already = todays_capture(ticker)
    if already:
        return result("already_captured", f"{rel(already[0])} saved earlier today")

    url, ext = build_url(ticker, issuer, fund["product_id"],
                         fund["url_template"], fund["file_type"])
    resp = fetch(url, ticker)
    staged = stage(ticker, issuer, ext, resp.content)          # raw save first
    log(f"  {ticker}: response HTTP {resp.status_code}, {len(resp.content)} bytes, "
        f"sha {sha256(staged)[:12]} -> {rel(staged)}")

    if resp.status_code != 200:
        why = f"HTTP {resp.status_code} from issuer"
        q = quarantine(staged, why)
        return result("blocked" if resp.status_code in (401, 403) else "failed",
                      f"quarantined {rel(q)}", why)
    try:
        df, as_of = validate(staged, ext, issuer)
    except Rejected as e:
        q = quarantine(staged, str(e))
        log(f"  {ticker}: REJECTED - {e} -> {rel(q)}")
        return result("blocked" if e.blocked else "failed",
                      f"quarantined {rel(q)}", str(e))
    status, detail = file_away(staged, ticker, issuer, ext, df, as_of, copy=False)
    return result(status, detail, as_of=as_of)


def capture_manual(ticker):
    """Validate and file a hand-downloaded file for a manual fund, if present."""
    for f in todays_capture(ticker):             # already filed today?
        ext = f.suffix.lower().lstrip(".")
        try:
            _, as_of = validate(f, ext, "manual")
            return result("already_captured", f"{rel(f)} (manual)", as_of=as_of)
        except Rejected as e:
            q = quarantine(f, str(e), copy=True)     # never move a filed file
            return result("failed", f"{rel(f)} is not valid holdings; copy in "
                          f"{rel(q)}", str(e))

    pattern = re.compile(rf"{re.escape(ticker)}(?![A-Za-z0-9])", re.I)
    candidates = [f for f in INBOX.glob("*.*")
                  if f.is_file() and pattern.match(f.name)] if INBOX.exists() else []
    if not candidates:
        return result("manual", "not downloaded today")

    src = max(candidates, key=lambda f: f.stat().st_mtime)
    ext = src.suffix.lower().lstrip(".")
    try:
        df, as_of = validate(src, ext, "manual")
    except Rejected as e:
        q = quarantine(src, str(e), copy=True)
        log(f"  {ticker}: manual file {src.name} REJECTED - {e}")
        return result("failed", f"inbox file {src.name} rejected; copy in {rel(q)}",
                      str(e))
    status, detail = file_away(src, ticker, "manual", ext, df, as_of, copy=True)
    return result(status, f"manual file {src.name}; {detail}", as_of=as_of)


def preflight(funds_file):
    """Return plain-English problems that must block the run."""
    problems = []
    if _MISSING:
        problems.append(
            f"Missing Python libraries: {', '.join(_MISSING)}. This usually means "
            f"the wrong Python was used. Running: {sys.executable}. "
            f"Expected: {expected_python()}. To install into the running one: "
            f"\"{sys.executable}\" -m pip install {' '.join(_MISSING)}")
    # A venv inside a cloud-synced folder has silently lost libraries before.
    if "onedrive" in sys.executable.lower():
        problems.append(f"Python is running from inside a OneDrive-synced folder "
                        f"({sys.executable}). Use {expected_python()} instead.")
    if funds_file is not None and not funds_file.exists():
        problems.append(f"Fund list not found: {funds_file}")
    if funds_file is None:
        try:
            if not funds_from_config(read_config()):
                problems.append(f"No [fund TICKER] sections in {CONFIG_FILE}")
        except SystemExit as e:
            problems.append(str(e))
    try:
        RAW_ROOT.mkdir(parents=True, exist_ok=True)
        probe = RAW_ROOT / f".write_test_{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as e:
        problems.append(f"Cannot write to the archive folder {RAW_ROOT}: {e}")
    return problems


def acquire_lock():
    """Take a Windows file lock (released even if the process dies); None if held."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "a+b")
    fh.seek(0)
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    return fh


def rotate_logs():
    cutoff = datetime.date.today() - datetime.timedelta(days=LOG_KEEP_DAYS)
    for f in LOG_DIR.glob("etf_archiver_????-??-??.log"):
        try:
            if datetime.date.fromisoformat(f.stem[-10:]) < cutoff:
                f.unlink()
        except (ValueError, OSError):
            pass


def backup_new_files():
    """Copy new raw files one way to backup_root; return (copied, warning).

    Never reads, deletes or overwrites anything in the backup (a local
    ledger tracks what was sent) and never raises: a backup problem must not
    fail a capture.
    """
    try:
        if BACKUP_ROOT is None:
            return 0, "no backup folder configured (backup_root in config.ini); backup skipped"
        if not BACKUP_ROOT.parent.parent.exists():
            return 0, f"Backup location not found ({BACKUP_ROOT.parent.parent}); backup skipped"
        ledger = RAW_ROOT / "_manifest" / "backup_ledger.csv"
        done = set()
        if ledger.exists():
            with open(ledger, newline="", encoding="utf-8") as f:
                done = {r["path"] for r in csv.DictReader(f)}
        skip_dirs = {"_incoming", "_rejected", "_manual_inbox"}
        todo = [f for f in sorted(RAW_ROOT.rglob("*"))
                if f.is_file() and f != ledger and not f.name.startswith(".")
                and f.relative_to(RAW_ROOT).parts[0] not in skip_dirs
                and f.relative_to(RAW_ROOT).as_posix() not in done]
        copied, new_rows = 0, []
        for f in todo:
            r = f.relative_to(RAW_ROOT).as_posix()
            dest = BACKUP_ROOT / r
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + f".partial{os.getpid()}")
            with open(part, "xb") as out:
                out.write(f.read_bytes())
            shutil.copystat(f, part)
            try:
                os.rename(part, dest)           # fails if dest exists: no overwrite
                copied += 1
                outcome = "copied"
            except FileExistsError:
                outcome = "already in backup"
            new_rows.append({"path": r, "sha256": sha256(f), "outcome": outcome,
                             "at": datetime.datetime.now().isoformat(timespec="seconds")})
        if new_rows:
            new = not ledger.exists()
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with open(ledger, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=["path", "sha256", "outcome", "at"])
                if new:
                    w.writeheader()
                w.writerows(new_rows)
        return copied, None
    except Exception as e:
        return 0, f"Backup failed ({type(e).__name__}: {e}); capture unaffected"


JOB_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS job_runs (
    job_name    VARCHAR NOT NULL,   -- which scheduled job: 'etf_archiver', 'snaptrade_daily', ...
    run_id      VARCHAR NOT NULL,   -- shared by every row of one run
    started_at  TIMESTAMP,
    finished_at TIMESTAMP,
    item        VARCHAR,            -- what the row is about: a fund ticker, or '_run' for the summary
    status      VARCHAR,
    detail      VARCHAR,
    error       VARCHAR
)"""


def write_job_runs(rows):
    """Append rows to job_runs, holding the database open only briefly.

    Retries while another job holds the lock, and parks the rows in a file
    for the next run if it stays locked. Returns a warning or None.
    """
    import duckdb
    pending_dir = LOG_DIR / "job_runs_pending"
    backlog = sorted(pending_dir.glob("*.json")) if pending_dir.exists() else []
    all_rows = list(rows)
    for b in backlog:
        all_rows += json.loads(b.read_text(encoding="utf-8"))
    cols = ["job_name", "run_id", "started_at", "finished_at",
            "item", "status", "detail", "error"]

    def park(why):
        pending_dir.mkdir(parents=True, exist_ok=True)
        (pending_dir / f"{TODAY}_{RUN_TIME}.json").write_text(
            json.dumps(rows, indent=1), encoding="utf-8")
        return f"job_runs not written ({why}); saved for the next run"

    if not SANDBOX and not DB_PATH.exists():
        return park(f"database not found at {DB_PATH}")
    for attempt in range(DB_LOCK_TRIES):
        try:
            con = duckdb.connect(str(DB_PATH))
        except duckdb.IOException as e:
            if attempt + 1 < DB_LOCK_TRIES:
                log(f"database busy ({str(e)[:80]}); retrying in {DB_LOCK_WAIT}s")
                time.sleep(DB_LOCK_WAIT)
                continue
            return park(f"database stayed locked: {e}")
        try:
            con.execute(JOB_RUNS_DDL)
            con.execute("BEGIN")
            con.executemany(f"INSERT INTO job_runs ({', '.join(cols)}) "
                            f"VALUES ({', '.join('?' * len(cols))})",
                            [[r[c] for c in cols] for r in all_rows])
            con.execute("COMMIT")
        finally:
            con.close()
        for b in backlog:
            b.unlink()
        return None
    return park("unknown")


def write_status(results, warnings, started, finished, preflight_problems=None):
    """Write the plain-English ETF_ARCHIVER_STATUS.txt summary."""
    lines = [f"ETF ARCHIVER - last run {started:%Y-%m-%d %H:%M} "
             f"(finished {finished:%H:%M})", ""]
    if preflight_problems:
        lines += ["DID NOT RUN. Nothing was downloaded today yet.", ""]
        lines += [f"  - {p}" for p in preflight_problems]
    else:
        by = {}
        for r in results:
            by.setdefault(r["status"], []).append(r)
        bad = by.get("failed", []) + by.get("blocked", [])
        lines.append("RESULT: " + ("PROBLEMS - see below" if bad else "OK"))
        lines.append("")

        def section(title, key, show):
            rs = by.get(key, [])
            if rs:
                lines.append(f"{title} ({len(rs)}):")
                lines.extend("  " + show(r) for r in rs)
                lines.append("")
        section("Captured today", "captured",
                lambda r: f"{r['item']:5}  holdings as of {r['as_of'] or '?'}")
        section("Unchanged since last capture (nothing new published)", "unchanged",
                lambda r: f"{r['item']:5}  holdings as of {r['as_of'] or '?'}")
        section("Already captured earlier today", "already_captured",
                lambda r: f"{r['item']:5}  {r['detail']}")
        section("BLOCKED by the issuer", "blocked",
                lambda r: f"{r['item']:5}  {r['error']}")
        section("FAILED", "failed", lambda r: f"{r['item']:5}  {r['error']}")
        manual = by.get("manual", [])
        if manual:
            lines.append(f"Manual funds not downloaded today ({len(manual)}):")
            lines.append("  " + ", ".join(r["item"] for r in manual))
            lines.append("  Only if you want them: download the holdings file from the")
            lines.append("  fund's website and save it into")
            lines.append(f"    {INBOX}")
            lines.append("  with the ticker at the start of the file name (e.g.")
            lines.append("  DRAM_holdings.csv), then run the archiver again.")
            lines.append("")
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in warnings)
        lines.append("")
    lines.append(f"Full log: {LOG_FILE}")
    STATUS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_funds(funds_file):
    """Return funds from config.ini, or from a CSV fund list if one is given."""
    if funds_file is None:
        return funds_from_config(read_config())
    with open(funds_file, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("ticker") or "").strip()]
    funds = []
    for r in rows:
        funds.append({
            "ticker": r["ticker"].strip().upper(),
            "issuer": (r.get("issuer") or "").strip().lower(),
            "product_id": (r.get("product_id") or "").strip(),
            "url_template": (r.get("url_template") or "").strip(),
            # older copies of funds.csv called this column "filetype"
            "file_type": (r.get("file_type") or r.get("filetype") or "").strip(),
            "fetch_method": (r.get("fetch_method") or "cffi").strip().lower(),
        })
    return funds


def main(funds_file):
    """Run one capture pass. Exit codes: 0 all automated funds captured or
    unchanged, 1 a fund failed or was blocked, 2 could not start, 3 already running.
    """
    started = datetime.datetime.now()
    run_id = f"{started:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    log("=" * 60)
    log(f"ETF archiver starting for {TODAY}  run {run_id}")
    log(f"python: {sys.executable}")

    problems = preflight(funds_file)
    if problems:
        for p in problems:
            log(f"CANNOT START: {p}")
        write_status([], [], started, datetime.datetime.now(), problems)
        if "duckdb" not in _MISSING:
            write_job_runs([{"job_name": JOB_NAME, "run_id": run_id,
                             "started_at": started, "finished_at": datetime.datetime.now(),
                             "item": "_run", "status": "not_started",
                             "detail": "", "error": " | ".join(problems)}])
        return 2

    lock = acquire_lock()
    if lock is None:
        log("Another archiver run is already in progress. Exiting without changes.")
        return 3
    try:
        rotate_logs()
        funds = read_funds(funds_file)
        log(f"Found {len(funds)} funds.")
        results = []
        for fund in funds:
            t, issuer = fund["ticker"], fund["issuer"]
            t0 = datetime.datetime.now()
            try:
                if issuer == "manual":
                    r = capture_manual(t)
                elif fund["fetch_method"] not in ("", "cffi"):
                    # Only curl_cffi fetching exists; fail loudly rather than
                    # silently fall back if a fund asks for another method.
                    r = result("failed", "", f"fetch_method '{fund['fetch_method']}' "
                               "is not implemented (only 'cffi' is)")
                else:
                    r = capture_automated(t, issuer, fund)
                    if r["status"] != "already_captured":
                        time.sleep(POLITE_DELAY)
            except Exception as e:
                # One fund failing must never stop the others.
                r = result("failed", "", f"{type(e).__name__}: {e}")
                log(traceback.format_exc(limit=2).rstrip())
            r.update(item=t, started_at=t0, finished_at=datetime.datetime.now())
            results.append(r)
            log(f"  {t}: {r['status'].upper()}"
                + (f" - {r['detail']}" if r["detail"] else "")
                + (f" - {r['error']}" if r["error"] else ""))

        warnings = []
        leftover = list((RAW_ROOT / "_incoming").rglob("*.*")) \
            if (RAW_ROOT / "_incoming").exists() else []
        if leftover:
            w = (f"{len(leftover)} downloaded file(s) are waiting in "
                 f"{RAW_ROOT / '_incoming'} after an error. They are kept, not "
                 "lost, but were not filed - check the log.")
            warnings.append(w)
            log(f"WARNING: {w}")
        copied, warn = backup_new_files()
        if warn:
            warnings.append(warn)
            log(f"WARNING: {warn}")
        else:
            log(f"Backup: {copied} new file(s) copied to {BACKUP_ROOT}")

        counts = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        bad = counts.get("failed", 0) + counts.get("blocked", 0)
        finished = datetime.datetime.now()
        rows = [{"job_name": JOB_NAME, "run_id": run_id,
                 "started_at": r["started_at"], "finished_at": r["finished_at"],
                 "item": r["item"], "status": r["status"],
                 "detail": r["detail"], "error": r["error"]} for r in results]
        rows.append({"job_name": JOB_NAME, "run_id": run_id,
                     "started_at": started, "finished_at": finished,
                     "item": "_run", "status": "problems" if bad else "ok",
                     "detail": ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                               + f"; backup copied {copied}",
                     "error": " | ".join(warnings)})
        warn = write_job_runs(rows)
        if warn:
            warnings.append(warn)
            log(f"WARNING: {warn}")

        write_status(results, warnings, started, finished)
        log("Done. " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        log(f"Status summary: {STATUS_FILE}")
        log("=" * 60)
        return 1 if bad else 0
    finally:
        lock.close()


def check_file(path, issuer):
    """Validate one file and print the verdict; saves nothing."""
    path = pathlib.Path(path)
    if not path.is_file():
        print(f"No such file: {path}")
        return 2
    ext = path.suffix.lower().lstrip(".")
    try:
        df, as_of = validate(path, ext, issuer)
    except Rejected as e:
        print(f"REJECTED ({'blocked/HTML' if e.blocked else 'invalid'}): {e}")
        return 1
    print(f"ACCEPTED: {len(df)} holdings rows, as-of {as_of or 'not stated'}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Download today's ETF holdings.")
    ap.add_argument("--sandbox", metavar="DIR",
                    help="write every output under DIR instead of the real archive")
    ap.add_argument("--funds-file", metavar="F",
                    help="CSV fund list to use instead of config.ini (testing)")
    ap.add_argument("--check-file", metavar="F",
                    help="validate one file and print the verdict; saves nothing")
    ap.add_argument("--issuer", default="generic", help="issuer for --check-file")
    args = ap.parse_args()
    if args.check_file:
        if _MISSING:
            sys.exit(f"Missing Python libraries: {', '.join(_MISSING)}")
        sys.exit(check_file(args.check_file, args.issuer))
    if args.sandbox:
        configure(args.sandbox)
    try:
        code = main(pathlib.Path(args.funds_file) if args.funds_file else None)
    except Exception:
        # Under pythonw (the scheduled task) there is no console, so an
        # unexpected crash must land in the log or it is invisible.
        log("CRASHED:\n" + traceback.format_exc())
        code = 1
    sys.exit(code)
