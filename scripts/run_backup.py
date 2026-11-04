"""Database backup taken before a script writes; shared by ingest_snaptrade and compute_returns.

Under daily_run.py, the run's start-of-run backup is reused instead of taking a
second copy: PORTFOLIO_RUN_BACKUP names it. It is reused only if it exists in the
same backups folder, carries a daily-run name and is under 24 hours old;
otherwise a fresh backup is taken, so a write is never left without one.
"""
import datetime
import os
import pathlib
import re
import shutil

RUN_BACKUP_ENV = "PORTFOLIO_RUN_BACKUP"
RUN_BACKUP_RE = re.compile(r"^portfolio_daily_(\d{8}_\d{6})\.duckdb$")
MAX_AGE = datetime.timedelta(hours=24)


def run_backup(backup_dir, now=None):
    """Return the daily run's backup if it may be reused, else None."""
    named = os.getenv(RUN_BACKUP_ENV)
    if not named:
        return None
    path = pathlib.Path(named)
    m = RUN_BACKUP_RE.match(path.name)
    if not (m and path.is_file()
            and path.resolve().parent == pathlib.Path(backup_dir).resolve()):
        return None
    taken = datetime.datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
    now = now or datetime.datetime.now()
    return path if datetime.timedelta(0) <= now - taken <= MAX_AGE else None


def backup_before_write(db_path, backup_dir, name):
    """Return (backup path, reused). Copies db_path to backup_dir/name unless reused."""
    reused = run_backup(backup_dir)
    if reused is not None:
        return reused, True
    backup_dir = pathlib.Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / name
    shutil.copy2(db_path, path)
    return path, False
