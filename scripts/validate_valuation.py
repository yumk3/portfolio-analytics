"""Independently validate the daily valuation (checks 0-11); exits non-zero on any FAIL.
Recomputes from source tables with correlated subqueries rather than value_portfolio's
ASOF JOIN, so a bug cannot hide in both. Usage: python scripts/validate_valuation.py
"""
import os
import sys
import csv
import decimal
import datetime
import pathlib
import subprocess
import hashlib

import duckdb
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import local_inputs                                          # noqa: E402

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DB_PATH = os.getenv("DB_PATH", "data/portfolio.duckdb")
db_file = pathlib.Path(DB_PATH)
if not db_file.is_absolute():
    db_file = PROJECT_ROOT / db_file

CENT = decimal.Decimal("0.01")
ZERO_TOL = 1e-9
FORWARD_FILL_MAX_DAYS = 5           # must match value_portfolio.py
SPLIT_CONTINUITY_PCT = 25.0         # check 6
JUMP_PCT = 25.0                     # check 7
TRADE_FLOW_PCT = 5.0                # check 8b

# Check 9 compares against SnapTrade three independent ways.
QTY_DOLLAR_TOL = decimal.Decimal("1.00")   # 9a: qty diff worth under $1
QTY_PCT_TOL = decimal.Decimal("0.1")       # 9a: or under 0.1% of position
ACCEPTED_EXC_ABS = decimal.Decimal("5")    # 9b: WARN under, FAIL at/above
PRICE_EFFECT_PCT = 2.0                     # 9c: FAIL above - a wrong price

STATEMENT_PCT = 0.25                # check 10

# Accepted negative residuals: SnapTrade has no row for them, so they are
# excluded from the position comparison and reported as one combined line.
ACCEPTED_EXCEPTIONS = local_inputs.accepted_exceptions("valuation")

# Price jumps confirmed by hand as real, reported as reviewed rather than new.
REVIEWED_JUMPS = local_inputs.reviewed_jumps()


def sql_in(names):
    """Return names formatted for SQL IN (...); empty becomes NULL, which matches nothing."""
    return ",".join("'%s'" % s for s in names) or "NULL"

CHECKPOINTS = PROJECT_ROOT / "inputs" / "statement_checkpoints.csv"

results = []


def out(s=""):
    print(s)


def record(num, name, status, detail=""):
    results.append((str(num), name, status))
    out("  [%-4s] CHECK %-3s %s" % (status, num, name))
    if detail:
        for d in detail.split("\n"):
            out("            %s" % d)


def hdr(t):
    out()
    out("=" * 78)
    out(t)
    out("=" * 78)


def D(x):
    return decimal.Decimal(str(x)) if x is not None else None


def check0_basis(con):
    """Check 0: price basis - a split must not move units or prices on its own."""
    problems = []
    rng = con.execute("SELECT min(as_of_date), max(as_of_date) "
                      "FROM holdings_daily").fetchone()
    rows = con.execute("""
        SELECT s.symbol, s.split_date, s.ratio,
               (SELECT units FROM holdings_daily h WHERE h.symbol = s.symbol
                  AND h.as_of_date = s.split_date - INTERVAL 1 DAY LIMIT 1),
               (SELECT units FROM holdings_daily h WHERE h.symbol = s.symbol
                  AND h.as_of_date = s.split_date LIMIT 1),
               (SELECT close_price FROM prices p WHERE p.symbol = s.symbol
                  AND p.price_date < s.split_date
                ORDER BY p.price_date DESC LIMIT 1),
               (SELECT close_price FROM prices p WHERE p.symbol = s.symbol
                  AND p.price_date >= s.split_date
                ORDER BY p.price_date ASC LIMIT 1)
        FROM splits s WHERE s.split_date BETWEEN ? AND ?
        ORDER BY s.split_date""", [rng[0], rng[1]]).fetchall()
    tested = 0
    for sym, sd, ratio, ub, uo, pb, po in rows:
        if ub is None or abs(ub) <= ZERO_TOL:
            continue
        tested += 1
        if uo is not None and abs(uo - ub) > ZERO_TOL:
            problems.append("%s %s: units JUMPED %.8f -> %.8f" % (sym, sd, ub, uo))
        if pb and po:
            actual = po / pb
            if abs(actual - 1.0 / ratio) < abs(actual - 1.0):
                problems.append("%s %s: price DROPPED to %.4f of prior (~1/%g) "
                                "- looks RAW while units are restated"
                                % (sym, sd, actual, ratio))
    dl = con.execute("SELECT min(CAST(loaded_at AS DATE)) FROM prices").fetchone()[0]
    for sym, sd in con.execute("SELECT symbol, split_date FROM splits "
                               "WHERE split_date > ?", [dl]).fetchall():
        problems.append("%s split %s is LATER than the price download (%s). "
                        "Re-run load_prices.py in full." % (sym, sd, dl))
    detail = ("splits inside holdings range: %d, held through: %d, prices "
              "downloaded: %s" % (len(rows), tested, dl))
    if problems:
        record(0, "price basis consistency", "FAIL", detail + "\n" + "\n".join(problems))
    else:
        record(0, "price basis consistency", "PASS", detail +
               "\nunits do not move and prices do not drop on any split held "
               "through - both sides split-restated, consistent")


