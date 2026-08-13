"""Tests for the moving weighted-average cost engine."""

import logging
from datetime import date
from decimal import Decimal

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from avg_cost import Lot, run_avg_cost


def _lot(lot_id: int, side: str, qty: float, price: float, fees: float = 0.0, trade_date: date | None = None) -> Lot:
    """Helper to build a Lot quickly."""
    return Lot(
        id=lot_id,
        symbol="TEST",
        account="ACC",
        side=side,
        trade_date=trade_date or date(2026, 1, 1),
        quantity=Decimal(str(qty)),
        price=Decimal(str(price)),
        fees=Decimal(str(fees)),
    )


class TestSingleBuy:
    def test_open_position(self):
        result = run_avg_cost([_lot(1, "BUY", 10, 50.0)])
        assert result.open_qty == Decimal("10")
        assert result.avg_cost_ps == Decimal("50")
        assert result.open_cost == Decimal("500")
        assert result.realized_pnl == Decimal("0")

    def test_buy_fees_increase_cost_basis(self):
        # (50*10 + 10) / 10 = 51 per share
        result = run_avg_cost([_lot(1, "BUY", 10, 50.0, fees=10.0)])
        assert result.avg_cost_ps == Decimal("51")
        assert result.open_cost == Decimal("510")


class TestWeightedAverage:
    def test_two_buys_average(self):
        # 10 @ 50 + 10 @ 70 -> avg 60
        lots = [
            _lot(1, "BUY", 10, 50.0),
            _lot(2, "BUY", 10, 70.0, trade_date=date(2026, 1, 2)),
        ]
        result = run_avg_cost(lots)
        assert result.open_qty == Decimal("20")
        assert result.avg_cost_ps == Decimal("60")

    def test_sell_does_not_change_avg_cost(self):
        lots = [
            _lot(1, "BUY", 10, 50.0),
            _lot(2, "BUY", 10, 70.0, trade_date=date(2026, 1, 2)),
            _lot(3, "SELL", 5, 80.0, trade_date=date(2026, 1, 3)),
        ]
        result = run_avg_cost(lots)
        assert result.avg_cost_ps == Decimal("60")
        assert result.open_qty == Decimal("15")


class TestRealizedPnl:
    def test_sell_realizes_against_avg(self):
        # avg 60, sell 5 @ 80 -> realized (80-60)*5 = 100
        lots = [
            _lot(1, "BUY", 10, 50.0),
            _lot(2, "BUY", 10, 70.0, trade_date=date(2026, 1, 2)),
            _lot(3, "SELL", 5, 80.0, trade_date=date(2026, 1, 3)),
        ]
        result = run_avg_cost(lots)
        assert result.realized_pnl == Decimal("100")

    def test_sell_fees_reduce_proceeds(self):
        # proceeds ps = (60*10 - 10)/10 = 59; realized (59-50)*10 = 90
        lots = [
            _lot(1, "BUY", 10, 50.0),
            _lot(2, "SELL", 10, 60.0, fees=10.0, trade_date=date(2026, 1, 2)),
        ]
        result = run_avg_cost(lots)
        assert result.realized_pnl == Decimal("90")

    def test_full_close_resets_avg_cost(self):
        lots = [
            _lot(1, "BUY", 10, 50.0),
            _lot(2, "SELL", 10, 60.0, trade_date=date(2026, 1, 2)),
        ]
        result = run_avg_cost(lots)
        assert result.open_qty == Decimal("0")
        assert result.avg_cost_ps == Decimal("0")
        assert result.open_cost == Decimal("0")


class TestSellExceedsBuys:
    def test_excess_is_truncated_with_warning(self, caplog):
        # Shorts unsupported: consume what's open, warn, ignore the excess.
        lots = [
            _lot(1, "BUY", 5, 50.0),
            _lot(2, "SELL", 10, 60.0, trade_date=date(2026, 1, 2)),
        ]
        with caplog.at_level(logging.WARNING):
            result = run_avg_cost(lots)
        assert "SELL exceeds BUYs" in caplog.text
        assert result.realized_pnl == Decimal("50")  # (60-50) * 5 matched
        assert result.open_qty == Decimal("0")

    def test_sell_with_nothing_open_is_ignored(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = run_avg_cost([_lot(1, "SELL", 5, 60.0)])
        assert "SELL exceeds BUYs" in caplog.text
        assert result.realized_pnl == Decimal("0")
        assert result.open_qty == Decimal("0")


class TestUnknownSide:
    def test_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown side"):
            run_avg_cost([_lot(1, "HOLD", 5, 50.0)])


class TestOrdering:
    def test_lots_sorted_by_date_then_id(self):
        # SELL dated after the BUYs must consume them even if listed first.
        lots = [
            _lot(3, "SELL", 10, 80.0, trade_date=date(2026, 1, 3)),
            _lot(1, "BUY", 5, 50.0, trade_date=date(2026, 1, 1)),
            _lot(2, "BUY", 5, 70.0, trade_date=date(2026, 1, 2)),
        ]
        result = run_avg_cost(lots)
        # avg = 60, realized (80-60)*10 = 200
        assert result.realized_pnl == Decimal("200")
        assert result.open_qty == Decimal("0")
