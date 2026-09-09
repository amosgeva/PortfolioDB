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
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import holdings
import ledger_inputs


def open_quantity(conn, symbol: str, account: str | None, as_of: date) -> Decimal:
    """Shares of ``symbol`` held in ``account`` as of ``as_of``, in the units the
    prepared ledger is read in (post-split), including trades dated that day."""
    ledger = ledger_inputs.load(conn, symbol=symbol, account=account, as_of=as_of)
    sym = symbol.upper()
    return Decimal(str(holdings.holdings_on(ledger.lots, as_of).get(sym, 0.0)))


def oversell_warning(conn, symbol: str, account: str | None, trade_date: date, quantity) -> str | None:
    """The warning to print for a SELL that exceeds the open position, or None."""
    qty = Decimal(str(quantity))
    held = open_quantity(conn, symbol, account, trade_date)
    if qty <= held:
        return None
    where = account or "(no account)"

    def _plain(d: Decimal) -> str:
        # normalize() alone renders 20 as "2E+1"; 'f' keeps it a number a
        # human reads.
        return format(d.normalize(), "f")

    return (
        f"WARNING: selling {_plain(qty)} {symbol.upper()} in {where} on {trade_date.isoformat()}, "
        f"but the ledger holds {_plain(held)} there as of that date. Shorts are not "
        f"supported: the FIFO engine will match {_plain(held)} and drop the rest with a log "
        "warning, so realized P&L will be understated. Check the account, the trade date, "
        "or a BUY that was never entered."
    )
