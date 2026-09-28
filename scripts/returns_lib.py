"""Pure return calculations for the securities sleeve (TWR, XIRR, flows,
realized P&L). No I/O; imported by compute_returns and validate_returns.
"""
import datetime
import math

from scipy.optimize import brentq, newton

DAYS_PER_YEAR = 365.0        # Excel XIRR convention
EPS = 1e-9                   # "zero" for dollar amounts


# Flows are relative to the securities sleeve (the data holds no cash).
# Income leaves the sleeve but counts as return, so a reinvested dividend
# (DIVIDEND + REI of equal amount) nets to zero flow. Cash amounts are used
# rather than quantity x price because they are what actually moved.
_TYPE_RULES = {
    "BUY": "inflow",
    "REI": "inflow",           # the buy half of a reinvested dividend
    "SELL": "outflow",
    "DIVIDEND": "income",
    "INTEREST": "income",
    "CONTRIBUTION": "ignore",  # cash into the account, not into securities
    "WITHDRAWAL": "ignore",    # cash out of the account
    "FEE": "ignore",           # charged to cash, not to the sleeve
}


def classify_flow(txn_type, amount):
    """Return (category, positive magnitude) for a transaction.

    category is 'inflow', 'outflow', 'income', 'ignore' or 'unknown'.
    """
    category = _TYPE_RULES.get((txn_type or "").strip().upper(), "unknown")
    return category, abs(float(amount or 0.0))


def net_flow(inflow, outflow, income):
    """Money put into the sleeve minus money taken out, including income."""
    return inflow - outflow - income


def sub_period_return(v_prev, v_now, inflow=0.0, outflow=0.0, income=0.0):
    """Return (r, status) for one day: r = (V_t + Out + Inc) / (V_t-1 + In) - 1.

    Inflows are treated as arriving at the open (exposed to the day's move)
    and outflows as leaving at the close; unlike an all-end-of-day
    convention this stays defined when the sleeve starts from zero.
    status is 'ok', 'empty' (nothing held, no flows; r = 0.0) or
    'undefined' (value with nothing invested; r = None).
    """
    denom = v_prev + inflow
    numer = v_now + outflow + income
    if abs(denom) < EPS and abs(numer) < EPS:
        return 0.0, "empty"
    if denom <= EPS:
        return None, "undefined"
    return numer / denom - 1.0, "ok"


def chain_link(returns):
    """Chain daily returns into a cumulative return; None counts as 0."""
    growth = 1.0
    for r in returns:
        if r is not None:
            growth *= 1.0 + r
    return growth - 1.0


def annualize(cumulative, days):
    """Annualize a cumulative return; None for periods under a year."""
    if cumulative is None or days < DAYS_PER_YEAR or cumulative <= -1.0:
        return None
    return (1.0 + cumulative) ** (DAYS_PER_YEAR / days) - 1.0


def xnpv(rate, flows):
    """Net present value of dated flows, Excel XNPV convention."""
    if rate <= -1.0:
        return float("nan")
    d0 = flows[0][0]
    return sum(a / (1.0 + rate) ** ((d - d0).days / DAYS_PER_YEAR) for d, a in flows)


def _xnpv_log(g, flows):
    """XNPV in terms of g = ln(1 + rate), which is defined for every g."""
    d0 = flows[0][0]
    return sum(a * math.exp(-g * (d - d0).days / DAYS_PER_YEAR) for d, a in flows)


# Excel's starting guess; with several roots, the nearest one is reported.
_EXCEL_GUESS = math.log1p(0.10)
_G_GRID = [x / 4.0 for x in range(-120, 81)]     # g from -30 to +20 in 0.25 steps


