"""Load per-user lists (accepted exceptions, reviewed price jumps, opening balances) from
gitignored CSVs under inputs/ (templates: *.example.csv). Paths resolve from this file, so
scheduled runs work from any directory; a missing file yields an empty result.
"""
import csv
import pathlib

INPUTS = pathlib.Path(__file__).resolve().parents[1] / "inputs"
ACCEPTED_EXCEPTIONS_CSV = INPUTS / "accepted_exceptions.local.csv"
REVIEWED_JUMPS_CSV = INPUTS / "reviewed_jumps.local.csv"
OPENING_BALANCES_CSV = INPUTS / "opening_balances.csv"


def _rows(path):
    """Read a CSV into dicts, skipping blank lines and '#' comment lines."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        lines = [l for l in f if l.strip() and not l.lstrip().startswith("#")]
    return [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(lines)]


def accepted_exceptions(scope):
    """Return {symbol: reason} for reviewed residuals, in file order.

    scope is 'valuation' (valuation and returns scripts) or 'rebuild'
    (rebuild_holdings); each has its own reason text.
    """
    return {r["symbol"]: r["reason"] for r in _rows(ACCEPTED_EXCEPTIONS_CSV)
            if r.get("scope") == scope}


def reviewed_jumps():
    """Return {(symbol, 'YYYY-MM-DD'): note} for price jumps confirmed by hand."""
    return {(r["symbol"], r["date"]): r["note"] for r in _rows(REVIEWED_JUMPS_CSV)}


def opening_balances():
    """Return (account_type, symbol, as_of_date, units, source, note) tuples."""
    return [(r["account_type"], r["symbol"], r["as_of_date"], float(r["units"]),
             r["source"], r["note"]) for r in _rows(OPENING_BALANCES_CSV)]
