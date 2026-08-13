"""Per-match fee attribution in the FIFO engine.

`buy_cost_ps` and `sell_proceeds_ps` have always been net of fees, so
`realized_pnl` was already net — but gross was not recoverable from them. These
fields make it recoverable, and the invariant that keeps fee reporting honest is

    gross_realized_pnl - fees == realized_pnl

exactly, not approximately.

Run from app/ (bare-module imports), like the other engine suites.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fifo import Lot, MatchLine, OpenBuy, run_fifo


def lot(id_, side, day, qty, price, fees=0):
    return Lot(
        id=id_, symbol="X", account="A", side=side, trade_date=date(2026, *day),
        quantity=Decimal(str(qty)), price=Decimal(str(price)), fees=Decimal(str(fees)),
    )


class TestPerShareFee:
    def test_divides_the_fee_across_the_lot(self):
        assert lot(1, "BUY", (1, 1), 10, 100, 5).per_share_fee == Decimal("0.5")

    def test_zero_fee_lot(self):
        assert lot(1, "BUY", (1, 1), 10, 100, 0).per_share_fee == Decimal("0")

    def test_zero_quantity_does_not_divide_by_zero(self):
        l = lot(1, "BUY", (1, 1), 10, 100, 5)
        l.quantity = Decimal("0")
        assert l.per_share_fee == Decimal("0")


class TestMatchLineDefaults:
    def test_fees_default_to_zero_not_guessed(self):
        """A MatchLine built the old way reports no fees rather than inventing
        an allocation — every pre-existing construction site stays valid."""
        ml = MatchLine(
            symbol="X", account="A", sell_lot_id=2, buy_lot_id=1,
            qty=Decimal("1"), buy_cost_ps=Decimal("100"),
            sell_proceeds_ps=Decimal("110"),
        )
        assert ml.fees == Decimal("0")
        assert ml.gross_realized_pnl == ml.realized_pnl == Decimal("10")

    def test_open_buy_fee_defaults_to_zero(self):
        ob = OpenBuy(
            buy_lot_id=1, trade_date=date(2026, 1, 1),
            qty_remaining=Decimal("1"), per_share_cost=Decimal("100"),
        )
        assert ob.per_share_fee == Decimal("0")


class TestIdentity:
    """gross - fees == net, across every shape of match."""

    def _check(self, lots):
        result = run_fifo(lots)
        for ml in result.matches:
            assert ml.gross_realized_pnl - ml.fees == ml.realized_pnl
        gross = sum((m.gross_realized_pnl for m in result.matches), Decimal("0"))
        fees = sum((m.fees for m in result.matches), Decimal("0"))
        assert gross - fees == result.realized_pnl
        return result

    def test_full_match(self):
        r = self._check([
            lot(1, "BUY", (1, 1), 10, 100, 5),
            lot(2, "SELL", (2, 1), 10, 120, 3),
        ])
        m = r.matches[0]
        assert m.gross_realized_pnl == Decimal("200")   # (120-100) x 10
        assert m.fees == Decimal("8")                   # 5 + 3
        assert m.realized_pnl == Decimal("192")

    def test_partial_match_takes_a_proportional_fee(self):
        r = self._check([
            lot(1, "BUY", (1, 1), 10, 100, 5),
            lot(2, "SELL", (2, 1), 4, 120, 2),
        ])
        m = r.matches[0]
        assert m.buy_fee_ps == Decimal("0.5")
        assert m.fees == Decimal("4")   # 4 x (0.5 buy + 0.5 sell)
        assert m.gross_realized_pnl == Decimal("80")

    def test_one_sell_across_two_buys_splits_the_sell_fee(self):
        r = self._check([
            lot(1, "BUY", (1, 1), 4, 100, 4),
            lot(2, "BUY", (1, 15), 6, 110, 6),
            lot(3, "SELL", (3, 1), 10, 130, 10),
        ])
        assert len(r.matches) == 2
        # The whole sell fee is distributed across the parcels it covers.
        assert sum((m.sell_fee_ps * m.qty for m in r.matches), Decimal("0")) == Decimal("10")
        assert sum((m.fees for m in r.matches), Decimal("0")) == Decimal("20")

    def test_zero_fees_throughout(self):
        r = self._check([
            lot(1, "BUY", (1, 1), 5, 100),
            lot(2, "SELL", (2, 1), 5, 90),
        ])
        m = r.matches[0]
        assert m.fees == Decimal("0")
        assert m.gross_realized_pnl == m.realized_pnl == Decimal("-50")

    def test_fees_only_on_the_buy_side(self):
        r = self._check([
            lot(1, "BUY", (1, 1), 5, 100, 10),
            lot(2, "SELL", (2, 1), 5, 100),
        ])
        m = r.matches[0]
        assert m.gross_realized_pnl == Decimal("0")
        assert m.realized_pnl == Decimal("-10")

    def test_fees_only_on_the_sell_side(self):
        r = self._check([
            lot(1, "BUY", (1, 1), 5, 100),
            lot(2, "SELL", (2, 1), 5, 100, 10),
        ])
        m = r.matches[0]
        assert m.gross_realized_pnl == Decimal("0")
        assert m.realized_pnl == Decimal("-10")

    def test_a_partially_matched_buy_leaves_its_fee_unrealized(self):
        """Fees on shares still open belong in the open cost basis, not in a
        realized figure."""
        r = self._check([
            lot(1, "BUY", (1, 1), 10, 100, 10),
            lot(2, "SELL", (2, 1), 3, 100),
        ])
        assert r.matches[0].fees == Decimal("3")   # 3 of 10 shares
        # The other 7 remain in open_cost, fee included.
        assert r.open_cost == Decimal("7") * Decimal("101")

    def test_truncated_sell_allocates_only_matched_fees(self):
        """A SELL exceeding open BUYs is truncated by the engine; only the
        matched portion of its fee is attributed."""
        r = self._check([
            lot(1, "BUY", (1, 1), 4, 100),
            lot(2, "SELL", (2, 1), 10, 110, 10),
        ])
        assert len(r.matches) == 1
        assert r.matches[0].qty == Decimal("4")
        assert r.matches[0].fees == Decimal("4")   # 4 x 1.0/share, not 10


class TestGrossComponents:
    def test_gross_prices_strip_the_fees_back_out(self):
        r = run_fifo([
            lot(1, "BUY", (1, 1), 10, 100, 5),
            lot(2, "SELL", (2, 1), 10, 120, 3),
        ])
        m = r.matches[0]
        assert m.gross_buy_cost_ps == Decimal("100")
        assert m.gross_sell_proceeds_ps == Decimal("120")

    def test_net_prices_still_carry_them(self):
        r = run_fifo([
            lot(1, "BUY", (1, 1), 10, 100, 5),
            lot(2, "SELL", (2, 1), 10, 120, 3),
        ])
        m = r.matches[0]
        assert m.buy_cost_ps == Decimal("100.5")
        assert m.sell_proceeds_ps == Decimal("119.7")


def test_existing_realized_pnl_is_unchanged_by_the_new_fields():
    """Regression: adding fee fields must not move any number that already
    existed. Same inputs, same realized total as before."""
    r = run_fifo([
        lot(1, "BUY", (1, 1), 10, 100, 5),
        lot(2, "SELL", (2, 1), 10, 120, 3),
    ])
    # (120*10 - 3) - (100*10 + 5) = 1197 - 1005
    assert r.realized_pnl == Decimal("192")