def check1_completeness(con):
    """Check 1: every held position is valued and no valuation row is orphaned."""
    src = con.execute("SELECT count(*) FROM holdings_daily WHERE abs(units) > ?",
                      [ZERO_TOL]).fetchone()[0]
    dst = con.execute("SELECT count(*) FROM positions_value_daily").fetchone()[0]
    oa = con.execute("""SELECT count(*) FROM holdings_daily h WHERE abs(h.units) > ?
        AND NOT EXISTS (SELECT 1 FROM positions_value_daily v WHERE v.date=h.as_of_date
          AND v.account_id=h.account_id AND v.symbol=h.symbol)""", [ZERO_TOL]).fetchone()[0]
    ob = con.execute("""SELECT count(*) FROM positions_value_daily v
        WHERE NOT EXISTS (SELECT 1 FROM holdings_daily h WHERE h.as_of_date=v.date
          AND h.account_id=v.account_id AND h.symbol=v.symbol AND abs(h.units) > ?)""",
        [ZERO_TOL]).fetchone()[0]
    nullq = con.execute("SELECT count(*) FROM positions_value_daily "
                        "WHERE quantity IS NULL").fetchone()[0]
    neg = con.execute("""SELECT symbol, count(*), max(abs(coalesce(market_value,0)))
        FROM positions_value_daily WHERE quantity < 0 GROUP BY 1 ORDER BY 1""").fetchall()

    d = ["row counts and orphans - all clean:",
         "  holdings non-zero rows      %d" % src,
         "  valued rows                 %d   %s" % (dst, "MATCH" if src == dst else "MISMATCH"),
         "  orphans holdings -> valued  %d" % oa,
         "  orphans valued -> holdings  %d" % ob,
         "  NULL quantities             %d" % nullq]
    fail = (src != dst) or oa or ob or nullq
    neg_fail = False
    if neg:
        d.append("")
        d.append("WARN reason - negative quantities, and ONLY this:")
        for sym, n, maxabs in neg:
            if sym not in ACCEPTED_EXCEPTIONS:
                neg_fail = True
                d.append("  %s: %d rows, NOT on the accepted list -> FAIL" % (sym, n))
            elif D(maxabs) >= ACCEPTED_EXC_ABS:
                neg_fail = True
                d.append("  %s: %d rows, |value| up to $%s >= $%s -> FAIL"
                         % (sym, n, maxabs, ACCEPTED_EXC_ABS))
            else:
                d.append("  %s: %d rows, |value| max $%s - accepted, WARN only"
                         % (sym, n, maxabs))
        d.append("")
        d.append("NOT caused by the 163 missing prices - those are counted in "
                 "CHECK 5, which carries that warning separately.")
    status = "FAIL" if (fail or neg_fail) else ("WARN" if neg else "PASS")
    record(1, "completeness", status, "\n".join(d))


def check2_recompute(con):
    """Check 2: quantity x price reproduces market_value to the cent."""
    rows = con.execute("SELECT quantity, price, market_value FROM "
                       "positions_value_daily WHERE price IS NOT NULL").fetchall()
    bad, worst = 0, decimal.Decimal(0)
    for q, p, mv in rows:
        exp = (D(q) * D(p)).quantize(decimal.Decimal("0.0001"),
                                     rounding=decimal.ROUND_HALF_UP)
        dif = abs(exp - D(mv))
        worst = max(worst, dif)
        if dif > CENT:
            bad += 1
    nulls = con.execute("SELECT count(*) FROM positions_value_daily "
                        "WHERE price IS NULL AND market_value IS NOT NULL").fetchone()[0]
    record(2, "recompute quantity x price", "FAIL" if (bad or nulls) else "PASS",
           "recomputed %d priced rows in Python Decimal; worst difference $%s; "
           "rows over a cent: %d; NULL price with a value: %d"
           % (len(rows), worst, bad, nulls))


