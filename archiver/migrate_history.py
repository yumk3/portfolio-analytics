r"""One-off: copy an older ETF capture archive into data\raw\etf_holdings with SHA-256
manifests written before and after; sources come from [migration] in config.ini.
Usage: <your venv>\Scripts\python.exe archiver\migrate_history.py (safe to re-run)
"""
import csv
import ctypes
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
from ctypes import wintypes

import openpyxl

BASE = pathlib.Path(__file__).parent
REPO = BASE.parent
sys.path.insert(0, str(BASE))
from etf_archiver import RAW_ROOT, read_config, read_funds   # noqa: E402

MIGRATION = read_config()["migration"]
PREVIOUS_RAW = pathlib.Path(MIGRATION["previous_raw"])
PREVIOUS_LOG = PREVIOUS_RAW.parent / "archiver_log.txt"
DRYRUN_RAW = pathlib.Path(MIGRATION["dry_run_raw"])
MANUAL_FILE = pathlib.Path(MIGRATION["manual_file"])
MANUAL_TICKER = MIGRATION["manual_ticker"].strip().upper()

MANIFEST_DIR = RAW_ROOT / "_manifest"
STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def issuers():
    """Return {ticker: issuer} from the fund list in config.ini."""
    return {f["ticker"]: f["issuer"] for f in read_funds(None)}


def as_of(path):
    """Return the as-of date stated inside a holdings file, or '' if none."""
    try:
        if path.suffix == ".json":
            return json.loads(path.read_bytes()).get("effectiveDate", "") or ""
        if path.suffix == ".xlsx":
            ws = openpyxl.load_workbook(path, read_only=True).active
            for row in ws.iter_rows(max_row=10, values_only=True):
                for c in row:
                    m = re.search(r"As of (\d{1,2}-\w{3}-\d{4})", str(c or ""))
                    if m:
                        return datetime.datetime.strptime(
                            m.group(1), "%d-%b-%Y").date().isoformat()
    except Exception as e:
        return "unreadable: %s" % type(e).__name__
    return ""


# shutil.copy2 preserves the modified time only; creation time is set via Win32.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE


def copy_creation_time(src, dst):
    """Copy src's creation time onto dst (Windows only)."""
    ns = os.stat(src).st_birthtime_ns if hasattr(os.stat(src), "st_birthtime_ns") \
        else os.stat(src).st_ctime_ns       # st_ctime is creation time on Windows
    ft = ns // 100 + 116444736000000000     # FILETIME: 100 ns ticks since 1601
    h = _k32.CreateFileW(str(dst), 0x100, 0x7, None, 3, 0x80, None)  # WRITE_ATTRIBUTES
    if h in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        c = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
        if not _k32.SetFileTime(h, ctypes.byref(c), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _k32.CloseHandle(h)


def plan():
    """Return one manifest row per source file (hash, size, destination), reading only."""
    iss = issuers()
    rows = []

    def add_capture(src, kind, note=""):
        m = re.fullmatch(r"([A-Z]+)_(\d{4}-\d{2}-\d{2})\.(\w+)", src.name)
        ticker, day, ext = m.groups()
        rows.append({"source_kind": kind, "source_path": str(src),
                     "dest_path": str(RAW_ROOT / day /
                                      f"{ticker}_{iss.get(ticker, 'unknown')}.{ext}"),
                     "ticker": ticker, "download_date": day,
                     "as_of_date": as_of(src), "note": note})

    for src in sorted(PREVIOUS_RAW.glob("*/*.*")):
        add_capture(src, "previous archive")
    for src in sorted(DRYRUN_RAW.glob("*/*.*")):
        add_capture(src, "dry run 2026-09-23 (original script, temp folder)")

    day = datetime.date.fromtimestamp(MANUAL_FILE.stat().st_mtime).isoformat()
    rows.append({"source_kind": "manual download",
                 "source_path": str(MANUAL_FILE),
                 "dest_path": str(RAW_ROOT / day / f"{MANUAL_TICKER}_manual{MANUAL_FILE.suffix}"),
                 "ticker": MANUAL_TICKER, "download_date": day, "as_of_date": "",
                 "note": MIGRATION.get("manual_note", "")})

    rows.append({"source_kind": "legacy log",
                 "source_path": str(PREVIOUS_LOG),
                 "dest_path": str(RAW_ROOT / "_legacy" / "archiver_log.txt"),
                 "ticker": "", "download_date": "", "as_of_date": "",
                 "note": MIGRATION.get("legacy_log_note", "")})

    for r in rows:
        s = pathlib.Path(r["source_path"])
        st = s.stat()
        r["size_bytes"] = st.st_size
        r["sha256"] = sha256(s)
        r["source_modified"] = datetime.datetime.fromtimestamp(
            st.st_mtime).isoformat(timespec="seconds")
    return rows


def write_manifest(rows, name):
    """Write a manifest CSV; refuses to overwrite an existing one."""
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    path = MANIFEST_DIR / name
    cols = list(rows[0].keys())
    for r in rows:
        cols += [k for k in r if k not in cols]
    with open(path, "x", newline="", encoding="utf-8") as f:   # never overwrite
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return path


def main():
    """Hash sources, copy without overwriting, then re-verify; exit 0 only if all match."""
    rows = plan()
    dests = [r["dest_path"] for r in rows]
    if len(set(dests)) != len(dests):
        print("STOPPED: two sources map to the same destination. Nothing copied.")
        return 2

    before = write_manifest(rows, f"migration_{STAMP}_before.csv")
    print(f"manifest written before copying: {before}")

    for r in rows:
        src, dst = pathlib.Path(r["source_path"]), pathlib.Path(r["dest_path"])
        if dst.exists():
            r["action"] = ("already present, identical"
                           if sha256(dst) == r["sha256"]
                           else "CONFLICT - destination differs, left alone")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copy_creation_time(src, dst)
        r["action"] = "copied"

    bad = 0
    for r in rows:
        src, dst = pathlib.Path(r["source_path"]), pathlib.Path(r["dest_path"])
        ok_hash = dst.exists() and sha256(dst) == r["sha256"]
        ok_src = sha256(src) == r["sha256"]        # source untouched?
        ok_time = dst.exists() and int(dst.stat().st_mtime) == int(src.stat().st_mtime)
        r["verified_sha256"] = "OK" if ok_hash else "MISMATCH"
        r["source_unchanged"] = "OK" if ok_src else "CHANGED"
        r["timestamp_kept"] = "OK" if ok_time else "DIFFERS"
        bad += not (ok_hash and ok_src and ok_time)

    after = write_manifest(rows, f"migration_{STAMP}_verified.csv")

    print()
    for r in rows:
        print(f"  {r['verified_sha256']:8} {r['action']:28} "
              f"as-of {r['as_of_date'] or '-':10}  "
              f"{pathlib.Path(r['dest_path']).relative_to(REPO)}")
    print(f"\n{len(rows)} files, {len(rows) - bad} verified, {bad} problems.")
    print(f"verified manifest: {after}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
