"""Say so at ingestion when a SELL is bigger than the position it closes.

Shorts are unsupported: the FIFO engine matches what it can and truncates the
rest with a log warning, which understates realized P&L and leaves a position
that reads as flat while the broker shows one. Until now that warning fired at
*read* time — in the dashboard's log, in the MCP data-quality report — long
after the row went in, and the caller who typed the sale never saw it. The
usual cause is not a short at all: the wrong account, a mistyped date, or a
BUY that was never entered.

This module is the check the write paths run before inserting. It warns; it
does not refuse. The ledger is the operator's record of what happened, and a
sale that really did exceed the recorded position is exactly the thing they
need to see written down, then fix with the missing BUY or a correction.

Public API (the Manage page calls this too)::

    check_sell(conn, symbol, account, trade_date, quantity) -> SellCheck
    oversell_warning(conn, symbol, account, trade_date, quantity) -> str | None
    open_quantity(conn, symbol, account, as_of) -> Decimal

``account`` is matched exactly, as the engines match it: ``None`` is the
unassigned account, not "any account" (audit F-2 — a sale entered with no
account used to be checked against every account's shares, passed, and was
then dropped by FIFO, which finds no buys in the unassigned stream). Pass
``None`` rather than ``""`` for "no account"; the two are different streams.
``quantity`` is in the trade date's share units, as the operator types it.
Everything is exact ``Decimal`` (audit F-13).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import corporate_actions
import holdings
import ledger_inputs
from ledger_core import ZERO, to_decimal


def _account_lots(lots, account: str | None) -> list[dict]:
    return [lot for lot in lots if lot.get("account") == account]


def _plain(d: Decimal) -> str:
    # normalize() alone renders 20 as "2E+1"; 'f' keeps it a number a human reads.
    return format(d.normalize(), "f")


def open_quantity(conn, symbol: str, account: str | None, as_of: date) -> Decimal:
    """Shares of ``symbol`` held in ``account`` as of ``as_of`` — in that day's
    share units, including trades dated that day — by the engines' rules."""
    sym = symbol.upper()
    ledger = ledger_inputs.load(conn, symbol=sym, as_of=as_of)
    return holdings.open_quantities(_account_lots(ledger.lots, account)).get(sym, ZERO)


@dataclass(frozen=True)
class SellCheck:
    """What recording a SELL would do to the position.

    ``held`` and ``shortfall`` are in the trade date's units: the sale is
    larger than what that account held that day by ``shortfall``.
    ``later_shortfall`` is in today's units: how much of sales *already
    recorded after* this date would newly be dropped, because this one,
    backdated, uses up shares they were matched against (``later_lot_ids``).
    """

    symbol: str
    account: str | None
    trade_date: date
    quantity: Decimal
    held: Decimal
    shortfall: Decimal
    later_shortfall: Decimal = ZERO
    later_lot_ids: tuple = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.shortfall <= 0 and self.later_shortfall <= 0

    def message(self) -> str | None:
        if self.ok:
            return None
        where = self.account if self.account is not None else "(no account)"
        parts = []
        if self.shortfall > 0:
            parts.append(
                f"WARNING: selling {_plain(self.quantity)} {self.symbol} in {where} on "
                f"{self.trade_date.isoformat()}, but the ledger holds {_plain(self.held)} there as "
                f"of that date. Shorts are not supported: the FIFO engine will match "
                f"{_plain(self.held)} and drop the rest with a log warning, so realized P&L will "
                "be understated."
            )
        if self.later_shortfall > 0:
            ids = ", ".join(str(i) for i in self.later_lot_ids)
            parts.append(
                f"WARNING: this sale is dated before sale(s) already recorded in {where} "
                f"(lot id {ids}) and uses shares they were matched against: "
                f"{_plain(self.later_shortfall)} {self.symbol} of them would be dropped."
            )
        parts.append(
            "Check the account, the trade date, or a BUY that was never entered."
        )
        return " ".join(parts)


def check_sell(conn, symbol: str, account: str | None, trade_date: date, quantity) -> SellCheck:
    """Check a SELL before it is inserted. Read-only; runs on ``conn``."""
    sym = symbol.upper()
    qty = to_decimal(quantity)
    held = open_quantity(conn, sym, account, trade_date)
    shortfall = qty - held if qty > held else ZERO

    # The whole stream in today's units, with and without the new sale, so a
    # backdated sale that starves a later one is caught too. The new row is
    # restated with the same actions as the rest and sorts after every lot
    # already on its date — which is where its (larger) id will put it.
    ledger = ledger_inputs.load(conn, symbol=sym)
    stream = _account_lots(ledger.lots, account)
    new_lot = corporate_actions.adjust_lot_rows(
        [{"id": float("inf"), "symbol": sym, "account": account, "side": "SELL",
          "trade_date": trade_date, "quantity": qty, "price": ZERO, "fees": ZERO}],
        ledger.actions,
    )[0]
    before = {t["id"]: t["dropped"] for t in holdings.truncations(stream)}
    later_ids: list = []
    later = ZERO
    for t in holdings.truncations(stream + [new_lot]):
        if t["id"] == new_lot["id"]:
            continue
        extra = t["dropped"] - before.get(t["id"], ZERO)
        if extra > 0:
            later += extra
            later_ids.append(t["id"])
    return SellCheck(
        symbol=sym, account=account, trade_date=trade_date, quantity=qty,
        held=held, shortfall=shortfall, later_shortfall=later,
        later_lot_ids=tuple(later_ids),
    )


def oversell_warning(conn, symbol: str, account: str | None, trade_date: date, quantity) -> str | None:
    """The warning to print for a SELL that exceeds the open position, or None."""
    return check_sell(conn, symbol, account, trade_date, quantity).message()
