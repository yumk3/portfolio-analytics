"""Hand-checkable tests for scripts/returns_lib.py, including Excel's XIRR example.

Run: python -m pytest tests -v
"""
import datetime as dt
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import returns_lib as rl                                     # noqa: E402

D = dt.date


def test_twr_vs_money_weighted_divergence():
    """
    Textbook case: $100 grows 10% to $110, then $1,000 is added just before
    a 20% fall. TWR ignores the timing of the deposit: 1.10 x 0.80 - 1 = -12%.
    The money-weighted return is much worse, because most of the money was
    only there for the fall.
    """
    r1, s1 = rl.sub_period_return(v_prev=100, v_now=110)
    r2, s2 = rl.sub_period_return(v_prev=110, v_now=888, inflow=1000)  # 1110 x 0.8
    assert (s1, s2) == ("ok", "ok")
    assert r1 == pytest.approx(0.10)
    assert r2 == pytest.approx(-0.20)
    twr = rl.chain_link([r1, r2])
    assert twr == pytest.approx(-0.12)

    flows = [(D(2024, 1, 1), -100), (D(2024, 7, 1), -1000), (D(2025, 1, 1), 888)]
    mwr, reason = rl.xirr(flows)
    assert reason == "ok"
    assert rl.xnpv(mwr, flows) == pytest.approx(0, abs=1e-6)
    assert mwr < -0.30 < twr          # heavily weighted toward the loss


def test_xirr_matches_excel_documentation_example():
    """Microsoft's XIRR help page example: Excel returns 0.373362535 (37.34%)."""
    flows = [(D(2008, 1, 1), -10000), (D(2008, 3, 1), 2750),
             (D(2008, 10, 30), 4250), (D(2009, 2, 15), 3250), (D(2009, 4, 1), 2750)]
    rate, reason = rl.xirr(flows)
    assert reason == "ok"
    assert rate == pytest.approx(0.373362535, abs=1e-8)


def test_xirr_one_year_exactly_ten_percent():
    """-1000 then +1100 exactly 365 days later: Excel XIRR = 10.0000%."""
    rate, _ = rl.xirr([(D(2019, 1, 1), -1000), (D(2020, 1, 1), 1100)])
    assert rate == pytest.approx(0.10, abs=1e-10)


def test_xirr_leap_year_uses_365_day_convention():
    """366 days apart: Excel gives 1.1 ** (365/366) - 1 = 9.9714%."""
    rate, _ = rl.xirr([(D(2020, 1, 1), -1000), (D(2021, 1, 1), 1100)])
    assert rate == pytest.approx(1.1 ** (365 / 366) - 1, abs=1e-10)


def test_xirr_losing_investment():
    """Half the money lost in one year: -50%."""
    rate, _ = rl.xirr([(D(2019, 1, 1), -1000), (D(2020, 1, 1), 500)])
    assert rate == pytest.approx(-0.5, abs=1e-10)


def test_zero_flow_period_twr_equals_simple_return():
    """No flows: TWR = end / start - 1, and money-weighted agrees with it."""
    values = [100.0, 105.0, 99.75]
    daily = [rl.sub_period_return(a, b)[0] for a, b in zip(values, values[1:])]
    twr = rl.chain_link(daily)
    assert twr == pytest.approx(99.75 / 100 - 1)

    flows = [(D(2024, 3, 1), -100.0), (D(2024, 3, 3), 99.75)]
    ann, period, _ = rl.money_weighted(flows, days=2)
    assert ann is None                          # under a year: not annualized
    assert period == pytest.approx(twr, abs=1e-12)


def test_reinvested_dividend_nets_to_zero_flow():
    """SnapTrade reports e.g. DIVIDEND +0.25 and REI -0.25 on the same day."""
    div_cat, div_amt = rl.classify_flow("DIVIDEND", 0.25)
    rei_cat, rei_amt = rl.classify_flow("REI", -0.25)
    assert (div_cat, rei_cat) == ("income", "inflow")
    assert rl.net_flow(inflow=rei_amt, outflow=0.0, income=div_amt) == pytest.approx(0.0)

    r, _ = rl.sub_period_return(v_prev=100.0, v_now=101.25, inflow=0.25, income=0.25)
    assert r == pytest.approx((101.25 + 0.25) / (100.0 + 0.25) - 1)