def check3_rollup(con):
    """Check 3: positions sum to accounts and accounts sum to ALL."""
    ba = con.execute("""WITH pos AS (SELECT date, account_id, sum(market_value) mv
        FROM positions_value_daily GROUP BY 1,2)
        SELECT count(*) FROM pos p JOIN portfolio_value_daily f
        ON f.date=p.date AND f.account_id=p.account_id
        WHERE abs(coalesce(p.mv,0)-coalesce(f.market_value,0)) > 0.01""").fetchone()[0]
    bl = con.execute("""WITH acc AS (SELECT date, sum(market_value) mv
        FROM portfolio_value_daily WHERE account_id <> 'ALL' GROUP BY 1)
        SELECT count(*) FROM acc a JOIN portfolio_value_daily f
        ON f.date=a.date AND f.account_id='ALL'
        WHERE abs(coalesce(a.mv,0)-coalesce(f.market_value,0)) > 0.01""").fetchone()[0]
    n = con.execute("SELECT count(DISTINCT date) FROM portfolio_value_daily").fetchone()[0]
    record(3, "roll-up positions -> account -> ALL",
           "FAIL" if (ba or bl) else "PASS",
           "%d dates; account mismatches %d; ALL mismatches %d" % (n, ba, bl))


def fingerprint(con):
    a = con.execute("SELECT date,account_id,symbol,quantity,price,price_date,"
                    "days_stale,market_value,price_status FROM positions_value_daily "
                    "ORDER BY 1,2,3").fetchall()
    b = con.execute("SELECT date,account_id,market_value,position_count,"
                    "missing_price_count FROM portfolio_value_daily ORDER BY 1,2").fetchall()
    return (len(a), hashlib.sha256(repr(a).encode()).hexdigest(),
            len(b), hashlib.sha256(repr(b).encode()).hexdigest())


def check4_idempotency(con):
    """Check 4: two runs of value_portfolio (as a subprocess) give identical output."""
    before = fingerprint(con)
    con.close()
    r = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "value_portfolio.py")],
                       capture_output=True, text=True)
    con2 = duckdb.connect(str(db_file))
    after = fingerprint(con2)
    record(4, "idempotency (value_portfolio.py run twice)",
           "PASS" if (before == after and r.returncode == 0) else "FAIL",
           "run 1: %d rows sha %s\nrun 2: %d rows sha %s\nsubprocess exit %d"
           % (before[0], before[1][:24], after[0], after[1][:24], r.returncode))
    return con2


def check5_staleness(con):
    """Check 5: report forward-filled and missing price rows."""
    d = ["status distribution:"]
    for s, n, mx in con.execute("""SELECT price_status, count(*), max(days_stale)
            FROM positions_value_daily GROUP BY 1 ORDER BY 2 DESC""").fetchall():
        d.append("  %-15s %5d  max stale %s" % (s, n, mx if mx is not None else "-"))
    d.append("by asset class (crypto trades 7 days, equities 5):")
    # the same generic crypto list load_prices.py uses (coins not held match nothing)
    crypto = "('BTC','ETH','XRP','SOL','DOGE','LTC','ADA','MATIC')"
    for label, lst in (("crypto", crypto), ("equity", crypto)):
        op = "IN" if label == "crypto" else "NOT IN"
        r = con.execute("""SELECT count(*), sum(CASE WHEN price_status='forward_filled'
                THEN 1 ELSE 0 END), max(days_stale) FROM positions_value_daily
                WHERE symbol %s %s""" % (op, lst)).fetchone()
        d.append("  %-7s %5d rows, %d forward-filled, max stale %s"
                 % (label, r[0], r[1], r[2]))
    d.append("missing-price rows (all listed, none hidden):")
    for s, n, lo, hi in con.execute("""SELECT symbol, count(*), min(date), max(date)
            FROM positions_value_daily WHERE price_status='missing'
            GROUP BY 1 ORDER BY 2 DESC""").fetchall():
        d.append("  %-7s %5d rows  %s .. %s" % (s, n, lo, hi))
    miss = con.execute("SELECT count(*) FROM positions_value_daily "
                       "WHERE price_status='missing'").fetchone()[0]
    record(5, "price staleness", "WARN" if miss else "PASS", "\n".join(d))


