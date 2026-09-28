"""Rebuild holdings_daily (dense daily units per account and symbol, in today's
share terms) from transactions, splits and opening_balances, then reconcile the
final day against positions. Usage: python scripts/rebuild_holdings.py
"""
import os
import sys
import pathlib
import datetime

import duckdb
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import local_inputs                                          # noqa: E402

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Database: --db, else PORTFOLIO_DB, else DB_PATH (.env), else the default.
# Relative paths resolve against the project root.
DB_PATH = os.getenv("PORTFOLIO_DB") or os.getenv("DB_PATH", "data/portfolio.duckdb")
db_file = pathlib.Path(DB_PATH)
if not db_file.is_absolute():
    db_file = PROJECT_ROOT / db_file

SOURCE = "rebuilt_from_transactions"
# Relative to the largest position ever held, so a large position is not
# excused a large error and a tiny one is not flagged for rounding dust.
REL_TOLERANCE = 0.005
OLD_ABS_TOLERANCE = 0.01   # previous absolute rule, still reported for comparison
EPS = 1e-9

# Reviewed residuals are reported as KNOWN rather than hidden, so a genuinely
# new discrepancy stands out.
ACCEPTED_EXCEPTIONS = local_inputs.accepted_exceptions("rebuild")


def split_factor(splits_by_symbol, symbol, event_date):
    """Return the product of split ratios dated strictly after event_date.

    Scaling every quantity by later splits expresses all history in today's
    share terms (made-up XYZ, 10:1 in 2024: 2 shares bought in 2022 are
    stored as 20 units), so any two dates compare directly; historical rows
    therefore do not match statements of the day.
    """
    factor = 1.0
    for split_date, ratio in splits_by_symbol.get(symbol, ()):
        if split_date > event_date:
            factor *= ratio
    return factor


