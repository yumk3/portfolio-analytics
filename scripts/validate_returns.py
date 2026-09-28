r"""Read-only validation of the returns tables: internal consistency, an independent
SQL recompute, flow completeness, and broker/statement checkpoints. Exit 1 on any FAIL.
Usage: python scripts\validate_returns.py
"""
import csv
import datetime
import os
import pathlib
import sys

import duckdb
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import returns_lib as rl                                     # noqa: E402
import local_inputs                                          # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = pathlib.Path(os.getenv("DB_PATH") or REPO / "data" / "portfolio.duckdb")
if not DB_PATH.is_absolute():
    DB_PATH = REPO / DB_PATH
# Overridable so the checks can be exercised with sample checkpoint files.
BROKER_CSV = pathlib.Path(os.getenv("RETURNS_BROKER_CSV")
                          or REPO / "inputs" / "broker_return_checkpoints.csv")
STATEMENT_CSV = pathlib.Path(os.getenv("RETURNS_STATEMENT_CSV")
                             or REPO / "inputs" / "statement_gain_checkpoints.csv")

RATE_TOL = 1e-9            # 1a, 1c: identical formulas must agree to rounding
DOLLAR_TOL = 0.01          # 1b: sums must tie to the cent
UNEXPLAINED_DOLLARS = 0.05 # 2: a day's unexplained value move worth flagging
MATCH_PP = 0.10            # 3a: within 0.1 percentage points
MATCH_DOLLARS = 0.10       # 3b: within 10 cents
VALUE_TOL_PCT = 0.25       # 3b: endpoint value gap allowed (validate_valuation check 10)
RESIDUALS = tuple(local_inputs.accepted_exceptions("valuation"))
ONE_DAY = datetime.timedelta(days=1)

results = []               # (id, name, status, one-line meaning)


def report(cid, name, status, meaning, details=()):
    results.append((cid, name, status, meaning))
    print(f"\n[{status}] {cid}  {name}\n  {meaning}")
    for d in details:
        print(f"    {d}")