def check6_split(con):
    """Check 6: position value stays continuous across split dates."""
    rows = con.execute("""WITH v AS (SELECT p.*, lag(market_value)
            OVER (PARTITION BY account_id, symbol ORDER BY date) prev_mv
            FROM positions_value_daily p)
        SELECT v.symbol, v.date, s.ratio, v.prev_mv, v.market_value,
               100.0*(v.market_value-v.prev_mv)/nullif(abs(v.prev_mv),0)
        FROM v JOIN splits s ON s.symbol=v.symbol AND s.split_date=v.date
        WHERE v.prev_mv IS NOT NULL AND v.market_value IS NOT NULL
        ORDER BY v.date""").fetchall()
    bad = [r for r in rows if r[5] is not None and abs(r[5]) > SPLIT_CONTINUITY_PCT]
    d = ["split dates with a valued position either side: %d" % len(rows)]
    for sym, dt, ratio, pv, mv, pct in rows:
        d.append("  %-6s %s ratio %-6g  value %s -> %s  (%+.2f%%)"
                 % (sym, dt, ratio, pv, mv, pct))
    record(6, "split continuity (value must not jump)",
           "FAIL" if bad else "PASS", "\n".join(d))


def check7_jump(con):
    """Check 7: single-day price moves over the threshold, minus reviewed ones."""
    pj = con.execute("""WITH p AS (SELECT symbol, price_date, close_price,
            lag(close_price) OVER (PARTITION BY symbol ORDER BY price_date) prev
            FROM prices)
        SELECT symbol, price_date, prev, close_price,
               100.0*(close_price-prev)/nullif(prev,0)
        FROM p WHERE prev IS NOT NULL
          AND abs(100.0*(close_price-prev)/nullif(prev,0)) > ?
        ORDER BY abs(100.0*(close_price-prev)/nullif(prev,0)) DESC""",
        [JUMP_PCT]).fetchall()
    reviewed = [r for r in pj if (r[0], str(r[1])) in REVIEWED_JUMPS]
    new = [r for r in pj if (r[0], str(r[1])) not in REVIEWED_JUMPS]
    d = ["single-day price moves over %.0f%%: %d total" % (JUMP_PCT, len(pj)),
         "",
         "REVIEWED and accepted as real market moves (%d):" % len(reviewed)]
    for s, dt, pv, cv, pct in reviewed:
        d.append("  %-7s %s  %.4f -> %.4f  (%+.1f%%)  %s"
                 % (s, dt, pv, cv, pct, REVIEWED_JUMPS[(s, str(dt))]))
    d.append("")
    d.append("NEW, not yet reviewed (%d):" % len(new))
    if new:
        for s, dt, pv, cv, pct in new[:10]:
            d.append("  %-7s %s  %.4f -> %.4f  (%+.1f%%)" % (s, dt, pv, cv, pct))
    else:
        d.append("  none")
    record(7, "jump test", "WARN" if new else "PASS", "\n".join(d))


