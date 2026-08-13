"""Average-cost position and realized P&L engine (per symbol + account).

Rules:
- Fees on BUY increase cost basis.
- Fees on SELL reduce proceeds.
- Average cost is recalculated on BUYs only (moving weighted average).
- On SELL, realized P&L = (net proceeds per share - avg_cost) * qty_sold.
- Shorting not supported.

This is meant to complement fifo.py so we can report both methods.
"""

from __future__ import annotations

import logging
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
    def net_per_share(self) -> Decimal:
        if self.quantity == 0:
            return Decimal("0")
        gross = self.price * self.quantity
        if self.side.upper() == "BUY":
            return (gross + self.fees) / self.quantity
        return (gross - self.fees) / self.quantity


@dataclass
class AvgResult:
    open_qty: Decimal
    open_cost: Decimal
    avg_cost_ps: Decimal
    realized_pnl: Decimal


def run_avg_cost(lots: list[Lot]) -> AvgResult:
    # Sort deterministically
    lots_sorted = sorted(lots, key=lambda l: (l.trade_date, l.id))

    qty = Decimal("0")
    avg_cost = Decimal("0")  # per share
    realized = Decimal("0")

    for lot in lots_sorted:
        side = lot.side.upper()
        if side == "BUY":
            buy_ps = lot.net_per_share
            new_qty = qty + lot.quantity
            if new_qty == 0:
                qty = Decimal("0")
                avg_cost = Decimal("0")
            else:
                # Weighted average (qty * avg_cost + buy_qty * buy_ps) / new_qty
                avg_cost = ((qty * avg_cost) + (lot.quantity * buy_ps)) / new_qty
                qty = new_qty
            continue

        if side != "SELL":
            raise ValueError(f"Unknown side: {lot.side}")

        if lot.quantity > qty:
            excess = lot.quantity - qty
            logging.warning(
                f"SELL exceeds BUYs for {lot.symbol} ({lot.account}). "
                f"Shorting not supported in avg-cost engine - ignoring excess {excess}."
            )
            # Consume whatever is left, ignore the excess
            if qty <= 0:
                continue
            sell_qty = qty
        else:
            sell_qty = lot.quantity

        sell_ps = lot.net_per_share
        realized += (sell_ps - avg_cost) * sell_qty
        qty -= sell_qty
        if qty == 0:
            avg_cost = Decimal("0")

    open_cost = qty * avg_cost
    return AvgResult(open_qty=qty, open_cost=open_cost, avg_cost_ps=avg_cost, realized_pnl=realized)