def read_checkpoints(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        lines = [l for l in f if l.strip() and not l.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def load(con):
    daily = con.execute("SELECT * FROM returns_daily ORDER BY account, date").df()
    summary = con.execute("SELECT * FROM returns_summary").df()
    pos = con.execute("SELECT * FROM position_returns").df()
    pvd = con.execute("""SELECT date, account_id, symbol, quantity::DOUBLE q, price::DOUBLE p,
                                market_value::DOUBLE mv FROM positions_value_daily""").df()
    txns = con.execute("""SELECT transaction_id, account_id, symbol, txn_type, quantity, amount,
                                 trade_date FROM transactions WHERE trade_date IS NOT NULL""").df()
    opening = con.execute("SELECT account_id, symbol, as_of_date, units FROM opening_balances").df()
    splits = con.execute("SELECT symbol, split_date, ratio FROM splits").df()
    for df, c in ((daily, "date"), (pvd, "date"), (txns, "trade_date"),
                  (opening, "as_of_date"), (splits, "split_date")):
        df[c] = pd.to_datetime(df[c]).dt.date
    return daily, summary, pos, pvd, txns, opening, splits


def excluded_symbols(pvd, txns):
    return set(pvd.loc[pvd["p"].isna() | pvd["mv"].isna(), "symbol"]) | \
        (set(txns["symbol"].dropna()) - set(pvd["symbol"]))


def check_1a(daily, summary):
    """Check 1a: in windows with no flows, TWR must equal money-weighted return."""
    diffs, n = [], 0
    for acct, g in daily.groupby("account"):
        g = g.set_index("date")
        months = sorted({(d.year, d.month) for d in g.index})
        for y, m in months:
            start = datetime.date(y, m, 1) - ONE_DAY
            end = (datetime.date(y + m // 12, m % 12 + 1, 1) - ONE_DAY)
            if start not in g.index or end not in g.index:
                continue
            w = g.loc[(g.index > start) & (g.index <= end)]
            vs = g.at[start, "end_value"]
            if vs <= 0 or (w["status"] != "ok").any() or \
                    (w[["inflows", "outflows", "income"]].abs().sum().sum() > 0):
                continue
            twr = g.at[end, "twr_index"] / g.at[start, "twr_index"] - 1
            _, mwr, _ = rl.money_weighted([(start, -vs), (end, g.at[end, "end_value"])],
                                          (end - start).days)
            diffs.append((acct, f"{y}-{m:02d}", abs(twr - mwr)))
            n += 1
    no_flow_rows = summary[(summary[["inflows", "outflows", "income"]].abs().sum(axis=1) == 0)
                           & (summary["start_value"] > 0) & summary["mwr_period"].notna()]
    for r in no_flow_rows.itertuples():
        diffs.append((r.account, r.period, abs(r.twr - r.mwr_period)))
        n += 1
    worst = max(diffs, key=lambda x: x[2]) if diffs else None
    ok = worst is None or worst[2] < RATE_TOL
    report("1a", "No-flow periods: time-weighted = money-weighted",
           "PASS" if ok else "FAIL",
           f"{n} windows with no money moving in or out (every such calendar month, plus "
           f"{len(no_flow_rows)} summary periods); the two methods must agree exactly.",
           [f"largest difference: {worst[2]:.2e} ({worst[0]} {worst[1]})" if worst else "no windows"])


def check_1b(summary, pos):
    """Check 1b: per-position dollar gains must sum to the portfolio's (headline and all-in)."""
    si = summary[summary["period"] == "SI"].set_index("account")
    lines, bad = [], 0
    for acct in si.index:
        p = pos[pos["account"] == ("COMBINED" if acct == "TOTAL" else acct)]
        priced = p[~p["flags"].str.contains("unpriced", na=False)]["dollar_gain"].sum()
        allin = p["dollar_gain"].sum()
        d1 = priced - si.at[acct, "dollar_gain"]
        d2 = allin - si.at[acct, "all_in_dollar_gain"]
        bad += abs(d1) > DOLLAR_TOL or abs(d2) > DOLLAR_TOL
        lines.append(f"{acct:22} positions ${priced:9.2f} vs portfolio ${si.at[acct, 'dollar_gain']:9.2f}"
                     f" (diff {d1:+.4f}) | all-in ${allin:9.2f} vs ${si.at[acct, 'all_in_dollar_gain']:9.2f}"
                     f" (diff {d2:+.4f})")
    report("1b", "Per-position dollar gains add up to the portfolio's",
           "PASS" if not bad else "FAIL",
           "Since inception, the sum of every position's dollar gain must equal the account's "
           "dollar gain, both for the headline (priced) and the all-in figure.", lines)


# Check 1c recomputes daily returns in SQL from the source tables - a separate
# code path from compute_returns, so a bug cannot hide in both.
RECOMPUTE_SQL = """
WITH excl AS (
    SELECT DISTINCT symbol FROM positions_value_daily WHERE price IS NULL OR market_value IS NULL
    UNION
    SELECT DISTINCT symbol FROM transactions
    WHERE symbol IS NOT NULL AND symbol NOT IN (SELECT symbol FROM positions_value_daily)),
v AS (
    SELECT account_id, date, sum(market_value)::DOUBLE AS v FROM positions_value_daily
    WHERE symbol NOT IN (SELECT symbol FROM excl) GROUP BY ALL),
f0 AS (
    SELECT account_id, trade_date AS date,
           CASE WHEN txn_type IN ('BUY','REI') THEN -amount ELSE 0 END AS i,
           CASE WHEN txn_type = 'SELL' THEN amount ELSE 0 END AS o,
           CASE WHEN txn_type IN ('DIVIDEND','INTEREST') THEN amount ELSE 0 END AS inc
    FROM transactions WHERE symbol NOT IN (SELECT symbol FROM excl)
    UNION ALL
    SELECT ob.account_id, ob.as_of_date, p.market_value::DOUBLE, 0, 0
    FROM opening_balances ob JOIN positions_value_daily p
      ON p.account_id = ob.account_id AND p.symbol = ob.symbol AND p.date = ob.as_of_date
    WHERE ob.symbol NOT IN (SELECT symbol FROM excl)),
f AS (SELECT account_id, date, sum(i) i, sum(o) o, sum(inc) inc FROM f0 GROUP BY ALL),
lastd AS (SELECT max(date) AS a FROM positions_value_daily),
bounds AS (
    SELECT account_id, min(d) AS d0 FROM (
        SELECT account_id, date AS d FROM v WHERE v <> 0
        UNION ALL SELECT account_id, date FROM f) GROUP BY 1),
spine AS (
    SELECT account_id, CAST(unnest(generate_series(d0, (SELECT a FROM lastd), INTERVAL 1 DAY)) AS DATE) AS date
    FROM bounds),
acct AS (
    SELECT s.account_id, s.date, coalesce(v.v, 0) v, coalesce(f.i, 0) i,
           coalesce(f.o, 0) o, coalesce(f.inc, 0) inc
    FROM spine s LEFT JOIN v USING (account_id, date) LEFT JOIN f USING (account_id, date)),
allr AS (
    SELECT * FROM acct
    UNION ALL
    SELECT 'TOTAL', date, sum(v), sum(i), sum(o), sum(inc) FROM acct GROUP BY date),
lagged AS (
    SELECT *, lag(v, 1, 0) OVER (PARTITION BY account_id ORDER BY date) AS vp FROM allr)
SELECT account_id, date,
       CASE WHEN abs(vp + i) < 1e-9 AND abs(v + o + inc) < 1e-9 THEN 0.0
            WHEN vp + i <= 1e-9 THEN NULL
            ELSE (v + o + inc) / (vp + i) - 1 END AS r
FROM lagged ORDER BY account_id, date
"""


def check_1c(con, daily):
    sql = con.execute(RECOMPUTE_SQL).df()
    sql["date"] = pd.to_datetime(sql["date"]).dt.date
    ours = daily.assign(key=daily["account_id"].fillna("TOTAL"))[
        ["key", "date", "daily_return", "twr_index"]]
    m = sql.merge(ours, left_on=["account_id", "date"], right_on=["key", "date"],
                  how="outer", indicator=True)
    only = m[m["_merge"] != "both"]
    both = m[m["_merge"] == "both"]
    null_mismatch = (both["r"].isna() != both["daily_return"].isna()).sum()
    diff = (both["r"] - both["daily_return"]).abs().max()
    idx_lines, worst_idx = [], 0.0
    for key, g in both.groupby("key"):
        idx = (1 + g.sort_values("date")["r"].fillna(0)).prod()
        stored = g.sort_values("date")["twr_index"].iloc[-1]
        worst_idx = max(worst_idx, abs(idx / stored - 1))
        idx_lines.append(f"{key[:22]:22} final index SQL {idx:.10f} vs stored {stored:.10f}")
    ok = len(only) == 0 and null_mismatch == 0 and diff < RATE_TOL and worst_idx < RATE_TOL
    report("1c", "Daily returns recomputed independently (SQL)",
           "PASS" if ok else "FAIL",
           f"{len(both)} account-days recomputed from the source tables in SQL; every daily "
           "return and the final growth index must match what compute_returns.py stored.",
           [f"days only on one side: {len(only)}; undefined-day mismatches: {null_mismatch}; "
            f"largest daily difference: {diff:.2e}"] + idx_lines)


def check_2(daily, pvd, txns, opening, splits, excluded):
    """Check 2: each day's value = prior units plus recorded trades, at today's prices.

    Values are built from the same transactions, so this cannot detect a trade
    the broker never sent (check 3b and validate_valuation cover that); it
    catches value moving with no price change or recorded trade behind it.
    """
    split_list = {}
    for s in splits.itertuples():
        split_list.setdefault(s.symbol, []).append((s.split_date, float(s.ratio)))

    def factor(sym, d):                 # same rule as rebuild_holdings: splits AFTER d
        f = 1.0
        for sd, ratio in split_list.get(sym, ()):
            if sd > d:
                f *= ratio
        return f

    inc = pvd[~pvd["symbol"].isin(excluded)]
    q = inc.set_index(["account_id", "symbol", "date"])["q"].to_dict()
    p = inc.set_index(["account_id", "symbol", "date"])["p"].to_dict()
    mv = inc.set_index(["account_id", "symbol", "date"])["mv"].to_dict()
    dq, cash = {}, {}                    # (aid, date) -> {sym: units}, {sym: signed cash}
    for t in txns[txns["symbol"].notna() & ~txns["symbol"].isin(excluded)].itertuples():
        cat, _ = rl.classify_flow(t.txn_type, t.amount)
        if cat not in ("inflow", "outflow", "income"):
            continue
        k = (t.account_id, t.trade_date)
        dq.setdefault(k, {}).setdefault(t.symbol, 0.0)
        dq[k][t.symbol] += float(t.quantity or 0.0) * factor(t.symbol, t.trade_date)
        cash.setdefault(k, {}).setdefault(t.symbol, 0.0)
        cash[k][t.symbol] += float(t.amount)
    for o in opening[~opening["symbol"].isin(excluded)].itertuples():
        k = (o.account_id, o.as_of_date)
        dq.setdefault(k, {}).setdefault(o.symbol, 0.0)
        dq[k][o.symbol] += float(o.units) * factor(o.symbol, o.as_of_date)
        cash.setdefault(k, {}).setdefault(o.symbol, 0.0)
        cash[k][o.symbol] -= mv.get((o.account_id, o.symbol, o.as_of_date), 0.0)

    held = {}                            # (aid, date) -> symbols with a valuation row
    for (a, s, d) in q:
        held.setdefault((a, d), set()).add(s)

    rows, flagged = 0, []
    acct_days = daily[daily["account_id"].notna() & (daily["status"] != "empty")]
    for r in acct_days.itertuples():
        a, d, dp = r.account_id, r.date, r.date - ONE_DAY
        syms = held.get((a, d), set()) | held.get((a, dp), set()) | set(dq.get((a, d), {}))
        expected = price_effect = trade_effect = 0.0
        for s in syms:
            q_prev = q.get((a, s, dp), 0.0)
            p_prev = p.get((a, s, dp))
            p_now = p.get((a, s, d), p_prev)         # sold out: no row today
            if p_now is None:
                p_now = 0.0
            change = dq.get((a, d), {}).get(s, 0.0)
            expected += (q_prev + change) * p_now
            price_effect += q_prev * (p_now - (p_prev if p_prev is not None else p_now))
            trade_effect += change * p_now + cash.get((a, d), {}).get(s, 0.0)
        unexplained = r.end_value - expected
        rows += 1
        if abs(unexplained) > UNEXPLAINED_DOLLARS:
            gain = r.end_value - r.start_value - r.inflows + r.outflows + r.income
            flagged.append((abs(unexplained), r.account, d, unexplained, gain,
                            price_effect, trade_effect))
    flagged.sort(reverse=True)
    lines = [f"{rows} account-days checked; threshold ${UNEXPLAINED_DOLLARS:.2f}"]
    lines += [f"{a[:22]:22} {d}  unexplained ${u:+.2f} (day's gain ${g:+.2f} = price ${pe:+.2f}"
              f" + trades/income ${te:+.2f} + unexplained)"
              for _, a, d, u, g, pe, te in flagged[:10]]
    report("2", "Flow completeness: value changes explained by prices and trades",
           "PASS" if not flagged else "WARN",
           "Each day, value = yesterday's units plus today's recorded trades, at today's prices. "
           + ("Nothing moved without a price change or recorded trade behind it."
              if not flagged else f"{len(flagged)} day(s) moved by more than the threshold "
              "with no price change or recorded trade to explain it."),
           lines)


def window(daily, acct, start, end):
    """Return start/end values, TWR, money-weighted return and flows for (start, end]."""
    g = daily[daily["account"] == acct].set_index("date")
    if g.empty or end not in g.index:
        return None
    first = g.index.min()
    in_p = g[(g.index > start) & (g.index <= end)]
    vs = g.at[start, "end_value"] if start in g.index else 0.0
    idx_s = g.at[start, "twr_index"] if start in g.index else 1.0
    flows = [(start, -vs)] if abs(vs) > rl.EPS else []
    for d, r in in_p.iterrows():
        if r["inflows"]:
            flows.append((d, -r["inflows"]))
        if r["outflows"] + r["income"]:
            flows.append((d, r["outflows"] + r["income"]))
    ve = g.at[end, "end_value"]
    flows.append((end, ve))
    days = (end - max(start, first - ONE_DAY)).days
    _, mwr, _ = rl.money_weighted(flows, days)
    return {"vs": vs, "ve": ve, "twr": g.at[end, "twr_index"] / idx_s - 1, "mwr": mwr,
            "in": in_p["inflows"].sum(), "out": in_p["outflows"].sum(),
            "inc": in_p["income"].sum()}


def residual_and_excluded(acct_ids, start, end, pvd, txns, excluded):
    """Dollar effect of the accepted residual lines and of excluded symbols."""
    sel = pvd[pvd["account_id"].isin(acct_ids) & pvd["symbol"].isin(RESIDUALS)]
    mv_s = sel[sel["date"] == start]["mv"].sum()
    mv_e = sel[sel["date"] == end]["mv"].sum()
    t = txns[txns["account_id"].isin(acct_ids) & (txns["trade_date"] > start)
             & (txns["trade_date"] <= end)]
    rf = t[t["symbol"].isin(RESIDUALS) & t["txn_type"].isin(
        ["BUY", "SELL", "REI", "DIVIDEND", "INTEREST"])]["amount"].sum()
    residual_gain = mv_e - mv_s + rf                  # amounts: buys -, sells/income +
    ex_pnl = 0.0
    ex = txns[txns["account_id"].isin(acct_ids) & txns["symbol"].isin(excluded)] \
        .assign(sells_last=lambda d: (d["quantity"].fillna(0) < 0).astype(int)) \
        .sort_values(["trade_date", "sells_last", "transaction_id"])   # buys first in a day
    for _, g in ex.groupby(["account_id", "symbol"]):
        ev, dates = [], []
        for r in g.itertuples():
            cat, _ = rl.classify_flow(r.txn_type, r.amount)
            if cat in ("inflow", "outflow", "income"):
                ev.append((0.0 if cat == "income" else float(r.quantity or 0), float(r.amount)))
                dates.append(r.trade_date)
        ex_pnl += sum(x for d, x in zip(dates, rl.average_cost_realized(ev)) if start < d <= end)
    return residual_gain, mv_s, mv_e, ex_pnl


def check_3a(daily, pvd, txns, excluded, ids):
    """Check 3a: compare broker-reported return percentages, classifying each gap."""
    rows = read_checkpoints(BROKER_CSV)
    if not rows:
        report("3a", "Broker return percentages", "INFO",
               f"No checkpoints entered yet in {BROKER_CSV.name}. If your broker shows no "
               "return % on statements, or uses an unpublished method that includes cash, "
               "check 3b (statement dollars) is the primary broker test.")
        return
    lines, bad = [], 0
    for c in rows:
        acct = c["account"].strip()
        start = datetime.date.fromisoformat(c["period_start"].strip()) - ONE_DAY
        end = datetime.date.fromisoformat(c["period_end"].strip())
        w = window(daily, acct, start, end)
        if w is None:
            lines.append(f"{acct} {c['period_start']}..{c['period_end']}: no data for that end date")
            bad += 1
            continue
        broker = float(c["broker_reported_return"])
        method = c["broker_method"].strip().lower()
        cands = {"twr": [("TWR", w["twr"])], "money-weighted": [("MWR", w["mwr"])]} \
            .get(method, [("TWR", w["twr"]), ("MWR", w["mwr"])])
        acct_ids = ids.get(acct, list(ids.values()))
        acct_ids = acct_ids if isinstance(acct_ids, list) else [acct_ids]
        res_gain, _, _, ex_pnl = residual_and_excluded(acct_ids, start, end, pvd, txns, excluded)
        base = w["vs"] + w["in"]
        res_pp = res_gain / base * 100 if base else 0.0
        ex_pp = ex_pnl / base * 100 if base else 0.0
        verdict, best = "UNEXPLAINED", None
        for name, ours in cands:
            if ours is None:
                continue
            gap = ours * 100 - broker
            adjusted = ours * 100 - res_pp + ex_pp
            if abs(gap) <= MATCH_PP:
                verdict, best = "MATCH", (name, ours, gap)
                break
            if abs(adjusted - broker) <= MATCH_PP and verdict != "MATCH":
                verdict, best = "EXPLAINED", (name, ours, gap)
            elif best is None or abs(gap) < abs(best[2]):
                best = best if verdict == "EXPLAINED" else (name, ours, gap)
        name, ours, gap = best if best else ("-", None, float("nan"))
        parts = [f"accepted residuals {res_pp:+.2f}pp", f"excluded symbols {-ex_pp:+.2f}pp"]
        if w["twr"] is not None and w["mwr"] is not None:
            parts.append(f"method gap TWR-MWR {(w['twr'] - w['mwr']) * 100:+.2f}pp")
        if c["includes_cash"].strip().upper() == "Y":
            parts.append("broker includes cash: cash drag not measurable (no cash data)")
        lines.append(f"{acct[:22]:22} {c['period_start']}..{c['period_end']} broker {broker:+.2f}% "
                     f"({method}) vs ours {name} {ours * 100 if ours is not None else float('nan'):+.2f}%"
                     f" -> gap {gap:+.2f}pp  {verdict}")
        lines.append("      estimated parts: " + "; ".join(parts))
        bad += verdict == "UNEXPLAINED"
    report("3a", "Broker return percentages", "PASS" if not bad else "WARN",
           f"{len(rows)} checkpoint(s): MATCH within {MATCH_PP}pp, EXPLAINED when residuals, "
           "excluded symbols or method account for the gap, otherwise UNEXPLAINED.", lines)


def check_3b(daily, pvd, txns, excluded, ids):
    """Check 3b: compare monthly dollar gains with statement values and activity."""
    rows = read_checkpoints(STATEMENT_CSV)
    if not rows:
        report("3b", "Statement dollar gains", "INFO", f"No rows in {STATEMENT_CSV.name}.")
        return
    lines, counts = [], {}
    hdr = (f"{'account':10} {'month':8} {'our gain':>9} {'stmt gain':>9} {'gap':>7}  "
           f"{'residual':>8} {'values':>7} {'activity':>8}  result")
    lines.append(hdr)
    for c in rows:
        acct = c["account"].strip()
        start = datetime.date.fromisoformat(c["period_start"].strip()) - ONE_DAY
        end = datetime.date.fromisoformat(c["period_end"].strip())
        w = window(daily, acct, start, end)
        if w is None:
            lines.append(f"{acct} {end}: no data for that end date")
            counts["NO DATA"] = counts.get("NO DATA", 0) + 1
            continue
        ours = rl.dollar_gain(w["vs"], w["ve"], w["in"], w["out"], w["inc"])
        s_start, s_end = float(c["statement_value_start"]), float(c["statement_value_end"])
        act = [c[k].strip() for k in ("statement_buys", "statement_sells", "statement_dividends")]
        our_flows = w["in"] + w["out"] + w["inc"]
        prefix = ""
        if all(act):
            buys, sells, divs = (float(x) for x in act)
        elif our_flows == 0:
            buys = sells = divs = 0.0
            prefix = "PRELIM "        # assumes the statement shows no activity either
        else:
            lines.append(f"{acct[10:20]:10} {end:%Y-%m}  our gain {ours:+8.2f}; our records show "
                         f"buys {w['in']:.2f}, sells {w['out']:.2f}, income {w['inc']:.2f} -> "
                         "NEEDS ACTIVITY from the statement")
            counts["NEEDS ACTIVITY"] = counts.get("NEEDS ACTIVITY", 0) + 1
            continue
        stmt = s_end - s_start - buys + sells + divs
        gap = ours - stmt
        acct_ids = [ids[acct]] if acct in ids else list(ids.values())
        res_gain, rs, re_, _ = residual_and_excluded(acct_ids, start, end, pvd, txns, excluded)
        v_start_ex, v_end_ex = w["vs"] - rs, w["ve"] - re_
        values_part = (v_end_ex - s_end) - (v_start_ex - s_start)
        activity_part = -(w["in"] - buys) + (w["out"] - sells) + (w["inc"] - divs)
        residual_part = gap - values_part - activity_part   # = residual value change
        within = all(abs(ours_v - s) <= VALUE_TOL_PCT / 100 * abs(s)
                     for ours_v, s in ((v_start_ex, s_start), (v_end_ex, s_end)))
        if abs(gap) <= MATCH_DOLLARS:
            verdict = "MATCH"
        elif abs(activity_part) <= 0.01 and within:
            verdict = "EXPLAINED"
        else:
            verdict = "UNEXPLAINED"
        verdict = prefix + verdict
        counts[verdict] = counts.get(verdict, 0) + 1
        lines.append(f"{acct[10:20]:10} {end:%Y-%m}  {ours:+9.2f} {stmt:+9.2f} {gap:+7.2f}  "
                     f"{residual_part:+8.2f} {values_part:+7.2f} {activity_part:+8.2f}  {verdict}")
    lines.append("gap = residual (accepted residual lines the statement does not have) + values (our "
                 "month-end values vs the statement's, e.g. price timing) + activity (our "
                 "recorded trades vs the statement's)")
    lines.append(f"MATCH within ${MATCH_DOLLARS:.2f}; EXPLAINED when activity agrees and both "
                 f"month-end values are within {VALUE_TOL_PCT}%; PRELIM = activity columns not "
                 "filled in yet, assumed zero because our records show none that month")
    status = "WARN" if any("UNEXPLAINED" in k or "NEEDS" in k for k in counts) else \
        ("INFO" if any(k.startswith("PRELIM") for k in counts) else "PASS")
    report("3b", "Statement dollar gains", status,
           f"{len(rows)} statement month(s): " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())),
           lines)


def main():
    if not DB_PATH.exists():
        print(f"ERROR: database not found: {DB_PATH}")
        return 2
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        daily, summary, pos, pvd, txns, opening, splits = load(con)
        excluded = excluded_symbols(pvd, txns)
        ids = dict(con.execute("SELECT account_name, account_id FROM accounts").fetchall())
        print(f"Validating returns as of {daily['date'].max()} "
              f"(excluded unpriced symbols: {', '.join(sorted(excluded))})")
        check_1a(daily, summary)
        check_1b(summary, pos)
        check_1c(con, daily)
        check_2(daily, pvd, txns, opening, splits, excluded)
        check_3a(daily, pvd, txns, excluded, ids)
        check_3b(daily, pvd, txns, excluded, ids)
    finally:
        con.close()

    print("\n" + "=" * 78)
    print(f"{'check':6} {'result':7} what it means")
    print("-" * 78)
    for cid, name, status, _ in results:
        print(f"{cid:6} {status:7} {name}")
    fails = sum(1 for r in results if r[2] == "FAIL")
    print("-" * 78)
    print(f"{len(results)} checks: " + ", ".join(
        f"{sum(1 for r in results if r[2] == s)} {s}" for s in ("PASS", "WARN", "INFO", "FAIL")))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