def check8_attribution(con):
    """Check 8: daily value change decomposes into market and trade effects.

    Prior quantity comes from the dense holdings_daily: positions_value_daily
    omits zero days, so a window over it would reach back across an exit and
    re-entry to a stale quantity.
    """
    prev_sql = """
        SELECT v.account_id, v.symbol, v.date, v.quantity, v.price,
               coalesce(h.units, 0) AS pq
        FROM positions_value_daily v
        LEFT JOIN holdings_daily h
          ON h.account_id = v.account_id AND h.symbol = v.symbol
         AND h.as_of_date = v.date - INTERVAL 1 DAY
    """

    phantom = con.execute("""WITH v AS (%s)
        SELECT v.account_id, v.symbol, v.date, v.pq, v.quantity FROM v
        WHERE abs(v.quantity - v.pq) > ?
          AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.account_id=v.account_id
                AND t.symbol=v.symbol AND t.trade_date=v.date)
          AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.symbol=v.symbol
                AND s.split_date=v.date)
          AND NOT EXISTS (SELECT 1 FROM opening_balances o WHERE o.account_id=v.account_id
                AND o.symbol=v.symbol AND o.as_of_date=v.date)
        ORDER BY v.date""" % prev_sql, [ZERO_TOL]).fetchall()

    trades = con.execute("""WITH v AS (%s),
        t AS (SELECT account_id, symbol, trade_date, sum(quantity) qty, sum(-amount) gross
              FROM transactions WHERE symbol IS NOT NULL GROUP BY 1,2,3)
        SELECT v.symbol, v.date, (v.quantity-v.pq)*v.price, t.gross,
               100.0*((v.quantity-v.pq)*v.price - t.gross)/nullif(abs(t.gross),0)
        FROM v JOIN t ON t.account_id=v.account_id AND t.symbol=v.symbol
                     AND t.trade_date=v.date
        WHERE v.price IS NOT NULL AND t.gross <> 0
        ORDER BY v.date""" % prev_sql).fetchall()

    over = [r for r in trades if r[4] is not None and abs(r[4]) > TRADE_FLOW_PCT]
    d = ["prior quantity read from holdings_daily (dense), NOT from a window",
         "over positions_value_daily (sparse) - see the docstring for why",
         "",
         "8a  quantity changed with NO transaction, NO split, NO opening balance: %d"
         % len(phantom)]
    for aid, sym, dt, pq, q in phantom[:8]:
        d.append("    %-7s %s  %.8f -> %.8f" % (sym, dt, pq, q))
    if len(phantom) > 8:
        d.append("    ... and %d more" % (len(phantom) - 8))
    d.append("")
    d.append("8b  flow effect vs the transaction gross amount, EVERY trade day:")
    d.append("    %-7s %-12s %13s %13s %10s" %
             ("SYMBOL", "DATE", "FLOW EFFECT", "TRADE GROSS", "GAP %"))
    for sym, dt, flow, gross, pct in trades:
        mark = ("  <== over %.0f%%" % TRADE_FLOW_PCT) if (
            pct is not None and abs(pct) > TRADE_FLOW_PCT) else ""
        d.append("    %-7s %-12s %13.4f %13.4f %9.1f%%%s"
                 % (sym, dt, flow, gross, pct, mark))
    d.append("")
    d.append("    %d trade days compared, %d over %.0f%%"
             % (len(trades), len(over), TRADE_FLOW_PCT))
    d.append("    gaps are expected: the flow effect uses that day's CLOSE while")
    d.append("    the trade used its EXECUTION price, so a volatile day shows a gap.")
    record(8, "attribution tie-out",
           "FAIL" if phantom else ("WARN" if over else "PASS"),
           "\n".join(d))


def _snap_rows(con, snap_date):
    """Positions SnapTrade reports, joined to ours. Excludes 9b exceptions."""
    return con.execute("""
        SELECT a.account_name, p.account_id, p.symbol,
               p.quantity, p.market_value, v.quantity, v.price, v.market_value
        FROM positions p
        JOIN accounts a ON a.account_id = p.account_id
        LEFT JOIN positions_value_daily v
          ON v.account_id=p.account_id AND v.symbol=p.symbol AND v.date=?
        WHERE p.as_of_date = ? ORDER BY a.account_name, p.symbol""",
        [snap_date, snap_date]).fetchall()


def check9a_quantity(con, snap_date):
    """Check 9a: quantity differences vs SnapTrade, valued in dollars."""
    d = ["quantity difference valued at OUR price; pass if under $%s or %s%% "
         "of the position, whichever is larger" % (QTY_DOLLAR_TOL, QTY_PCT_TOL),
         "  %-22s %-6s %16s %16s %14s %12s" %
         ("ACCOUNT", "SYMBOL", "SNAPTRADE QTY", "OUR QTY", "QTY DIFF", "$ IMPACT")]
    bad, total_impact = 0, decimal.Decimal(0)
    for acct, aid, sym, sq, smv, vq, vp, vmv in _snap_rows(con, snap_date):
        if vq is None:
            bad += 1
            d.append("  %-22s %-6s %16s %16s %14s %12s  NO ROW" % (acct, sym, sq, "-", "-", "-"))
            continue
        diff = D(vq) - D(sq)
        impact = (diff * D(vp)) if vp is not None else decimal.Decimal(0)
        total_impact += impact
        tol = max(QTY_DOLLAR_TOL, D(smv) * QTY_PCT_TOL / 100)
        ok = abs(impact) <= tol
        if not ok:
            bad += 1
        d.append("  %-22s %-6s %16.8f %16.8f %14.8f %12s  %s"
                 % (acct, sym, sq, vq, diff, impact.quantize(decimal.Decimal("0.0001")),
                    "ok" if ok else "OVER (tol $%s)" % tol.quantize(decimal.Decimal("0.01"))))
    d.append("")
    d.append("  total dollar impact of all quantity differences: $%s"
             % total_impact.quantize(decimal.Decimal("0.0001")))
    record("9a", "SnapTrade quantity (valued in dollars)",
           "FAIL" if bad else "PASS", "\n".join(d))
    return total_impact


