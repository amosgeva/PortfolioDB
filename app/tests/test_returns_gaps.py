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


class TestFillPriceIsOrderIndependent:
    """QA of PR #125: the fallback took the last lot's price, so the same ledger
    gave 220 on one surface (rows BUY-before-SELL) and 200 on another (rows as
    stored), and MAX TWR 8.18% against 12.00%. The observation is now the
    quantity-weighted price of the latest trade date's fills."""

    # QAOLD quoted all along; QANEW unquoted until D3. On D2 a SELL of 1 at 120
    # (stored first) and a BUY of 2 at 100, same account; plus a later D2 BUY
    # at 90 in another account.
    LOTS = [
        lot("OLD", D1, i=1),
        lot("NEW", D2, qty=1.0, price=120.0, side="SELL", i=2),
        lot("NEW", D2, qty=2.0, price=100.0, i=3),
        dict(lot("NEW", D2, qty=1.0, price=90.0, i=4), account="B"),
    ]
    PRICES = {D1: {"OLD": 100.0}, D2: {"OLD": 100.0}, D3: {"OLD": 100.0, "NEW": 110.0}}

    def _curve(self, lots):
        records = twr.build_daily_records(lots, self.PRICES, [])
        return [r["mv"] for r in records], [g for _, g in twr.growth_curve(records)]

    def test_every_order_and_every_reader_shape_gives_one_answer(self):
        import itertools
        expected = None
        for perm in itertools.permutations(self.LOTS):
            for strip in (False, True):     # the MCP loader selects no id/account
                lots = [{k: v for k, v in l.items() if not strip or k not in ("id",)} for l in perm]
                got = self._curve(lots)
                expected = expected or got
                assert got == expected, (perm, strip)

    def test_the_day_is_valued_at_the_quantity_weighted_fill_price(self):
        mv, _ = self._curve(self.LOTS)
        # NEW held after D2: account A 2 − 1 = 1, account B 1 → 2 shares, at
        # (120·1 + 100·2 + 90·1) / 4 = 102.5.
        assert mv[1] == pytest.approx(100.0 + 2 * 102.5)

    def test_only_the_latest_trade_date_in_a_bucket_counts(self):
        """Trades on D2 and D3 with no D2 snapshot both land on D3's snapshot;
        the D3 fill is the more recent observation."""
        lots = [lot("OLD", D1, i=1), lot("NEW", D2, price=80.0, i=2), lot("NEW", D3, price=95.0, i=3)]
        prices = {D1: {"OLD": 100.0}, D3: {"OLD": 100.0}, D4: {"OLD": 100.0, "NEW": 95.0}}
        records = twr.build_daily_records(lots, prices, [])
        assert records[1]["mv"] == pytest.approx(100.0 + 2 * 95.0)


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
