"""Historical holdings reconstruction.

The regression these guard against: valuing history at *today's* share counts,
which back-projects current positions onto a past that did not hold them.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import holdings

JER = ZoneInfo("Asia/Jerusalem")


def lot(symbol: str, side: str, day: date, qty: float) -> dict:
    return {"symbol": symbol, "side": side, "trade_date": day, "quantity": qty}


LOTS = [
    lot("AAA", "BUY", date(2026, 1, 10), 10.0),
    lot("BBB", "BUY", date(2026, 2, 10), 5.0),
    lot("AAA", "SELL", date(2026, 3, 10), 10.0),
]


class TestHoldingsSeries:
    def test_nothing_held_before_the_first_trade(self):
        assert holdings.holdings_on(LOTS, date(2026, 1, 1)) == {}

    def test_lot_counts_from_its_trade_date_inclusive(self):
        assert holdings.holdings_on(LOTS, date(2026, 1, 10)) == {"AAA": 10.0}

    def test_second_position_accumulates(self):
        assert holdings.holdings_on(LOTS, date(2026, 2, 15)) == {"AAA": 10.0, "BBB": 5.0}

    def test_closed_position_drops_out_entirely(self):
        """A fully-sold symbol must disappear, not linger at qty 0 — otherwise
        it keeps showing up in allocation and weight denominators."""
        assert holdings.holdings_on(LOTS, date(2026, 3, 15)) == {"BBB": 5.0}

    def test_partial_sale_leaves_the_remainder(self):
        lots = [
            lot("AAA", "BUY", date(2026, 1, 10), 10.0),
            lot("AAA", "SELL", date(2026, 2, 10), 4.0),
        ]
        assert holdings.holdings_on(lots, date(2026, 2, 10)) == {"AAA": 6.0}

    def test_series_is_returned_in_ascending_order(self):
        points = [date(2026, 3, 15), date(2026, 1, 1), date(2026, 2, 15)]
        out = holdings.holdings_series(LOTS, points)
        assert [p for p, _ in out] == sorted(points)

    def test_timestamps_resolve_to_local_calendar_day(self):
        ts = datetime(2026, 1, 10, 23, 30, tzinfo=JER)
        assert holdings.holdings_on(LOTS, ts) == {"AAA": 10.0}

    def test_empty_inputs(self):
        assert holdings.holdings_series([], [date(2026, 1, 1)]) == [
            (date(2026, 1, 1), {})
        ]
        assert holdings.holdings_series(LOTS, []) == []


class TestValueSeries:
    def test_values_each_point_at_the_holdings_then(self):
        prices = [
            (date(2026, 1, 5), {"AAA": 100.0, "BBB": 50.0}),
            (date(2026, 1, 15), {"AAA": 100.0, "BBB": 50.0}),
            (date(2026, 2, 15), {"AAA": 100.0, "BBB": 50.0}),
        ]
        out = dict(holdings.value_series(LOTS, prices))
        assert out[date(2026, 1, 5)] == 0.0        # nothing held yet
        assert out[date(2026, 1, 15)] == 1000.0    # AAA only
        assert out[date(2026, 2, 15)] == 1250.0    # AAA + BBB

    def test_does_not_back_project_current_holdings(self):
        """The defect this module exists to fix.

        BBB is bought in February. Valuing January at today's holdings would
        report 1250 for a month in which BBB was not owned.
        """
        prices = [(date(2026, 1, 15), {"AAA": 100.0, "BBB": 50.0})]
        value = holdings.value_series(LOTS, prices)[0][1]

        current = {"BBB": 5.0}  # what the portfolio holds at the end
        back_projected = sum(q * 50.0 for q in current.values())

        assert value == 1000.0
        assert value != back_projected

    def test_sold_position_stops_contributing(self):
        prices = [(date(2026, 3, 15), {"AAA": 100.0, "BBB": 50.0})]
        assert holdings.value_series(LOTS, prices)[0][1] == 250.0

    def test_sparse_prices_carry_forward(self):
        prices = [
            (date(2026, 1, 15), {"AAA": 100.0}),
            (date(2026, 1, 16), {}),  # no snapshot for AAA this point
        ]
        out = holdings.value_series(LOTS, prices, carry_forward=True)
        assert out[1][1] == 1000.0

    def test_without_carry_forward_a_gap_contributes_nothing(self):
        prices = [
            (date(2026, 1, 15), {"AAA": 100.0}),
            (date(2026, 1, 16), {}),
        ]
        out = holdings.value_series(LOTS, prices, carry_forward=False)
        assert out[1][1] == 0.0

    def test_unpriced_symbol_is_skipped_not_guessed(self):
        prices = [(date(2026, 2, 15), {"AAA": 100.0})]  # BBB has no price
        assert holdings.value_series(LOTS, prices)[0][1] == 1000.0