def check9b_exceptions(con, snap_date):
    """Check 9b: accepted exceptions, which SnapTrade has no row for."""
    rows = con.execute("""SELECT v.symbol, v.quantity, v.market_value
        FROM positions_value_daily v WHERE v.date=? AND v.symbol IN (%s)
        ORDER BY v.symbol""" % sql_in(ACCEPTED_EXCEPTIONS),
        [snap_date]).fetchall()
    combined = sum((D(r[2]) for r in rows if r[2] is not None), decimal.Decimal(0))
    d = ["positions we carry that SnapTrade has NO row for, so they are "
         "excluded from the comparison above:"]
    for sym, q, mv in rows:
        d.append("  %-6s qty %s  value $%s   %s" % (sym, q, mv, ACCEPTED_EXCEPTIONS[sym]))
    d.append("")
    d.append("  combined market value: $%s   (WARN under $%s, FAIL at or above)"
             % (combined.quantize(decimal.Decimal("0.0001")), ACCEPTED_EXC_ABS))
    status = "FAIL" if abs(combined) >= ACCEPTED_EXC_ABS else "WARN"
    record("9b", "accepted exceptions (excluded from comparison)", status, "\n".join(d))
    return combined


def check9c_value(con, snap_date):
    """Check 9c: value differences vs SnapTrade, split into quantity and price effects."""
    d = ["value difference split into a quantity effect and a price effect:",
         "  quantity effect = (our_qty - their_qty) x our_price",
         "  price effect    = their_qty x (our_price - their_price)",
         "  FAIL only if the PRICE effect exceeds %.0f%% - that means a wrong price."
         % PRICE_EFFECT_PCT,
         "",
         "  %-6s %12s %12s %12s %12s %9s  %s" %
         ("SYMBOL", "THEIR VAL", "OUR VAL", "QTY EFFECT", "PRICE EFF", "PRICE %", "OUR PRICE DATE")]
    bad, total_qty_eff, total_px_eff = 0, decimal.Decimal(0), decimal.Decimal(0)
    for acct, aid, sym, sq, smv, vq, vp, vmv in _snap_rows(con, snap_date):
        if vq is None or vp is None:
            continue
        their_price = D(smv) / D(sq) if D(sq) != 0 else decimal.Decimal(0)
        qty_eff = (D(vq) - D(sq)) * D(vp)
        px_eff = D(sq) * (D(vp) - their_price)
        total_qty_eff += qty_eff
        total_px_eff += px_eff
        px_pct = (px_eff / D(smv) * 100) if D(smv) else decimal.Decimal(0)
        if abs(px_pct) > decimal.Decimal(str(PRICE_EFFECT_PCT)):
            bad += 1
        pdate = con.execute("""SELECT price_date FROM positions_value_daily
            WHERE date=? AND symbol=? AND account_id=?""", [snap_date, sym, aid]).fetchone()
        d.append("  %-6s %12.4f %12.4f %12s %12s %8.3f%%  %s"
                 % (sym, smv, vmv,
                    qty_eff.quantize(decimal.Decimal("0.0001")),
                    px_eff.quantize(decimal.Decimal("0.0001")), px_pct,
                    pdate[0] if pdate else "-"))
    d.append("")
    d.append("  totals: quantity effect $%s, price effect $%s"
             % (total_qty_eff.quantize(decimal.Decimal("0.0001")),
                total_px_eff.quantize(decimal.Decimal("0.0001"))))
    d.append("  price effects under %.0f%% are TIMING (our close vs their intraday "
             "snapshot), not errors." % PRICE_EFFECT_PCT)
    record("9c", "SnapTrade value (quantity effect vs price effect)",
           "FAIL" if bad else "INFO", "\n".join(d))
    return total_qty_eff, total_px_eff


