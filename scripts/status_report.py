"""Write PORTFOLIO_STATUS.txt: one plain-English page on both scheduled jobs.

Built from job_runs (opened read-only), any run records still parked in the
pending folders, and the size of the database backups folder. The ETF archiver
and daily_run run it at the end of every run; it is safe to run by hand. It
never changes the database. Exit codes: 0 written, 1 the file could not be written.
Usage: python scripts\\status_report.py [--db PATH] [--out FILE] [--log-dir DIR] [--by NAME]
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import sys

from dotenv import dotenv_values

SCRIPTS = pathlib.Path(__file__).resolve().parent
REPO = SCRIPTS.parent
ENV_FILE = REPO / ".env"
STATUS_NAME = "PORTFOLIO_STATUS.txt"
COLS = ("job_name", "run_id", "started_at", "finished_at", "item", "status", "detail", "error")

# Must match scripts/register_archiver_task.ps1 (a test checks this).
ARCHIVER_TIMES = ("07:15", "13:00", "18:00")
EARLY = datetime.timedelta(minutes=5)        # a run this early still counts for its slot
LATE = datetime.timedelta(minutes=15)        # later than this is shown as "ran late"
DUE_AFTER = datetime.timedelta(minutes=30)   # a slot with no run is missed this long after it
NIGHTLY_MAX_AGE = datetime.timedelta(hours=26)
DEFAULT_WARN_GB = 2.0
BAD_STATUSES = ("failed", "blocked")
BACKUP_RE = re.compile(r"; backup (\S+\.duckdb)$")


class Unavailable(Exception):
    pass


def hhmm(t):
    return t.strftime("%H:%M")


def stamp(t):
    return t.strftime("%Y-%m-%d %H:%M") if t else "never"


def default_db():
    db = pathlib.Path(os.getenv("DB_PATH") or dotenv_values(ENV_FILE).get("DB_PATH")
                      or "data/portfolio.duckdb")
    return db if db.is_absolute() else REPO / db


def read_job_runs(db):
    """Return job_runs as dicts. Raises Unavailable if the database cannot be read."""
    import duckdb
    if not db.exists():
        raise Unavailable("database not found at %s" % db)
    try:
        con = duckdb.connect(str(db), read_only=True)
    except duckdb.IOException:
        raise Unavailable("database busy; retry after the nightly run")
    try:
        if not con.execute("SELECT count(*) FROM information_schema.tables "
                           "WHERE table_name = 'job_runs'").fetchone()[0]:
            return []
        return [dict(zip(COLS, r)) for r in
                con.execute("SELECT %s FROM job_runs" % ", ".join(COLS)).fetchall()]
    finally:
        con.close()


def read_pending(log_dir):
    """Return (rows, files, unreadable) from both jobs' pending folders."""
    rows, files, unreadable = [], [], []
    for sub in ("job_runs_pending", "daily_run_pending"):
        folder = log_dir / sub
        for f in sorted(folder.glob("*.json")) if folder.exists() else []:
            files.append(f)
            try:
                for r in json.loads(f.read_text(encoding="utf-8")):
                    d = dict(r) if isinstance(r, dict) else dict(zip(COLS, r))
                    for k in ("started_at", "finished_at"):
                        if isinstance(d.get(k), str):
                            d[k] = datetime.datetime.fromisoformat(d[k])
                    rows.append(d)
            except (OSError, ValueError, TypeError):
                unreadable.append(f.name)
    return rows, files, unreadable


def merge(*sources):
    """Combine row lists, keeping one row per (job_name, run_id, item)."""
    out = {}
    for rows in sources:
        for r in rows:
            out.setdefault((r["job_name"], r["run_id"], r["item"]), r)
    return list(out.values())


def runs_of(rows, job):
    """Return {run_id: {"start", "status", "summary", "error", "rows"}} for one job."""
    runs = {}
    for r in rows:
        if r["job_name"] != job or r["started_at"] is None:
            continue
        run = runs.setdefault(r["run_id"], {"start": r["started_at"], "status": None,
                                            "summary": None, "error": "", "rows": [],
                                            "finish": r["finished_at"]})
        if r["item"] == "_run":
            run.update(start=r["started_at"], finish=r["finished_at"], status=r["status"],
                       summary=r["detail"] or "", error=r["error"] or "")
        else:
            run["start"] = min(run["start"], r["started_at"])
            run["rows"].append(r)
    for run in runs.values():
        run["rows"].sort(key=lambda r: r["started_at"])
        run["status"] = run["status"] or "no summary row"
    return runs