def solve_log_rate(flows):
    """Solve XNPV = 0 for g = ln(1 + annual rate); return (g or None, reason).

    Solving in g keeps very short holdings tractable, where the annual rate
    is within a hair of -100% or astronomically large. Every sign-change
    bracket is solved with Brent's method and the root nearest Excel's 10%
    guess is kept; Newton's method is a fallback, accepted only if it truly
    zeroes XNPV. Never raises.
    """
    try:
        flows = sorted(((d, float(a)) for d, a in flows if abs(float(a)) > EPS),
                       key=lambda x: x[0])
        if len(flows) < 2:
            return None, "fewer than two non-zero flows"
        if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
            return None, "no sign change in flows (all in or all out)"
        if (flows[-1][0] - flows[0][0]).days == 0:
            return None, "all flows on the same day"

        f = lambda g: _xnpv_log(g, flows)
        vals = []
        for g in _G_GRID:
            try:
                v = f(g)
            except OverflowError:
                continue
            if math.isfinite(v):
                vals.append((g, v))
        roots = []
        for (g1, v1), (g2, v2) in zip(vals, vals[1:]):
            if v1 == 0:
                roots.append(g1)
            elif v1 * v2 < 0:
                roots.append(brentq(f, g1, g2, xtol=1e-14, rtol=1e-14, maxiter=500))
        if roots:
            best = min(roots, key=lambda g: abs(g - _EXCEL_GUESS))
            if len(roots) > 1:
                return best, (f"ok ({len(roots)} rates solve these flows; "
                              "reported the one nearest 10%)")
            return best, "ok"
        try:
            g = newton(f, _EXCEL_GUESS, maxiter=200)
            if math.isfinite(g) and abs(f(g)) < 1e-6 * max(abs(a) for _, a in flows):
                return float(g), "ok (newton fallback)"
        except (RuntimeError, OverflowError, ZeroDivisionError, ArithmeticError):
            pass
        return None, "did not converge (no rate zeroes XNPV)"
    except Exception as e:                       # callers rely on never raising
        return None, f"solver error: {type(e).__name__}: {e}"


def xirr(flows):
    """Return (annual rate or None, reason) for dated flows [(date, amount)].

    Excel XIRR sign convention: negative = paid in, positive = taken out.
    """
    g, reason = solve_log_rate(flows)
    return (None if g is None else math.expm1(g)), reason


def period_rate(annual_rate, days):
    """Convert an annual rate to the return over `days`."""
    if annual_rate is None or annual_rate <= -1.0:
        return None
    return math.expm1(math.log1p(annual_rate) * days / DAYS_PER_YEAR)


def money_weighted(flows, days):
    """Return (annualized or None, period return or None, reason).

    The annualized figure is given only for periods of a year or more.
    """
    g, reason = solve_log_rate(flows)
    if g is None:
        return None, None, reason
    annual = math.expm1(g) if days >= DAYS_PER_YEAR else None
    return annual, math.expm1(g * days / DAYS_PER_YEAR), reason


def simple_return(invested, received, market_value):
    """(received + market_value - invested) / invested; not annualized."""
    if invested is None or invested <= EPS:
        return None
    return (received + market_value - invested) / invested


def dollar_gain(v_start, v_end, inflow, outflow, income):
    """Dollar gain over a period, net of money moved in and out."""
    return v_end - v_start - inflow + outflow + income


def average_cost_realized(events):
    """Return realized P&L per event using average cost.

    events: [(qty_delta, cash)] in date order - buys (qty > 0, cash < 0),
    sells (qty < 0, cash > 0), income (qty 0, cash > 0).
    """
    units, cost, out = 0.0, 0.0, []
    for qty, cash in events:
        if qty > EPS:                     # buy
            units += qty
            cost += -cash
            out.append(0.0)
        elif qty < -EPS:                  # sell
            # Units sold beyond those held have no known cost, so their full
            # proceeds are realized.
            sold = min(-qty, max(units, 0.0))
            basis = cost * (sold / units) if units > EPS else 0.0
            units -= sold
            cost -= basis
            out.append(cash - basis)
        else:                             # income
            out.append(cash)
    return out


def days_between(d1, d2):
    return (d2 - d1).days


def as_date(x):
    if isinstance(x, datetime.datetime):
        return x.date()
    if isinstance(x, datetime.date):
        return x
    return datetime.date.fromisoformat(str(x)[:10])