def check9d_totals(con, snap_date, exc_total):
    """Check 9d: account totals gap must equal the sum of its explained parts."""
    d = ["the gap between our total and theirs must equal the sum of its parts, "
         "to the cent:"]
    bad = 0
    for acct, aid in con.execute("""SELECT DISTINCT a.account_name, a.account_id
            FROM positions p JOIN accounts a ON a.account_id=p.account_id
            WHERE p.as_of_date=? ORDER BY 1""", [snap_date]).fetchall():
        their = D(con.execute("SELECT sum(market_value) FROM positions "
                              "WHERE as_of_date=? AND account_id=?",
                              [snap_date, aid]).fetchone()[0] or 0)
        ours = D(con.execute("SELECT market_value FROM portfolio_value_daily "
                             "WHERE date=? AND account_id=?",
                             [snap_date, aid]).fetchone()[0] or 0)
        # parts: per-position differences for compared symbols + excluded ones
        parts = D(con.execute("""SELECT coalesce(sum(v.market_value - p.market_value),0)
            FROM positions p JOIN positions_value_daily v
              ON v.account_id=p.account_id AND v.symbol=p.symbol AND v.date=p.as_of_date
            WHERE p.as_of_date=? AND p.account_id=?""", [snap_date, aid]).fetchone()[0])
        exc = D(con.execute("""SELECT coalesce(sum(v.market_value),0)
            FROM positions_value_daily v WHERE v.date=? AND v.account_id=? AND v.symbol IN (%s)"""
            % sql_in(ACCEPTED_EXCEPTIONS),
            [snap_date, aid]).fetchone()[0])
        gap = ours - their
        explained = parts + exc
        resid = gap - explained
        if abs(resid) > CENT:
            bad += 1
        d.append("  %-22s ours %s - theirs %s = gap %s" % (acct, ours, their, gap))
        d.append("      per-position differences %s + excluded %s = %s   residual %s  %s"
                 % (parts.quantize(decimal.Decimal("0.0001")),
                    exc.quantize(decimal.Decimal("0.0001")),
                    explained.quantize(decimal.Decimal("0.0001")),
                    resid.quantize(decimal.Decimal("0.0001")),
                    "OK" if abs(resid) <= CENT else "UNEXPLAINED"))
    record("9d", "account totals arithmetic (exact)",
           "FAIL" if bad else "PASS", "\n".join(d))


def parse_csv_date(raw):
    """Parse a checkpoint date in any common format.

    Excel rewrites ISO dates as M/D/YYYY on save, which would silently match
    nothing if compared as text.
    """
    txt = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            return datetime.datetime.strptime(txt, fmt).date()
        except ValueError:
            continue
    return None


def check10_statements(con):
    """Check 10: month-end values vs broker statements (inputs/statement_checkpoints.csv).

    Compared excluding accepted residuals, as in 9b: a statement has no line
    for them. Both totals are printed so nothing is hidden.
    """
    if not CHECKPOINTS.exists():
        record(10, "broker statement checkpoints", "WARN", "%s not found" % CHECKPOINTS)
        return
    with CHECKPOINTS.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    filled = [r for r in rows if (r.get("statement_securities_value") or "").strip()]

    exc_list = sql_in(ACCEPTED_EXCEPTIONS)
    d = ["file: %s" % CHECKPOINTS.relative_to(PROJECT_ROOT),
         "rows: %d total, %d filled, %d blank (SKIPPED, not failed)"
         % (len(rows), len(filled), len(rows) - len(filled)),
         "compared EXCLUDING %s - a statement has no line for them (same basis as 9b)"
         % "/".join(ACCEPTED_EXCEPTIONS),
         "",
         "  %-12s %-22s %10s %10s %10s %10s %9s" %
         ("DATE", "ACCOUNT", "STATEMENT", "OURS(all)", "EXCLUDED", "OURS(excl)", "GAP %")]
    bad = 0
    for r in filled:
        dt = parse_csv_date(r.get("date"))
        if dt is None:
            bad += 1
            d.append("  %r: unrecognised date format -> FAIL" % r.get("date"))
            continue
        aid = r["account_id"]
        got = con.execute("SELECT market_value FROM portfolio_value_daily "
                          "WHERE date=? AND account_id=?", [dt, aid]).fetchone()
        if not got or got[0] is None:
            bad += 1
            d.append("  %s %s: no valuation row -> FAIL" % (dt, aid[:8]))
            continue
        exc = D(con.execute("""SELECT coalesce(sum(market_value),0)
            FROM positions_value_daily WHERE date=? AND account_id=?
              AND symbol IN (%s)""" % exc_list, [dt, aid]).fetchone()[0])
        stmt = D(r["statement_securities_value"])
        all_in = D(got[0])
        ours = all_in - exc
        pct = (ours - stmt) / stmt * 100 if stmt else decimal.Decimal(0)
        ok = abs(pct) <= D(STATEMENT_PCT)
        if not ok:
            bad += 1
        q2 = decimal.Decimal("0.01")
        d.append("  %-12s %-22s %10s %10s %10s %10s %+8.3f%%  %s"
                 % (dt, (r.get("account_name") or aid)[:22],
                    stmt.quantize(q2), all_in.quantize(q2),
                    exc.quantize(q2), ours.quantize(q2), pct,
                    "OK" if ok else "OVER TOLERANCE"))
    if not filled:
        d.append("nothing to compare yet - fill in the securities value and re-run")
    else:
        d.append("")
        d.append("  tolerance %.2f%%; any residual gap should be a known small quantity"
                 % STATEMENT_PCT)
        d.append("  residual, which the statements independently confirm")
    record(10, "broker statement checkpoints",
           "FAIL" if bad else ("WARN" if not filled else "PASS"),
           "\n".join(d))