# ------------------------------------------------------------ ETF archiver
def archiver_section(rows, now, attention):
    today = now.date()
    runs = sorted((r for r in runs_of(rows, "etf_archiver").values()
                   if r["start"].date() == today), key=lambda r: r["start"])
    lines = ["ETF ARCHIVER - today (%s)" % today]
    slots = [datetime.datetime.combine(today, datetime.time.fromisoformat(t))
             for t in ARCHIVER_TIMES]
    used = set()
    for i, t in enumerate(slots):
        end = (slots[i + 1] - EARLY if i + 1 < len(slots)
               else datetime.datetime.combine(today + datetime.timedelta(days=1),
                                              datetime.time()))
        hits = [r for r in runs if t - EARLY <= r["start"] < end]
        if hits:
            used.update(id(r) for r in hits)
            cells = ["%s %s  %s" % ("ran late" if r["start"] > t + LATE else "ran",
                                    hhmm(r["start"]), r["status"]) for r in hits]
            lines.append("  %s  %s" % (hhmm(t), "; ".join(cells)))
        elif now >= t + DUE_AFTER:
            lines.append("  %s  MISSED" % hhmm(t))
            attention.append("ETF archiver: %s run MISSED" % hhmm(t))
        else:
            lines.append("  %s  not yet due" % hhmm(t))
    other = [r for r in runs if id(r) not in used]
    if other:
        lines.append("  other runs: " + "; ".join("%s %s" % (hhmm(r["start"]), r["status"])
                                                  for r in other))
    for r in runs:
        if r["status"] == "not_started":
            attention.append("ETF archiver: could not start at %s: %s"
                             % (hhmm(r["start"]), r["error"]))

    funds, manual = {}, []
    for run in runs:
        for r in run["rows"]:
            funds.setdefault(r["item"], []).append((run["start"], r))
    if funds:
        lines.append("")
    for ticker, events in funds.items():
        if all(r["status"] == "manual" for _, r in events):
            manual.append(ticker)
            continue
        cells = []
        for start, r in events:
            note = r["error"] if r["status"] in BAD_STATUSES else (
                r["detail"] if r["status"] == "captured" else "")
            cells.append("%s %s%s" % (hhmm(start), r["status"],
                                      " (%s)" % note[:70] if note else ""))
        start, last = events[-1]
        if last["status"] in BAD_STATUSES:
            attention.append("ETF archiver: %s %s at %s: %s"
                             % (ticker, last["status"], hhmm(start), last["error"]))
            flag = ""
        elif any(r["status"] in BAD_STATUSES for _, r in events):
            flag = "   <- recovered"
        else:
            flag = ""
        lines.append("  %-6s %s%s" % (ticker, " . ".join(cells), flag))
    if manual:
        lines.append("  Manual funds, not downloaded: %s" % ", ".join(manual))
    if not runs:
        lines.append("  No runs recorded today.")
    lines.append("  Details of the last run: ETF_ARCHIVER_STATUS.txt")
    return lines


# ------------------------------------------------------------ nightly pipeline
def step_lines(detail):
    """A step's detail split into its summary and its WARN/FAIL items."""
    parts = (detail or "").split(" | ")
    return parts[0], parts[1:]


def nightly_section(rows, now, attention, warnings):
    runs = runs_of(rows, "daily_run")
    if not runs:
        attention.append("Nightly pipeline: no nightly run recorded yet")
        return ["NIGHTLY PIPELINE - no nightly run recorded yet"]
    run = max(runs.values(), key=lambda r: r["start"])
    summary = run["summary"] or ""
    backup = BACKUP_RE.search(summary)
    summary = BACKUP_RE.sub("", summary)
    lines = ["NIGHTLY PIPELINE - last run %s to %s: %s"
             % (stamp(run["start"]), hhmm(run["finish"]) if run["finish"] else "?",
                run["status"])]
    if now - run["start"] > NIGHTLY_MAX_AGE:
        attention.append("Nightly pipeline: no nightly run since %s" % stamp(run["start"]))
    if run["status"] != "ok":
        attention.append("Nightly pipeline: %s" % (summary or run["status"]))
    lines.append("  Result: %s" % (summary or run["status"]))
    for r in run["rows"]:
        head, items = step_lines(r["detail"])
        lines.append(("  %-19s %-8s %s" % (r["item"], r["status"], head)).rstrip())
        for it in items:
            lines.append("      %s" % it)
            if it.startswith("WARN"):
                warnings.append(it)

    returns = [x for x in runs_of(rows, "compute_returns").values() if x["status"] != "failed"]
    good = max(returns, key=lambda x: x["finish"] or x["start"]) if returns else None
    steps = {r["item"]: r["status"] for r in run["rows"]}
    if steps.get("validate_valuation") == "FAIL":
        before = [x for x in returns if x["start"] < run["start"]]
        prev = max(before, key=lambda x: x["finish"] or x["start"]) if before else None
        lines += ["",
                  "  VALUATION FAIL - what the tables hold now:",
                  "    Valuation tables are from today's failed run.",
                  "    Returns are from the previous good run (computed %s)."
                  % stamp(prev and prev["finish"]),
                  "    Last good state: the start-of-run backup %s."
                  % (backup.group(1) if backup else "(name not recorded)")]
    lines.append("")
    if good:
        detail = BACKUP_RE.sub("", good["summary"] or "").split("; backup ")[0]
        older = good["start"] < run["start"]
        lines.append("  returns %s (computed %s%s)"
                     % (detail, stamp(good["finish"]),
                        ", from the previous good run" if older else ""))
        errs = [e for e in (good["error"] or "").split(" | ") if e]
        lines.append("  returns warnings: %s" % ("; ".join(errs) if errs else "none"))
        warnings.extend(errs)
    else:
        lines.append("  returns: never computed")
    return lines


