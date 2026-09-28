r"""Build returns_daily, returns_summary and position_returns (TWR, XIRR, per-position
returns) for the securities sleeve; tables are replaced each run after a DB backup.
Usage: python scripts\compute_returns.py
"""
import datetime
import os
import pathlib
import shutil
import sys
import uuid

import duckdb
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import returns_lib as rl                                     # noqa: E402
import local_inputs                                          # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = pathlib.Path(os.getenv("DB_PATH") or REPO / "data" / "portfolio.duckdb")
if not DB_PATH.is_absolute():
    DB_PATH = REPO / DB_PATH
BACKUP_DIR = DB_PATH.parent / "backups"     # beside whichever database is used
JOB_NAME = "compute_returns"
TOTAL = "TOTAL"
COMBINED = "COMBINED"
# Reviewed residuals are flagged in position_returns, not dropped.
NEGATIVE_RESIDUALS = set(local_inputs.accepted_exceptions("valuation"))
MATERIAL_SHARE = 0.01                      # excluded P&L above this share of gain is noted
ONE_DAY = datetime.timedelta(days=1)

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


def load(con):
    q = lambda s, p=None: con.execute(s, p or []).df()
    accounts = q("SELECT account_id, account_name, account_type FROM accounts")
    pvd = q("""SELECT date, account_id, symbol, quantity::DOUBLE AS quantity,
                      price::DOUBLE AS price, market_value::DOUBLE AS market_value,
                      price_status FROM positions_value_daily""")
    txns = q("""SELECT transaction_id, account_id, symbol, txn_type, quantity,
                       amount, trade_date FROM transactions
                WHERE trade_date IS NOT NULL""")
    opening = q("SELECT account_id, symbol, as_of_date, units FROM opening_balances")
    holdings = q("""SELECT account_id, symbol, as_of_date, units FROM holdings_daily
                    WHERE units <> 0""")
    for df, cols in ((pvd, ["date"]), (txns, ["trade_date"]),
                     (opening, ["as_of_date"]), (holdings, ["as_of_date"])):
        for c in cols:
            df[c] = pd.to_datetime(df[c]).dt.date
    return accounts, pvd, txns, opening, holdings


def excluded_symbols(pvd, txns):
    """Return symbols ever held without a price, or traded but never valued.

    These are excluded from portfolio TWR and XIRR, value and flows alike, so
    a missing price cannot masquerade as a gain or loss.
    """
    unpriced = set(pvd.loc[pvd["price"].isna() | pvd["market_value"].isna(), "symbol"])
    never_valued = set(txns["symbol"].dropna()) - set(pvd["symbol"])
    return sorted(unpriced | never_valued)


def daily_flows(txns, opening, pvd, excluded, warnings):
    """Return {(account_id, date): {inflow, outflow, income}} for included symbols.

    Unknown transaction types are warned about rather than guessed; opening
    balances count as transfers in at that day's market value.
    """
    flows = {}
    def add(aid, d, cat, amt):
        f = flows.setdefault((aid, d), {"inflow": 0.0, "outflow": 0.0, "income": 0.0})
        f[cat] += amt
    for t in txns.itertuples():
        cat, amt = rl.classify_flow(t.txn_type, t.amount)
        if cat == "unknown":
            warnings.append(f"unknown transaction type {t.txn_type!r} "
                            f"({t.transaction_id[:8]}) - left out")
            continue
        if cat == "ignore" or t.symbol in excluded:
            continue
        add(t.account_id, t.trade_date, cat, amt)
    mv = pvd.set_index(["account_id", "symbol", "date"])["market_value"]
    for o in opening.itertuples():
        if o.symbol in excluded:
            continue
        v = mv.get((o.account_id, o.symbol, o.as_of_date))
        if v is None or pd.isna(v):
            warnings.append(f"opening balance {o.symbol} {o.as_of_date} has no market "
                            "value - transfer in not counted")
            continue
        add(o.account_id, o.as_of_date, "inflow", float(v))
    return flows


