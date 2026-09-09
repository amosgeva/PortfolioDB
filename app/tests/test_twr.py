"""Time-weighted return tests — hand-verified scenarios.

Pure functions, no DB. The headline check: a contribution must NOT show up as
a return (the bug TWR exists to fix).
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import twr  # noqa: E402

D1, D2, D3, D4 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)

# Buy 10 @100 on D1; +10% to D2; double the position D3 (a contribution, not a
# gain); +10% to D4. True TWR over the window = 1.10 * 1.00 * 1.10 - 1 = 21%.
LOTS = [
    {"symbol": "AAA", "side": "BUY", "trade_date": D1, "quantity": 10.0, "price": 100.0, "fees": 0.0},
    {"symbol": "AAA", "side": "BUY", "trade_date": D3, "quantity": 10.0, "price": 110.0, "fees": 0.0},
]
PRICES = {
    D1: {"AAA": 100.0},
    D2: {"AAA": 110.0},
    D3: {"AAA": 110.0},
    D4: {"AAA": 121.0},
}


def test_contribution_is_not_a_return():
    recs = twr.build_daily_records(LOTS, PRICES, [])
    out = twr.period_returns(recs, today=D4)
    # The D3 doubling must be neutralised: MAX = two +10% legs chained = 21%.
    assert out["MAX"] == pytest.approx(21.0)
    assert out["1D"] == pytest.approx(10.0)        # D3 -> D4
    # 1Y has no observation within a year before -> falls back to inception.
    assert out["1Y"] == pytest.approx(21.0)
    # No history before this Mon/the 1st/Jan-1 -> those bases don't exist.
    assert out["WTD"] is None
    assert out["MTD"] is None
    assert out["YTD"] is None


def test_dividend_counts_as_return():
    recs = twr.build_daily_records(LOTS, PRICES, [{"pay_date": D4, "amount": 22.0}])
    out = twr.period_returns(recs, today=D4)
    # D4 leg becomes (2420 + 22)/2200 - 1 = 11%; 1.1 * 1.11 - 1 = 22.1%.
    assert out["MAX"] == pytest.approx(22.1)


def test_benchmark_records_are_price_return():
    recs = twr.benchmark_records(PRICES, "AAA")
    out = twr.period_returns(recs, today=D4)
    assert out["MAX"] == pytest.approx(21.0)       # 100 -> 121
    assert out["1D"] == pytest.approx(10.0)        # 110 -> 121


def test_insufficient_history_is_none():
    recs = twr.build_daily_records(LOTS, {D4: {"AAA": 121.0}}, [])
    out = twr.period_returns(recs, today=D4)
    assert all(v is None for v in out.values())


def test_empty_inputs():
    assert twr.build_daily_records([], {}, []) == []
    assert twr.period_returns([], today=D4) == {p: None for p in twr.PERIODS}


# ── closing a position (audit F04) ─────────────────────────────────
#
# The old formula netted a sale into the denominator as a negative flow, so a
# day that closed the position had a denominator of MV − proceeds: the code
# either froze the factor (0%) or, on a losing sale, multiplied it by zero
# (−100%, and every later period with it). Purchases are start-weighted and
# proceeds end-weighted now, so a sale is valued from the previous close to
# its own price.


def _buy(day, qty, price, fees=0.0, sym="AAA"):
    return {"symbol": sym, "side": "BUY", "trade_date": day, "quantity": qty, "price": price, "fees": fees}


def _sell(day, qty, price, fees=0.0, sym="AAA"):
    return {"symbol": sym, "side": "SELL", "trade_date": day, "quantity": qty, "price": price, "fees": fees}


@pytest.mark.parametrize("sell_price,expected", [(110.0, 10.0), (90.0, -10.0), (100.0, 0.0)])
def test_full_sale_is_the_realised_return(sell_price, expected):
    """Buy one share at 100, sell the whole position next day at `sell_price`."""
    lots = [_buy(D1, 1, 100.0), _sell(D2, 1, sell_price)]
    prices = {D1: {"AAA": 100.0}, D2: {"AAA": sell_price}}
    out = twr.period_returns(twr.build_daily_records(lots, prices, []), today=D2)
    assert out["MAX"] == pytest.approx(expected)
    assert out["1D"] == pytest.approx(expected)


def test_partial_sale_day_is_the_price_move_not_more():
    """Two shares at 100; the price goes to 110 and one is sold at 110. The day
    is a +10% day. The old form read it as +22%: 110 / (200 − 110)."""
    lots = [_buy(D1, 2, 100.0), _sell(D2, 1, 110.0)]
    prices = {D1: {"AAA": 100.0}, D2: {"AAA": 110.0}}
    out = twr.period_returns(twr.build_daily_records(lots, prices, []), today=D2)
    assert out["1D"] == pytest.approx(10.0)


def test_fees_reduce_the_return_on_both_sides():
    """A buy fee inflates the capital at work; a sell fee shrinks the proceeds.

    Without fees: D2 is (220) / (100 + 110) — the share bought that day is
    start-weighted, so within the day the return is capital-weighted, 4.76%
    rather than the 10% the first share alone made. That is the documented
    day-granularity approximation. D3 sells both at the close price: 0%.
    With fees: D2 is 220 / (100 + 112), D3 is 217 / 220.
    """
    prices = {D1: {"AAA": 100.0}, D2: {"AAA": 110.0}, D3: {"AAA": 110.0}}
    no_fees = twr.build_daily_records(
        [_buy(D1, 1, 100.0), _buy(D2, 1, 110.0), _sell(D3, 2, 110.0)], prices, []
    )
    clean = twr.period_returns(no_fees, today=D3)["MAX"]
    assert clean == pytest.approx(220 / 210 * 100 - 100, abs=0.01)

    with_fees = twr.build_daily_records(
        [_buy(D1, 1, 100.0), _buy(D2, 1, 110.0, fees=2.0), _sell(D3, 2, 110.0, fees=3.0)], prices, []
    )
    fee_hit = twr.period_returns(with_fees, today=D3)["MAX"]
    assert fee_hit == pytest.approx(217 / 212 * 100 - 100, abs=0.01)
    assert fee_hit < clean


def test_losing_liquidation_then_reentry_recovers():
    """Sell at a loss, sit out a day, buy back in. The chain must continue from
    0.9, not be stuck at zero."""
    lots = [_buy(D1, 1, 100.0), _sell(D2, 1, 90.0), _buy(D4, 1, 100.0)]
    prices = {D1: {"AAA": 100.0}, D2: {"AAA": 90.0}, D3: {"AAA": 95.0}, D4: {"AAA": 110.0}}
    recs = twr.build_daily_records(lots, prices, [])
    curve = dict(twr.growth_curve(recs))
    assert curve[D2] == pytest.approx(0.9)
    assert curve[D3] == pytest.approx(0.9)            # idle: carried, not zeroed
    assert curve[D4] == pytest.approx(0.9 * 1.10)
    out = twr.period_returns(recs, today=D4)
    assert out["1D"] == pytest.approx(10.0)
    assert out["MAX"] == pytest.approx(-1.0)


def test_an_idle_period_is_unavailable_not_zero():
    """Everything sold on D2; D3 and D4 hold nothing. 1D over D3→D4 has no
    capital in it and must not read 0%. MAX still reports the realised +10%."""
    lots = [_buy(D1, 1, 100.0), _sell(D2, 1, 110.0)]
    prices = {D1: {"AAA": 100.0}, D2: {"AAA": 110.0}, D3: {"AAA": 120.0}, D4: {"AAA": 130.0}}
    out = twr.period_returns(twr.build_daily_records(lots, prices, []), today=D4)
    assert out["1D"] is None
    assert out["MAX"] == pytest.approx(10.0)


def test_income_after_liquidation_is_credited_to_the_capital_that_earned_it():
    lots = [_buy(D1, 1, 100.0), _sell(D2, 1, 110.0)]
    prices = {D1: {"AAA": 100.0}, D2: {"AAA": 110.0}, D3: {"AAA": 110.0}}
    recs = twr.build_daily_records(lots, prices, [{"pay_date": D3, "amount": 5.0}])
    # D2: 110/100 ; D3: nothing held, 5 of income against the last base of 100.
    assert twr.period_returns(recs, today=D3)["MAX"] == pytest.approx(15.5)


def test_records_separate_inflow_from_outflow():
    lots = [_buy(D1, 1, 100.0), _buy(D2, 1, 105.0, fees=1.0), _sell(D2, 1, 110.0, fees=2.0)]
    prices = {D1: {"AAA": 100.0}, D2: {"AAA": 110.0}}
    d2 = twr.build_daily_records(lots, prices, [])[1]
    assert d2["inflow"] == pytest.approx(106.0)
    assert d2["outflow"] == pytest.approx(108.0)
    assert d2["flow"] == pytest.approx(-2.0)


def test_net_flow_records_are_still_understood():
    """A producer that only knows the net flow (the old record shape, and
    benchmark_records) gets it split by sign."""
    recs = [
        {"day": D1, "mv": 100.0, "flow": 100.0, "div": 0.0},
        {"day": D2, "mv": 0.0, "flow": -110.0, "div": 0.0},
    ]
    assert dict(twr.growth_curve(recs))[D2] == pytest.approx(1.10)
    assert twr.active_days(recs) == {D2}