# ------------------------------------------------------------ backups
def warn_limit(raw):
    """Return (limit in GB, note or None)."""
    if raw is None or str(raw).strip() == "":
        return DEFAULT_WARN_GB, None
    try:
        v = float(raw)
        if v > 0:
            return v, None
    except ValueError:
        pass
    return DEFAULT_WARN_GB, ("BACKUP_WARN_GB=%r is not a positive number; using %g GB"
                             % (raw, DEFAULT_WARN_GB))


def backups_section(folder, warn_gb_raw, attention):
    limit, note = warn_limit(warn_gb_raw)
    if note:
        attention.append(note)
    files = [p for p in folder.rglob("*") if p.is_file()] if folder.exists() else []
    size = sum(p.stat().st_size for p in files)
    gb = size / 1024 ** 3
    shown = folder.relative_to(REPO) if folder.is_relative_to(REPO) else folder
    if gb > limit:
        attention.append("Backups folder is %.1f GB, above the %g GB warning level; "
                         "nothing is deleted automatically" % (gb, limit))
    return ["BACKUPS  %s: %d file(s), %.1f GB  (warning above %g GB, set by BACKUP_WARN_GB "
            "in .env)" % (shown, len(files), gb, limit)]


# ------------------------------------------------------------ the page
def report(db, log_dir, now, by="hand", warn_gb_raw=None):
    """Return the text of PORTFOLIO_STATUS.txt."""
    db, log_dir = pathlib.Path(db), pathlib.Path(log_dir)
    attention, warnings, body = [], [], []
    parked, files, unreadable = read_pending(log_dir)
    try:
        rows = merge(read_job_runs(db), parked)
    except Unavailable as e:
        attention.append("Run history unavailable: %s" % e)
        body += ["Run history unavailable: %s." % e, ""]
    else:
        body += archiver_section(rows, now, attention) + [""]
        body += nightly_section(rows, now, attention, warnings) + [""]
    if files:
        attention.append("%d run record file(s) waiting in %s; they are written on the next "
                         "successful run" % (len(files), log_dir))
    if unreadable:
        attention.append("unreadable run record file(s): %s" % ", ".join(unreadable))
    body += backups_section(db.parent / "backups", warn_gb_raw, attention)

    head = ["PORTFOLIO STATUS - written %s (by %s)" % (stamp(now), by)]
    if attention:
        head.append("OVERALL: ATTENTION - %d item(s)" % len(attention))
        head += ["  - %s" % a for a in attention]
    else:
        head.append("OVERALL: OK")
    if warnings:
        head.append("  (%d warning(s) from the checks are listed below; review each one)"
                    % len(warnings))
    return "\n".join(head + [""] + body) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write PORTFOLIO_STATUS.txt.")
    ap.add_argument("--db", help="DuckDB file (default: DB_PATH from .env)")
    ap.add_argument("--out", help="file to write (default: %s in the repo)" % STATUS_NAME)
    ap.add_argument("--log-dir", help="folder holding the jobs' pending run records "
                                      "(default: logs)")
    ap.add_argument("--by", default="hand", help="which job is writing it")
    args = ap.parse_args(argv)
    db = pathlib.Path(args.db) if args.db else default_db()
    out = pathlib.Path(args.out) if args.out else REPO / STATUS_NAME
    log_dir = pathlib.Path(args.log_dir) if args.log_dir else REPO / "logs"
    warn_gb = os.getenv("BACKUP_WARN_GB") or dotenv_values(ENV_FILE).get("BACKUP_WARN_GB")
    text = report(db, log_dir, datetime.datetime.now(), args.by, warn_gb)
    tmp = out.with_name(out.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, out)            # never a half-written file
    except OSError as e:
        print("ERROR: could not write %s (%s)" % (out, e))
        return 1
    print("Status: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