def build_series(label, aid, values, flows, asof, crypto_ff):
    """Return returns_daily rows for one account (or TOTAL), first activity to asof.

    Days without a value row are 0, so periods holding nothing chain as flat.
    """
    dates = [d for d in values if values[d] != 0] + list(flows)
    if not dates:
        return []
    d, v_prev, index, rows = min(dates), 0.0, 1.0, []
    while d <= asof:
        f = flows.get(d, {"inflow": 0.0, "outflow": 0.0, "income": 0.0})
        v_now = values.get(d, 0.0)
        r, status = rl.sub_period_return(v_prev, v_now, f["inflow"], f["outflow"], f["income"])
        if r is not None:
            index *= 1.0 + r
        note = ""
        if status == "undefined":
            note = "value appeared with nothing invested; day not linked"
        if d in crypto_ff:
            note = "; ".join(filter(None, [note, "crypto price carried forward from "
                                           f"previous day ({', '.join(sorted(crypto_ff[d]))})"]))
        rows.append({"date": d, "account": label, "account_id": aid,
                     "start_value": v_prev, "end_value": v_now,
                     "inflows": f["inflow"], "outflows": f["outflow"],
                     "income": f["income"], "daily_return": r,
                     "twr_index": index, "status": status, "notes": note})
        v_prev = v_now
        d += ONE_DAY
    return rows