def main():
    if not db_file.exists():
        print("ERROR: %s not found. Run scripts/create_db.py first." % db_file)
        return 2

    con = duckdb.connect(str(db_file))
    try:
        today = con.execute("SELECT current_date").fetchone()[0]

        splits_by_symbol = {}
        for sym, d, ratio in con.execute(
                "SELECT symbol, split_date, ratio FROM splits "
                "ORDER BY symbol, split_date").fetchall():
            splits_by_symbol.setdefault(sym, []).append((d, float(ratio)))

        events = {}   # (account_id, symbol) -> {date: unit delta in today's terms}

        for aid, sym, d, units in con.execute(
                "SELECT account_id, symbol, as_of_date, units "
                "FROM opening_balances").fetchall():
            adj = float(units) * split_factor(splits_by_symbol, sym, d)
            events.setdefault((aid, sym), {})
            events[(aid, sym)][d] = events[(aid, sym)].get(d, 0.0) + adj

        for aid, sym, d, qty in con.execute(
                "SELECT account_id, symbol, trade_date, quantity "
                "FROM transactions WHERE symbol IS NOT NULL "
                "AND trade_date IS NOT NULL AND quantity IS NOT NULL").fetchall():
            adj = float(qty) * split_factor(splits_by_symbol, sym, d)
            events.setdefault((aid, sym), {})
            events[(aid, sym)][d] = events[(aid, sym)].get(d, 0.0) + adj

        if not events:
            print("No transactions or opening balances to rebuild from.")
            return 0

        # Dense series, zeros included, so a full exit is an explicit 0
        # rather than missing data.
        rows = []
        peak_units = {}
        final_units = {}
        one_day = datetime.timedelta(days=1)
        for key in sorted(events):
            aid, sym = key
            by_date = events[key]
            running = 0.0
            peak = 0.0
            d = min(by_date)
            while d <= today:
                running += by_date.get(d, 0.0)
                if abs(running) < EPS:
                    running = 0.0        # float dust from repeated additions
                rows.append((aid, sym, d, running))
                if running > peak:
                    peak = running
                d += one_day
            peak_units[key] = peak
            final_units[key] = running

        df = pd.DataFrame(rows, columns=["account_id", "symbol",
                                         "as_of_date", "units"])

        before = con.execute("SELECT count(*) FROM holdings_daily").fetchone()[0]

        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("DELETE FROM holdings_daily")
            con.register("rebuilt_df", df)
            con.execute(
                "INSERT INTO holdings_daily "
                "(account_id, symbol, as_of_date, units, source) "
                "SELECT account_id, symbol, as_of_date, units, ? FROM rebuilt_df",
                [SOURCE])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.unregister("rebuilt_df")

        after = con.execute("SELECT count(*) FROM holdings_daily").fetchone()[0]

        print("=" * 78)
        print("REBUILD  (units in today's share terms)")
        print("=" * 78)
        print("  series rebuilt      : %d (account x symbol)" % len(events))
        print("  date range          : %s .. %s"
              % (min(r[2] for r in rows), today))
        print("  holdings_daily rows : %d -> %d" % (before, after))
        print("  splits available    : %d events across %d symbols"
              % (con.execute("SELECT count(*) FROM splits").fetchone()[0],
                 con.execute(
                     "SELECT count(DISTINCT symbol) FROM splits").fetchone()[0]))
        print("  opening balances    : %d"
              % con.execute(
                  "SELECT count(*) FROM opening_balances").fetchone()[0])

        pos = {(r[0], r[1]): float(r[2]) for r in con.execute(
            "SELECT account_id, symbol, quantity FROM positions "
            "WHERE as_of_date = (SELECT max(as_of_date) FROM positions)"
        ).fetchall()}
        names = {r[0]: r[1] for r in con.execute(
            "SELECT account_id, account_name FROM accounts").fetchall()}

        report = []
        for key in sorted(set(final_units) | set(pos)):
            aid, sym = key
            rebuilt = final_units.get(key, 0.0)
            expected = pos.get(key)
            residual = rebuilt - (expected if expected is not None else 0.0)
            peak = peak_units.get(key, 0.0)
            rel_thresh = REL_TOLERANCE * peak
            old_flag = abs(residual) > OLD_ABS_TOLERANCE
            new_flag = abs(residual) > rel_thresh
            report.append((names.get(aid, aid[:8]), sym, rebuilt, expected,
                           residual, peak, rel_thresh, old_flag, new_flag))

        print()
        print("=" * 78)
        print("PER-POSITION COMPARISON  (rebuilt final day vs positions table)")
        print("=" * 78)
        print("  %-22s %-6s %16s %16s %14s  %s" %
              ("ACCOUNT", "SYMBOL", "REBUILT", "POSITIONS", "RESIDUAL", "VERDICT"))
        held = [r for r in report if r[3] is not None]
        for acct, sym, rebuilt, expected, residual, peak, thr, oldf, newf in held:
            print("  %-22s %-6s %16.8f %16.8f %14.8f  %s"
                  % (acct, sym, rebuilt, expected, residual,
                     "MISMATCH" if newf else "match"))
        matched = sum(1 for r in held if not r[8])
        print("\n  positions compared: %d   match: %d   mismatch: %d"
              % (len(held), matched, len(held) - matched))

        print()
        print("=" * 78)
        print("TOLERANCE RULE COMPARISON  (every symbol)")
        print("=" * 78)
        print("  old rule: |residual| > %.2f absolute" % OLD_ABS_TOLERANCE)
        print("  new rule: |residual| > %.1f%% of max units ever held"
              % (REL_TOLERANCE * 100))
        print()
        print("  %-22s %-6s %14s %12s %12s %-8s %-8s %s"
              % ("ACCOUNT", "SYMBOL", "RESIDUAL", "MAX HELD", "NEW THRESH",
                 "OLD", "NEW", "CHANGED"))
        changed, flagged_new = [], []
        for acct, sym, rebuilt, expected, residual, peak, thr, oldf, newf in report:
            ch = "<== CHANGED" if oldf != newf else ""
            if oldf != newf:
                changed.append((acct, sym, residual, peak, thr, oldf, newf))
            if newf:
                flagged_new.append((acct, sym, residual, peak, thr))
            print("  %-22s %-6s %14.8f %12.6f %12.6f %-8s %-8s %s"
                  % (acct, sym, residual, peak, thr,
                     "FLAG" if oldf else "ok", "FLAG" if newf else "ok", ch))

        print()
        print("  symbols flagged under old rule : %d"
              % sum(1 for r in report if r[7]))
        print("  symbols flagged under new rule : %d" % len(flagged_new))
        print("  classification changed         : %d" % len(changed))
        for acct, sym, residual, peak, thr, oldf, newf in changed:
            print("     %-6s residual=%.8f max_held=%.6f new_thresh=%.6f  %s -> %s"
                  % (sym, residual, peak, thr,
                     "FLAG" if oldf else "ok", "FLAG" if newf else "ok"))

        print()
        print("=" * 78)
        print("EXCEPTIONS  (unreconciled under the new rule)")
        print("=" * 78)
        accepted = [x for x in flagged_new if x[1] in ACCEPTED_EXCEPTIONS]
        unexpected = [x for x in flagged_new if x[1] not in ACCEPTED_EXCEPTIONS]

        print("  KNOWN - previously reviewed and accepted (%d)" % len(accepted))
        if accepted:
            for acct, sym, residual, peak, thr in accepted:
                pct = (100 * abs(residual) / peak) if peak else float("inf")
                print("     %-22s %-6s residual=%.8f  (%.2f%% of max held %.6f)"
                      % (acct, sym, residual, pct, peak))
                print("        %s" % ACCEPTED_EXCEPTIONS[sym])
        else:
            print("     none")

        print()
        print("  NEW - not previously seen, needs a decision (%d)" % len(unexpected))
        if unexpected:
            for acct, sym, residual, peak, thr in unexpected:
                pct = (100 * abs(residual) / peak) if peak else float("inf")
                print("     %-22s %-6s residual=%.8f  (%.2f%% of max held %.6f)"
                      % (acct, sym, residual, pct, peak))
            print()
            print("  Investigate the above, then either correct the data or add the symbol\n"
                  "  to inputs/accepted_exceptions.local.csv (scope 'rebuild', with a reason),\n"
                  "  following the format in inputs/accepted_exceptions.local.example.csv.")
        else:
            print("     none")

        # An accepted exception that no longer appears means the data changed.
        resolved = [sym for sym in ACCEPTED_EXCEPTIONS
                    if sym not in {x[1] for x in flagged_new}]
        if resolved:
            print()
            print("  RESOLVED - listed as accepted but no longer flagged: %s"
                  % ", ".join(sorted(resolved)))
            print("     The underlying data changed. Remove these from"
                  " ACCEPTED_EXCEPTIONS.")
    finally:
        con.close()
    print("\nDone. Safe to rerun.")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Rebuild holdings_daily from transactions.")
    ap.add_argument("--db", help="DuckDB file to use instead of PORTFOLIO_DB / DB_PATH / "
                                 "data/portfolio.duckdb (relative to the project root)")
    args = ap.parse_args()
    if args.db:
        db_file = pathlib.Path(args.db)
        if not db_file.is_absolute():
            db_file = PROJECT_ROOT / db_file
    sys.exit(main())
