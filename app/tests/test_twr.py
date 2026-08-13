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