def period_starts(asof):
    """Return (period, start) pairs; each start is the close before the period begins."""
    q_month = 3 * ((asof.month - 1) // 3) + 1
    try:
        one_year = asof.replace(year=asof.year - 1)
    except ValueError:                                   # 29 February
        one_year = asof.replace(year=asof.year - 1, day=28)
    return [("1D", asof - ONE_DAY),
            ("MTD", asof.replace(day=1) - ONE_DAY),
            ("QTD", asof.replace(month=q_month, day=1) - ONE_DAY),
            ("YTD", datetime.date(asof.year - 1, 12, 31)),
            ("1Y", one_year),
            ("SI", None)]


def excluded_realized(txns, excluded):
    """Return [(account_id, symbol, date, realized P&L)] for excluded symbols."""
    out = []
    # Buys before sells within a day: transaction ids carry no time order,
    # and a sale cannot precede the purchase it closes.
    ex = txns[txns["symbol"].isin(excluded)].assign(
        sells_last=lambda d: (d["quantity"].fillna(0) < 0).astype(int))         .sort_values(["trade_date", "sells_last", "transaction_id"])
    for (aid, sym), g in ex.groupby(["account_id", "symbol"]):
        events, keep = [], []
        for t in g.itertuples():
            cat, _ = rl.classify_flow(t.txn_type, t.amount)
            if cat in ("inflow", "outflow", "income"):
                events.append((float(t.quantity or 0.0) if cat != "income" else 0.0,
                               float(t.amount)))
                keep.append(t.trade_date)
        for d, pnl in zip(keep, rl.average_cost_realized(events)):
            out.append((aid, sym, d, pnl))
    return out


def summarize(label, aid, rows, realized, touches, asof):
    """Return returns_summary rows for one account (or TOTAL).

    Money-weighted return is annualized only for periods of a year or more.
    """
    if not rows:
        return []
    by_date = {r["date"]: r for r in rows}
    first = rows[0]["date"]
    out = []
    periods = period_starts(asof)
    # Measured from the last day holding nothing, so a long empty stretch and
    # tiny early balances do not dominate the current run.
    empties = [r["date"] for r in rows if r["status"] == "empty"]
    if empties:
        periods.append(("SINCE_RESTART", max(empties)))
    for period, start in periods:
        notes = []
        if period == "SINCE_RESTART":
            notes.append(f"from {start + ONE_DAY}, after the last day holding nothing")
        if start is None or start < first - ONE_DAY:
            if start is not None:
                notes.append(f"account started {first}; measured from then")
            start = first - ONE_DAY
        in_period = [r for r in rows if start < r["date"] <= asof]
        idx_start = by_date[start]["twr_index"] if start in by_date else 1.0
        v_start = by_date[start]["end_value"] if start in by_date else 0.0
        v_end = by_date[asof]["end_value"]
        twr = by_date[asof]["twr_index"] / idx_start - 1.0
        days = (asof - start).days
        inflow = sum(r["inflows"] for r in in_period)
        outflow = sum(r["outflows"] for r in in_period)
        income = sum(r["income"] for r in in_period)

        flows = [(start, -v_start)] if abs(v_start) > rl.EPS else []
        for r in in_period:
            if r["inflows"]:
                flows.append((r["date"], -r["inflows"]))
            if r["outflows"] + r["income"]:
                flows.append((r["date"], r["outflows"] + r["income"]))
        flows.append((asof, v_end))
        mw_ann, mw_period, reason = rl.money_weighted(flows, days)
        if mw_period is None:
            notes.append(f"money-weighted: {reason}")
        elif "rates solve" in reason:
            notes.append(f"money-weighted: {reason}")

        gain = rl.dollar_gain(v_start, v_end, inflow, outflow, income)
        ex_pnl = sum(p for d, p in realized if start < d <= asof)
        ex_syms = sorted({s for d, s in touches if start < d <= asof})
        if ex_syms:
            notes.append(f"excluded unpriced symbols active: {', '.join(ex_syms)}")
        if ex_syms and abs(ex_pnl) > MATERIAL_SHARE * abs(gain):
            notes.append(f"MATERIAL: excluded realized P&L ${ex_pnl:,.2f} is "
                         f"{abs(ex_pnl) / abs(gain) * 100 if gain else float('inf'):.0f}% "
                         "of the period's dollar gain")
        empty = sum(1 for r in in_period if r["status"] == "empty")
        if empty:
            notes.append(f"{empty} day(s) holding nothing counted as flat")
        undefined = sum(1 for r in in_period if r["status"] == "undefined")
        if undefined:
            notes.append(f"{undefined} day(s) not linked (value with nothing invested)")
        ff = [r["date"] for r in in_period if "carried forward" in r["notes"]]
        if ff and (asof in ff or start in ff or period == "1D"):
            notes.append("crypto price carried forward on "
                         + ", ".join(str(d) for d in sorted(set(ff) & {asof, start}) or ff[-1:])
                         + "; return for that day is understated/zero")

        out.append({"account": label, "account_id": aid, "period": period,
                    "start_date": start, "end_date": asof, "days": days,
                    "start_value": v_start, "end_value": v_end,
                    "inflows": inflow, "outflows": outflow, "income": income,
                    "twr": twr, "twr_annualized": rl.annualize(twr, days),
                    "mwr_period": mw_period, "xirr_annualized": mw_ann,
                    "dollar_gain": gain, "excluded_realized_pnl": ex_pnl,
                    "all_in_dollar_gain": gain + ex_pnl,
                    "notes": "; ".join(notes)})
    return out


def position_rows(txns, opening, pvd, holdings, excluded, names, asof):
    mv_asof = (pvd[pvd["date"] == asof].groupby(["account_id", "symbol"])["market_value"]
               .sum(min_count=1).to_dict())
    open_mv = pvd.set_index(["account_id", "symbol", "date"])["market_value"]
    units_asof = (holdings[holdings["as_of_date"] == asof]
                  .groupby(["account_id", "symbol"])["units"].sum().to_dict())
    opened = set(zip(opening["account_id"], opening["symbol"]))

    flows = {}                                 # (aid, sym) -> [(date, amount)]
    for o in opening.itertuples():
        v = open_mv.get((o.account_id, o.symbol, o.as_of_date))
        if v is not None and not pd.isna(v):
            flows.setdefault((o.account_id, o.symbol), []).append((o.as_of_date, -float(v)))
    for t in txns[txns["symbol"].notna()].itertuples():
        cat, amt = rl.classify_flow(t.txn_type, t.amount)
        if cat in ("inflow", "outflow", "income"):
            flows.setdefault((t.account_id, t.symbol), []).append(
                (t.trade_date, -amt if cat == "inflow" else amt))

    def one(label, aid, sym, fl, units, mv, from_opening):
        fl = sorted(fl)
        is_open = abs(units) > rl.EPS
        mv = (mv if mv is not None and not pd.isna(mv) else None) if is_open else 0.0
        invested = -sum(a for _, a in fl if a < 0)
        received = sum(a for _, a in fl if a > 0)
        first = fl[0][0]
        last = asof if is_open else fl[-1][0]
        days = (last - first).days
        flags = []
        if sym in NEGATIVE_RESIDUALS or units < -rl.EPS:
            flags.append("negative_residual")
        if sym in excluded:
            flags.append("unpriced")
        if from_opening:
            flags.append("opening_balance_inferred")
        if days < rl.DAYS_PER_YEAR:
            flags.append("held_under_1y")
        xfl = fl + ([(asof, mv)] if is_open and mv else [])
        mw_ann, mw_period, reason = rl.money_weighted(xfl, days) if days > 0 else \
            (None, None, "bought and sold the same day")
        if mw_period is None:
            flags.append(f"no_mwr: {reason}")
        elif "rates solve" in reason:
            flags.append("multiple_rates_nearest_10pct")
        if is_open and mv is None:
            flags.append("open_but_unpriced")
        return {"account": label, "account_id": aid, "symbol": sym,
                "first_date": first, "last_date": last,
                "status": "open" if is_open else "closed", "units": units,
                "total_invested": invested, "total_received": received,
                "market_value": mv,
                "dollar_gain": (received + (mv or 0.0) - invested),
                "simple_return": rl.simple_return(invested, received, mv or 0.0),
                "mwr_period": mw_period, "xirr_annualized": mw_ann,
                "flags": ", ".join(flags)}

    rows = []
    for (aid, sym), fl in sorted(flows.items()):
        rows.append(one(names.get(aid, aid), aid, sym, fl, units_asof.get((aid, sym), 0.0),
                        mv_asof.get((aid, sym)), (aid, sym) in opened))
    for sym in sorted({s for _, s in flows}):
        keys = [k for k in flows if k[1] == sym]
        fl = [x for k in keys for x in flows[k]]
        mvs = [mv_asof.get(k) for k in keys if abs(units_asof.get(k, 0.0)) > rl.EPS]
        mv = None if any(m is None or pd.isna(m) for m in mvs) else sum(mvs)
        rows.append(one(COMBINED, None, sym, fl, sum(units_asof.get(k, 0.0) for k in keys),
                        mv, any(k in opened for k in keys)))
    return rows


def main():
    started = datetime.datetime.now()
    run_id = f"{started:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    if not DB_PATH.exists():
        print(f"ERROR: database not found: {DB_PATH}")
        return 2
    try:
        con = duckdb.connect(str(DB_PATH), read_only=True)
    except duckdb.IOException as e:
        print(f"ERROR: database is in use by another program ({e}). Try again shortly.")
        return 2
    try:
        accounts, pvd, txns, opening, holdings = load(con)
    finally:
        con.close()
    if pvd.empty:
        print("ERROR: positions_value_daily is empty - run value_portfolio.py first.")
        return 2

    warnings = []
    asof = max(pvd["date"])
    names = dict(zip(accounts["account_id"], accounts["account_name"]))
    crypto_accounts = set(accounts.loc[accounts["account_type"] == "DIGITALASSET", "account_id"])
    excluded = excluded_symbols(pvd, txns)
    included = pvd[~pvd["symbol"].isin(excluded)]

    # A held, included position with no valuation row would silently drop value.
    hv = holdings[~holdings["symbol"].isin(excluded)].merge(
        pvd[["account_id", "symbol", "date"]], how="left", indicator=True,
        left_on=["account_id", "symbol", "as_of_date"], right_on=["account_id", "symbol", "date"])
    holes = hv[(hv["_merge"] == "left_only") & (hv["as_of_date"] <= asof)]
    if len(holes):
        warnings.append(f"{len(holes)} held position-days have no valuation row "
                        f"(e.g. {holes.iloc[0]['symbol']} {holes.iloc[0]['as_of_date']})")

    values = included.groupby(["account_id", "date"])["market_value"].sum().to_dict()
    flows = daily_flows(txns, opening, pvd, excluded, warnings)
    ff = included[(included["price_status"] == "forward_filled")
                  & included["account_id"].isin(crypto_accounts)]
    crypto_ff = {}
    for r in ff.itertuples():
        crypto_ff.setdefault(r.date, set()).add(r.symbol)

    daily, summary = [], []
    realized = excluded_realized(txns, excluded)
    ex_hold = holdings[holdings["symbol"].isin(excluded)]
    for aid in accounts["account_id"]:
        v = {d: x for (a, d), x in values.items() if a == aid}
        f = {d: x for (a, d), x in flows.items() if a == aid}
        rows = build_series(names[aid], aid, v, f, asof,
                            crypto_ff if aid in crypto_accounts else {})
        daily += rows
        touches = [(d, s) for a, s, d, _ in realized if a == aid] + \
                  [(r.as_of_date, r.symbol) for r in ex_hold.itertuples() if r.account_id == aid]
        summary += summarize(names[aid], aid, rows,
                             [(d, p) for a, _, d, p in realized if a == aid], touches, asof)

    tv, tf = {}, {}
    for (a, d), x in values.items():
        tv[d] = tv.get(d, 0.0) + x
    for (a, d), x in flows.items():
        t = tf.setdefault(d, {"inflow": 0.0, "outflow": 0.0, "income": 0.0})
        for k in t:
            t[k] += x[k]
    total_rows = build_series(TOTAL, None, tv, tf, asof, crypto_ff)
    daily += total_rows
    summary += summarize(TOTAL, None, total_rows, [(d, p) for _, _, d, p in realized],
                         [(d, s) for _, s, d, _ in realized]
                         + [(r.as_of_date, r.symbol) for r in ex_hold.itertuples()], asof)

    positions = position_rows(txns, opening, pvd, holdings, excluded, names, asof)

    df_daily = pd.DataFrame(daily)
    df_summary = pd.DataFrame(summary)
    df_pos = pd.DataFrame(positions)
    stamp = datetime.datetime.now()
    for df in (df_daily, df_summary, df_pos):
        df["computed_at"] = stamp

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"portfolio_before_returns_{started:%Y%m%d_%H%M%S}.duckdb"
    shutil.copy2(DB_PATH, backup)
    try:
        con = duckdb.connect(str(DB_PATH))
    except duckdb.IOException as e:
        print(f"ERROR: database is in use by another program ({e}). Nothing written.")
        return 2
    try:
        con.execute("BEGIN")
        for name, df in (("returns_daily", df_daily), ("returns_summary", df_summary),
                         ("position_returns", df_pos)):
            con.register("df_in", df)
            con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM df_in")
            con.unregister("df_in")
        con.execute("COMMIT")
        finished = datetime.datetime.now()
        con.execute(JOB_RUNS_DDL)
        detail = lambda n, df: f"{len(df)} rows, as of {asof}"
        rows = [(JOB_NAME, run_id, started, finished, n, "ok", detail(n, df), "")
                for n, df in (("returns_daily", df_daily), ("returns_summary", df_summary),
                              ("position_returns", df_pos))]
        rows.append((JOB_NAME, run_id, started, finished, "_run",
                     "ok" if not warnings else "warnings",
                     f"as of {asof}; excluded {', '.join(excluded)}; backup {backup.name}",
                     " | ".join(warnings)))
        con.executemany("INSERT INTO job_runs (job_name, run_id, started_at, finished_at, "
                        "item, status, detail, error) VALUES (?,?,?,?,?,?,?,?)", rows)
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()

    pct = lambda x: "      -" if x is None or pd.isna(x) else f"{x * 100:7.2f}%"
    print(f"Returns as of {asof} (securities sleeve only; backup: {backup.name})")
    print(f"Left out of portfolio returns (unpriced): {', '.join(excluded) or 'none'}")
    print(f"\n{'account':22} {'period':6} {'TWR':>8} {'TWR ann':>8} {'MWR':>8} "
          f"{'XIRR ann':>8} {'$ gain':>9} {'excl $':>8}")
    for r in summary:
        print(f"{r['account'][:22]:22} {r['period']:6} {pct(r['twr'])} {pct(r['twr_annualized'])} "
              f"{pct(r['mwr_period'])} {pct(r['xirr_annualized'])} {r['dollar_gain']:9.2f} "
              f"{r['excluded_realized_pnl']:8.2f}")
    und = df_daily[df_daily["status"] == "undefined"]
    print(f"\nreturns_daily {len(df_daily)} rows ({len(und)} undefined days), "
          f"returns_summary {len(df_summary)}, position_returns {len(df_pos)}")
    for w in warnings:
        print(f"WARNING: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
