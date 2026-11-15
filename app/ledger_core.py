"""The pieces every ledger reader shares: the lot, its processing order, and
the one Decimal coercion. Pure and DB-free; imports nothing from the app.

Why this module exists (audit R-4, F-1, 2026-11-14):

* ``fifo.Lot`` and ``avg_cost.Lot`` were two copies of one dataclass with
  different property names, and three modules each defined their own
  ``Decimal(str(x))`` helper. One of each now lives here; the old names are
  kept as aliases so ``from fifo import Lot`` still works.

* **Processing order.** Both engines sorted by ``(trade_date, id)``, so a
  same-day SELL that happened to be stored before its BUY (a CSV listing sales
  first, or simply typed in that order) met an empty queue, was truncated to
  nothing with a log warning, and left the BUY open: a phantom position and a
  lost realized P&L, while every net-quantity reader counted zero (F-1).

  The ledger stores a trade *date*, not a time, so the true intraday order is
  unknowable. Within a day, buys are therefore processed before sells. For a
  day round trip this is exact: the share count ends flat and the realized
  P&L is (sell − buy) × qty whichever came first. What it never does is
  invent a short — shorts stay unsupported, and a sale larger than everything
  bought up to and including that day is still truncated, exactly as before.

``processing_key`` is that rule, and every reader that walks lots in order —
both engines, the holdings walk, the oversell check, the SQL function
``lot_open_quantities`` in sql/schema.sql — uses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

ZERO = Decimal(0)

# Same-day order: every BUY before any SELL. Unknown sides sort last so the
# engine that rejects them still sees them (and raises) rather than skipping.
SIDE_RANK = {"BUY": 0, "SELL": 1}


def to_decimal(x: Any) -> Decimal:
    """Exact Decimal from a ledger value: Decimal passes through, anything else
    goes through ``str`` so a float 0.1 becomes Decimal('0.1'), not
    0.1000000000000000055511151231257827."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def side_rank(side: Any) -> int:
    return SIDE_RANK.get(str(side).upper(), 2)


def processing_key(trade_date: date, side: Any, lot_id: Any) -> tuple:
    """Sort key for one lot: date, then BUY before SELL, then id."""
    return (trade_date, side_rank(side), lot_id if lot_id is not None else 0)


def lot_order_key(lot: Any) -> tuple:
    """``processing_key`` for a Lot object or a lot dict."""
    if isinstance(lot, Mapping):
        return processing_key(lot["trade_date"], lot["side"], lot.get("id"))
    return processing_key(lot.trade_date, lot.side, lot.id)


@dataclass
class Lot:
    """One BUY or SELL as the engines consume it. Quantities are positive;
    ``side`` carries the direction."""

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
        """Per-share cash, net of this lot's fee: for a BUY the fee inflates
        the cost, for a SELL it reduces the proceeds."""
        if self.quantity == 0:
            return ZERO
        gross = self.price * self.quantity
        if self.side.upper() == "BUY":
            return (gross + self.fees) / self.quantity
        return (gross - self.fees) / self.quantity

    # The average-cost engine's name for the same figure.
    net_per_share = per_share_cost

    @property
    def per_share_fee(self) -> Decimal:
        """Fee attributable to one share of this lot.

        Lets a partial match take its proportional share of the fee: matching 4
        of a 10-share lot that cost $5 in fees carries $2, not $5 and not $0.
        """
        if self.quantity == 0:
            return ZERO
        return self.fees / self.quantity
