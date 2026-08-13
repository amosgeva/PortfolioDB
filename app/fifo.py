"""FIFO position and realized P&L engine (per symbol + account).

We keep PortfolioDB as a *ledger* of lots (BUY/SELL). This module computes:
- Open quantity + remaining cost basis (FIFO)
- Realized P&L from SELL lots (FIFO)

Rules:
- FIFO matching is done **within (symbol, account)**.
- Buy lot fees increase cost basis; sell lot fees reduce proceeds.
- Quantities are positive in DB; side indicates direction.

This is intentionally implemented in Python (not SQL) for clarity and future extension.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass
class Lot:
    id: int
    symbol: str
    account: str | None
    side: str  # BUY/SELL
    trade_date: date
    quantity: Decimal
    price: Decimal
    fees: Decimal

    @property
    def per_share_cost(self) -> Decimal:
        # For BUY: (price*qty + fees)/qty; For SELL proceeds: (price*qty - fees)/qty
        if self.quantity == 0:
            return Decimal("0")
        gross = self.price * self.quantity
        if self.side.upper() == "BUY":
            return (gross + self.fees) / self.quantity
        else:
            return (gross - self.fees) / self.quantity

    @property
    def per_share_fee(self) -> Decimal:
        """Fee attributable to one share of this lot.

        Lets a partial match take its proportional share of the fee: matching 4
        of a 10-share lot that cost $5 in fees carries $2, not $5 and not $0.
        """
        if self.quantity == 0:
            return Decimal("0")
        return self.fees / self.quantity


@dataclass
class OpenBuy:
    buy_lot_id: int
    trade_date: date
    qty_remaining: Decimal
    per_share_cost: Decimal
    # Carried so a match can report cost gross of fees as well as net. Defaults
    # to zero so any existing constructor call keeps working.
    per_share_fee: Decimal = Decimal("0")


@dataclass
class MatchLine:
    """One closed parcel: some quantity of one BUY lot matched to one SELL lot.

    ``buy_cost_ps`` and ``sell_proceeds_ps`` are **net of fees** — buy fees
    inflate cost, sell fees reduce proceeds — so ``realized_pnl`` is already
    net. The two fee fields exist to recover the gross figures, which the net
    ones cannot yield on their own. They default to zero, so a MatchLine built
    without them behaves exactly as before and reports zero fees rather than
    guessing.
    """

    symbol: str
    account: str | None
    sell_lot_id: int
    buy_lot_id: int
    qty: Decimal
    buy_cost_ps: Decimal
    sell_proceeds_ps: Decimal
    buy_fee_ps: Decimal = Decimal("0")
    sell_fee_ps: Decimal = Decimal("0")

    @property
    def realized_pnl(self) -> Decimal:
        """Net of fees — the figure every existing caller already reads."""
        return (self.sell_proceeds_ps - self.buy_cost_ps) * self.qty

    @property
    def fees(self) -> Decimal:
        """Fees attributable to this parcel, both sides."""
        return (self.buy_fee_ps + self.sell_fee_ps) * self.qty

    @property
    def gross_buy_cost_ps(self) -> Decimal:
        """Buy price per share before its fee."""
        return self.buy_cost_ps - self.buy_fee_ps

    @property
    def gross_sell_proceeds_ps(self) -> Decimal:
        """Sell price per share before its fee."""
        return self.sell_proceeds_ps + self.sell_fee_ps

    @property
    def gross_realized_pnl(self) -> Decimal:
        """P&L before costs. ``gross_realized_pnl - fees == realized_pnl``
        exactly, which is the identity that keeps fee reporting from
        double-counting."""
        return (self.gross_sell_proceeds_ps - self.gross_buy_cost_ps) * self.qty


@dataclass
class FifoResult:
    open_qty: Decimal
    open_cost: Decimal
    realized_pnl: Decimal
    matches: list[MatchLine]
    open_buys: list[OpenBuy]


def run_fifo(lots: list[Lot]) -> FifoResult:
    """Run FIFO for a single (symbol, account) stream of lots."""

    # Sort deterministically
    lots_sorted = sorted(lots, key=lambda l: (l.trade_date, l.id))

    open_buys: deque[OpenBuy] = deque()
    matches: list[MatchLine] = []
    realized = Decimal("0")

    sym = lots_sorted[0].symbol if lots_sorted else ""
    acct = lots_sorted[0].account if lots_sorted else None

    for lot in lots_sorted:
        side = lot.side.upper()
        if side == "BUY":
            open_buys.append(
                OpenBuy(
                    buy_lot_id=lot.id,
                    trade_date=lot.trade_date,
                    qty_remaining=lot.quantity,
                    per_share_cost=lot.per_share_cost,
                    per_share_fee=lot.per_share_fee,
                )
            )
            continue

        if side != "SELL":
            raise ValueError(f"Unknown side: {lot.side}")

        qty_to_sell = lot.quantity
        sell_ps = lot.per_share_cost  # proceeds per share, net of the sell fee
        sell_fee_ps = lot.per_share_fee

        # Consume from oldest open buy lots
        while qty_to_sell > 0:
            if not open_buys:
                import logging
                logging.warning(
                    f"SELL exceeds BUYs for {lot.symbol} ({lot.account}). "
                    f"Shorting not supported in FIFO engine - ignoring excess {qty_to_sell}." 
                )
                break

            ob = open_buys[0]
            take = qty_to_sell if qty_to_sell <= ob.qty_remaining else ob.qty_remaining

            ml = MatchLine(
                symbol=sym,
                account=acct,
                sell_lot_id=lot.id,
                buy_lot_id=ob.buy_lot_id,
                qty=take,
                buy_cost_ps=ob.per_share_cost,
                sell_proceeds_ps=sell_ps,
                buy_fee_ps=ob.per_share_fee,
                sell_fee_ps=sell_fee_ps,
            )
            matches.append(ml)
            realized += ml.realized_pnl

            ob.qty_remaining -= take
            qty_to_sell -= take

            if ob.qty_remaining <= 0:
                open_buys.popleft()

    open_qty = sum((b.qty_remaining for b in open_buys), start=Decimal("0"))
    open_cost = sum((b.qty_remaining * b.per_share_cost for b in open_buys), start=Decimal("0"))

    return FifoResult(
        open_qty=open_qty,
        open_cost=open_cost,
        realized_pnl=realized,
        matches=matches,
        open_buys=open_buys,
    )