def test_classification_of_every_type_in_the_data():
    assert rl.classify_flow("BUY", -40.0) == ("inflow", 40.0)
    assert rl.classify_flow("SELL", 50.0) == ("outflow", 50.0)
    assert rl.classify_flow("CONTRIBUTION", 100.0) == ("ignore", 100.0)
    assert rl.classify_flow("SOMETHING_NEW", 1.0)[0] == "unknown"


def test_closed_position():
    """Buy $100, receive a $2 dividend, sell for $120, nothing left."""
    invested, received, mv = 100.0, 2.0 + 120.0, 0.0
    assert rl.simple_return(invested, received, mv) == pytest.approx(0.22)
    assert rl.dollar_gain(0, mv, invested, 120.0, 2.0) == pytest.approx(22.0)

    flows = [(D(2023, 1, 1), -100.0), (D(2023, 7, 1), 2.0), (D(2024, 1, 1), 120.0)]
    rate, reason = rl.xirr(flows)
    assert reason == "ok"
    assert rl.xnpv(rate, flows) == pytest.approx(0, abs=1e-8)
    assert 0.21 < rate < 0.23          # a little over 22%: the $2 came early


def test_average_cost_realized_pnl():
    """Buy 2 @ 10, buy 2 @ 20 (avg 15), sell 1 @ 30 -> realize 15; dividend 1."""
    events = [(2, -20.0), (2, -40.0), (-1, 30.0), (0, 1.0)]
    assert rl.average_cost_realized(events) == pytest.approx([0, 0, 15.0, 1.0])


def test_no_sign_change_returns_none_with_reason():
    rate, reason = rl.xirr([(D(2024, 1, 1), -100), (D(2024, 6, 1), -50)])
    assert rate is None and "no sign change" in reason
    rate, reason = rl.xirr([(D(2024, 1, 1), 100), (D(2024, 6, 1), 50)])
    assert rate is None and "no sign change" in reason


def test_xirr_never_crashes_on_bad_input():
    assert rl.xirr([])[0] is None
    assert rl.xirr([(D(2024, 1, 1), -100)])[0] is None
    assert rl.xirr([(D(2024, 1, 1), -100), (D(2024, 1, 1), 100)])[0] is None
    assert rl.xirr([(D(2024, 1, 1), "x")])[0] is None   # garbage: reason, not a crash


def test_one_day_loss_solves_and_period_return_is_exact():
    """
    Lost 5% in one day. Annualized that is about -99.99999%, which a naive
    rate search misses. The un-annualized return must be exactly -5%.
    """
    flows = [(D(2024, 1, 2), -100.0), (D(2024, 1, 3), 95.0)]
    ann, period, reason = rl.money_weighted(flows, days=1)
    assert reason == "ok" and ann is None
    assert period == pytest.approx(-0.05, abs=1e-12)
    rate, _ = rl.xirr(flows)
    assert -1 < rate < -0.9999999


def test_multiple_rates_picks_the_one_nearest_ten_percent():
    """
    An oversold position: $20 in, $70 out 3.5 years later, then a tiny
    negative residual value (-$0.10) a year after that. Two rates solve
    this: one near -100% and the sensible one, about +43%/yr. Report the
    sensible one and say that more than one exists.
    """
    flows = [(D(2022, 1, 3), -20.0), (D(2025, 7, 1), 70.0), (D(2026, 7, 1), -0.10)]
    rate, reason = rl.xirr(flows)
    assert 0.35 < rate < 0.50
    assert "rates solve" in reason
    assert rl.xnpv(rate, flows) == pytest.approx(0, abs=1e-8)


def test_empty_and_restart_days():
    """Holding nothing: flat. Restarting from zero with a buy: defined."""
    assert rl.sub_period_return(0.0, 0.0) == (0.0, "empty")
    r, status = rl.sub_period_return(v_prev=0.0, v_now=100.50, inflow=100.00)
    assert status == "ok" and r == pytest.approx(100.50 / 100.00 - 1)
    # value appearing from nothing with no flow cannot be a return
    assert rl.sub_period_return(0.0, 5.0) == (None, "undefined")


def test_annualize_only_for_a_year_or_more():
    assert rl.annualize(0.10, 364) is None
    assert rl.annualize(0.21, 730) == pytest.approx(0.1, abs=1e-3)
    assert rl.period_rate(0.10, 365) == pytest.approx(0.10)
