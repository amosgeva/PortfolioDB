"""Tests for the FIFO cost-basis engine."""

import logging
from datetime import date
from decimal import Decimal

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fifo import Lot, OpenBuy, MatchLine, FifoResult, run_fifo


def _lot(id: int, side: str, qty: float, price: float, fees: float = 0.0, trade_date: date | None = None) -> Lot:
    """Helper to build a Lot quickly."""
    return Lot(
        id=id,
        symbol="TEST",
        account="ACC",
        side=side,
        trade_date=trade_date or date(2026, 1, 1),
        quantity=Decimal(str(qty)),
        price=Decimal(str(price)),
        fees=Decimal(str(fees)),
    )


class TestSingleBuy:
    def test_open_qty_equals_buy_qty(self):
        result = run_fifo([_lot(1, "BUY", 10, 50.0)])
        assert result.open_qty == Decimal("10")

    def test_open_cost_equals_price_times_qty(self):
        result = run_fifo([_lot(1, "BUY", 10, 50.0)])
        assert result.open_cost == Decimal("500.00")

    def test_realized_pnl_is_zero(self):
        result = run_fifo([_lot(1, "BUY", 10, 50.0)])
        assert result.realized_pnl == Decimal("0")

    def test_no_matches(self):
        result = run_fifo([_lot(1, "BUY", 10, 50.0)])
        assert len(result.matches) == 0


class TestBuyWithFees:
    def test_fees_increase_cost_basis(self):
        result = run_fifo([_lot(1, "BUY", 10, 50.0, fees=10.0)])
        # Cost = (50*10 + 10) = 510, per share = 51
        assert result.open_cost == Decimal("510")
        assert result.open_buys[0].per_share_cost == Decimal("51")


class TestSimpleBuySell:
    def test_full_close(self):
        lots = [
            _lot(1, "BUY", 10, 50.0, trade_date=date(2026, 1, 1)),
            _lot(2, "SELL", 10, 60.0, trade_date=date(2026, 1, 2)),
        ]
        result = run_fifo(lots)
        assert result.open_qty == Decimal("0")
        assert result.open_cost == Decimal("0")
        assert result.realized_pnl == Decimal("100.00")  # (60-50)*10 = 100

    def test_partial_sell(self):
        lots = [
            _lot(1, "BUY", 10, 50.0, trade_date=date(2026, 1, 1)),
            _lot(2, "SELL", 3, 60.0, trade_date=date(2026, 1, 2)),
        ]
        result = run_fifo(lots)
        assert result.open_qty == Decimal("7")
        assert result.open_cost == Decimal("350")
        assert result.realized_pnl == Decimal("30.00")  # (60-50)*3 = 30


class TestFIFOOrder:
    def test_sells_from_oldest_buy_first(self):
        lots = [
            _lot(1, "BUY", 5, 40.0, trade_date=date(2026, 1, 1)),
            _lot(2, "BUY", 5, 60.0, trade_date=date(2026, 1, 2)),
            _lot(3, "SELL", 5, 70.0, trade_date=date(2026, 1, 3)),
        ]
        result = run_fifo(lots)
        # Should sell from lot 1 (price 40), realized = (70-40)*5 = 150
        assert result.realized_pnl == Decimal("150")
        assert result.open_qty == Decimal("5")
        # Remaining should be lot 2 cost
        assert result.open_cost == Decimal("300")  # 5 * 60

    def test_sells_across_multiple_buys(self):
        lots = [
            _lot(1, "BUY", 3, 40.0, trade_date=date(2026, 1, 1)),
            _lot(2, "BUY", 3, 60.0, trade_date=date(2026, 1, 2)),
            _lot(3, "SELL", 5, 70.0, trade_date=date(2026, 1, 3)),
        ]
        result = run_fifo(lots)
        # Sells 3 from lot1 (profit (70-40)*3=90) + 2 from lot2 (profit (70-60)*2=20) = 110
        assert result.realized_pnl == Decimal("110")
        assert result.open_qty == Decimal("1")
        assert result.open_cost == Decimal("60")  # 1 * 60


class TestSellWithFees:
    def test_sell_fees_reduce_proceeds(self):
        lots = [
            _lot(1, "BUY", 10, 50.0, trade_date=date(2026, 1, 1)),
            _lot(2, "SELL", 10, 60.0, fees=10.0, trade_date=date(2026, 1, 2)),
        ]
        result = run_fifo(lots)
        # Sell proceeds per share = (60*10 - 10)/10 = 59
        # Realized = (59 - 50) * 10 = 90
        assert result.realized_pnl == Decimal("90")


class TestSellExceedsBuys:
    def test_excess_is_truncated_with_warning(self, caplog):
        # Shorts are not supported: the engine matches what it can, logs a
        # warning, and ignores the excess (see fifo.py / CLAUDE.md).
        lots = [
            _lot(1, "BUY", 5, 50.0),
            _lot(2, "SELL", 10, 60.0, trade_date=date(2026, 1, 2)),
        ]
        with caplog.at_level(logging.WARNING):
            result = run_fifo(lots)
        assert "SELL exceeds BUYs" in caplog.text
        # Only the 5 matched shares realize P&L: (60 - 50) * 5
        assert result.realized_pnl == Decimal("50")
        assert result.open_qty == Decimal("0")
        assert len(result.matches) == 1
        assert result.matches[0].qty == Decimal("5")


class TestUnknownSide:
    def test_raises_value_error(self):
        lots = [_lot(1, "HOLD", 5, 50.0)]
        with pytest.raises(ValueError, match="Unknown side"):
            run_fifo(lots)


class TestMultipleBuySellCycles:
    def test_buy_sell_buy_sell(self):
        lots = [
            _lot(1, "BUY", 10, 100.0, trade_date=date(2026, 1, 1)),
            _lot(2, "SELL", 10, 120.0, trade_date=date(2026, 1, 2)),
            _lot(3, "BUY", 5, 110.0, trade_date=date(2026, 1, 3)),
            _lot(4, "SELL", 5, 130.0, trade_date=date(2026, 1, 4)),
        ]
        result = run_fifo(lots)
        # Cycle 1: (120-100)*10 = 200
        # Cycle 2: (130-110)*5 = 100
        assert result.realized_pnl == Decimal("300")
        assert result.open_qty == Decimal("0")


class TestLotPerShareCost:
    def test_buy_per_share_includes_fees(self):
        lot = _lot(1, "BUY", 10, 50.0, fees=20.0)
        assert lot.per_share_cost == Decimal("52")  # (500+20)/10

    def test_sell_per_share_subtracts_fees(self):
        lot = _lot(1, "SELL", 10, 60.0, fees=20.0)
        assert lot.per_share_cost == Decimal("58")  # (600-20)/10

    def test_zero_qty_returns_zero(self):
        lot = Lot(id=1, symbol="X", account=None, side="BUY",
                  trade_date=date(2026, 1, 1), quantity=Decimal("0"),
                  price=Decimal("50"), fees=Decimal("0"))
        assert lot.per_share_cost == Decimal("0")