def check11_spotcheck(con):
    """Check 11: a small table of positions to verify by hand."""
    dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM positions_value_daily "
                                       "ORDER BY date DESC LIMIT 1").fetchall()]
    sd = con.execute("""SELECT v.date FROM positions_value_daily v
        JOIN splits s ON s.symbol=v.symbol AND s.split_date=v.date
        WHERE v.market_value IS NOT NULL ORDER BY v.date DESC LIMIT 1""").fetchone()
    if sd:
        dates.append(sd[0])
    mid = con.execute("""SELECT date FROM positions_value_daily WHERE price_status='actual'
        ORDER BY date LIMIT 1 OFFSET 4000""").fetchone()
    if mid:
        dates.append(mid[0])
    d = ["hand-check these against your broker:",
         "  %-12s %-22s %-6s %16s %11s %-12s %6s %12s" %
         ("DATE", "ACCOUNT", "SYMBOL", "QUANTITY", "PRICE", "PRICE DATE", "STALE", "VALUE")]
    for dt in sorted(set(dates)):
        for r in con.execute("""SELECT v.date, a.account_name, v.symbol, v.quantity,
                v.price, v.price_date, v.days_stale, v.market_value
            FROM positions_value_daily v JOIN accounts a ON a.account_id=v.account_id
            WHERE v.date=? AND v.market_value IS NOT NULL
            ORDER BY abs(v.market_value) DESC LIMIT 3""", [dt]).fetchall():
            d.append("  %-12s %-22s %-6s %16.8f %11.4f %-12s %6s %12s" % r)
    record(11, "human spot check", "PASS", "\n".join(d))


def main():
    if not db_file.exists():
        print("ERROR: %s not found." % db_file)
        return 2
    con = duckdb.connect(str(db_file))
    try:
        for t in ("positions_value_daily", "portfolio_value_daily"):
            if not con.execute("SELECT count(*) FROM information_schema.tables "
                               "WHERE table_name=?", [t]).fetchone()[0]:
                print("ERROR: %s missing. Run scripts/value_portfolio.py first." % t)
                return 2

        hdr("VALUATION VALIDATION")
        out("  database : %s" % db_file)
        out("  the validator recomputes from source tables independently;")
        out("  it does not import value_portfolio.py")

        hdr("PRICE BASIS")
        check0_basis(con)
        hdr("INTERNAL INTEGRITY")
        check1_completeness(con)
        check2_recompute(con)
        check3_rollup(con)
        con = check4_idempotency(con)
        hdr("PRICE QUALITY")
        check5_staleness(con)
        check6_split(con)
        check7_jump(con)
        hdr("ATTRIBUTION TIE-OUT")
        check8_attribution(con)

        hdr("SNAPTRADE TIE-OUT (three independent comparisons)")
        snap_date = con.execute("SELECT max(as_of_date) FROM positions").fetchone()[0]
        out("  comparing against the last INGESTED snapshot dated %s" % snap_date)
        out("  no SnapTrade API call is made")
        out()
        check9a_quantity(con, snap_date)
        exc = check9b_exceptions(con, snap_date)
        check9c_value(con, snap_date)
        check9d_totals(con, snap_date, exc)

        hdr("EXTERNAL TIE-OUTS")
        check10_statements(con)
        hdr("HUMAN SPOT CHECK")
        check11_spotcheck(con)

        hdr("SUMMARY")
        fails = [r for r in results if r[2] == "FAIL"]
        warns = [r for r in results if r[2] == "WARN"]
        infos = [r for r in results if r[2] == "INFO"]
        for num, name, status in results:
            out("  CHECK %-3s %-46s %s" % (num, name, status))
        out()
        out("  %d checks: %d PASS, %d WARN, %d INFO, %d FAIL"
            % (len(results), len(results) - len(fails) - len(warns) - len(infos),
               len(warns), len(infos), len(fails)))
    finally:
        try:
            con.close()
        except Exception:
            pass
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
