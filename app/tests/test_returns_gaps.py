"""Time-weighted return across missing and broken prices.

Audit F-7: a newly bought symbol with no quote yet was counted as an inflow
but valued at nothing, so unchanged prices read as a loss and then a gain (QA
reproduction: growth 1 → 0.5 → 1). Audit F-15: one zero quote made the growth
curve 0 for good (QA reproduction: 100 → 0 → 100 gave 1 → 0 → 0).
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import holdings  # noqa: E402
import period_stats  # noqa: E402
import twr  # noqa: E402

D1, D2, D3, D4 = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12)


def lot(symbol, day, qty=1.0, price=100.0, side="BUY", i=1):
    return {"id": i, "symbol": symbol, "account": "A", "side": side, "trade_date": day,
            "quantity": qty, "price": price, "fees": 0.0}


class TestUnpricedNewSymbol:
    LOTS = [lot("A", D1, i=1), lot("B", D2, i=2)]
    PRICES = {D1: {"A": 100.0}, D2: {"A": 100.0}, D3: {"A": 100.0, "B": 100.0}}

    def test_unchanged_prices_are_a_flat_curve(self):
        records = twr.build_daily_records(self.LOTS, self.PRICES, [])
        assert [r["mv"] for r in records] == [100.0, 200.0, 200.0]
        assert [g for _, g in twr.growth_curve(records)] == [1.0, 1.0, 1.0]

    def test_no_false_worst_and_best_day(self):
        curve = twr.growth_curve(twr.build_daily_records(self.LOTS, self.PRICES, []))
        assert all(r == pytest.approx(0.0) for _, r in period_stats.daily_returns(curve))

    def test_the_first_quote_is_a_real_move_from_the_trade_price(self):
        prices = dict(self.PRICES)
        prices[D3] = {"A": 100.0, "B": 110.0}
        curve = twr.growth_curve(twr.build_daily_records(self.LOTS, prices, []))
        assert curve[-1][1] == pytest.approx(210.0 / 200.0)

    def test_the_trade_price_never_overrides_a_real_quote(self):
        """B already has a quote on D1; buying more at 90 on D2 must not
        replace the market price with the fill."""
        lots = [lot("A", D1, i=1), lot("B", D1, i=2), lot("B", D2, price=90.0, i=3)]
        prices = {D1: {"A": 100.0, "B": 100.0}, D2: {"A": 100.0}, D3: {"A": 100.0, "B": 100.0}}
        records = twr.build_daily_records(lots, prices, [])
        assert records[1]["mv"] == pytest.approx(100.0 + 2 * 100.0)


class TestZeroQuote:
    def test_a_zero_market_value_day_does_not_zero_the_curve(self):
        recs = [dict(day=D1, mv=100.0, flow=0.0), dict(day=D2, mv=0.0, flow=0.0), dict(day=D3, mv=100.0, flow=0.0)]
        assert [g for _, g in twr.growth_curve(recs)] == [1.0, 1.0, 1.0]

    def test_a_zero_price_is_skipped_in_valuation(self):
        lots = [lot("A", D1)]
        prices = {D1: {"A": 100.0}, D2: {"A": 0.0}, D3: {"A": 110.0}}
        records = twr.build_daily_records(lots, prices, [])
        assert [r["mv"] for r in records] == [100.0, 100.0, 110.0]   # last good price carried
        assert twr.growth_curve(records)[-1][1] == pytest.approx(1.1)
        assert holdings.value_series(lots, sorted(prices.items()))[1][1] == 100.0

    def test_flows_of_an_unvaluable_day_count_the_next_day(self):
        """A purchase on the broken day is not lost: it joins the next day's
        denominator."""
        recs = [dict(day=D1, mv=100.0, inflow=0.0, outflow=0.0),
                dict(day=D2, mv=0.0, inflow=100.0, outflow=0.0),
                dict(day=D3, mv=220.0, inflow=0.0, outflow=0.0)]
        assert twr.growth_curve(recs)[-1][1] == pytest.approx(220.0 / 200.0)

    def test_selling_everything_is_still_a_real_day(self):
        """mv 0 with proceeds is a sale, not a fault: the factor is the sale."""
        recs = [dict(day=D1, mv=100.0, inflow=0.0, outflow=0.0),
                dict(day=D2, mv=0.0, inflow=0.0, outflow=95.0)]
        assert twr.growth_curve(recs)[-1][1] == pytest.approx(0.95)
