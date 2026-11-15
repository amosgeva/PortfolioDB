"""Shared write-path helpers: one insert for a lot, one answer to "was it written?".

Every writer used to issue its own ``INSERT … ON CONFLICT DO NOTHING`` and
ignore the outcome. Two genuine fills on one day at the same quantity and
price collide on ``lots_dedupe_idx``, so the second was dropped while
``add_lot`` printed "OK" and ``sell_lot`` printed the (unchanged) position
(audit N-2, D-1). ``insert_lot`` returns whether the row went in, and
``duplicate_lot_message`` says what to do about it, so no writer can claim a
success it did not have. The CSV importer, the CLIs and the Manage page all
come through here.

Nothing here commits: the caller owns the transaction (``db.transaction()``),
so an instrument row and its lot land together or not at all.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from psycopg2 import sql

from db import fetch_all, run

import account_names
import symbol_paths


def ensure_instrument(conn, symbol: str) -> None:
    """Register ``symbol`` if it is new. The symbol must already be normalized
    (``symbol_paths.normalize_symbol``); the database refuses anything else."""
    run(conn, "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING", (symbol,))


def insert_lot(
    conn,
    *,
    symbol: str,
    account: str | None,
    side: str,
    trade_date: date | str,
    quantity: Decimal,
    price: Decimal,
    fees: Decimal,
    notes: str | None = None,
) -> bool:
    """Insert one lot inside the caller's transaction.

    Returns True when the row was written and False when ``lots_dedupe_idx``
    (symbol, account, side, trade_date, quantity, price) already held an
    identical lot, in which case nothing was written.

    ValueError, before anything is sent, when ``account`` is a reserved label
    (``account_names``: ``(none)`` means "no account" everywhere it is read).
    The callers check earlier and say so in their own words; this is the
    shared writer's own guard, and the database's CHECK is the last one.
    """
    account_names.refuse_reserved(account)
    n = run(
        conn,
        """
        INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (symbol, account, side, trade_date, quantity, price, fees, notes),
    )
    return bool(n)


def _plain(d) -> str:
    d = Decimal(str(d))
    return format(d.normalize(), "f")


def duplicate_lot_message(
    *, symbol: str, account: str | None, side: str, trade_date, quantity, price
) -> str:
    """What the operator needs to read when ``insert_lot`` returned False."""
    where = account if account is not None else "(no account)"
    return (
        f"DUPLICATE — NOT RECORDED: a {side} of {_plain(quantity)} {symbol} at {_plain(price)} "
        f"in {where} on {trade_date} is already in the ledger, and the dedupe guard "
        "(symbol, account, side, date, quantity, price) refuses an identical row. If this "
        "is the same trade entered twice, nothing needs doing. If it is a second fill at "
        "the same price, record the fills as one lot: delete the existing lot and add one "
        f"with the combined quantity and fees (FIFO treats them identically)."
    )


def currency_scope_problem(conn, symbol: str) -> str | None:
    """Why ``symbol`` is outside the ledger's USD-only scope, or None.

    Two signals (audit F-11): the instrument row says it trades in another
    currency, or the symbol carries a non-US exchange suffix (``.TA``, ``.L``,
    ``.HK`` …). Every figure the dashboard, the reports and the MCP produce is
    a sum of prices, and a price in agorot or pence summed as dollars is wrong
    by a factor of hundreds — so the writers refuse such a lot unless told the
    operator knows (``--allow-non-usd``).
    """
    rows = fetch_all(conn, "SELECT currency FROM instruments WHERE symbol = %s", (symbol,))
    currency = (rows[0]["currency"] or "USD").upper() if rows else "USD"
    if currency != "USD":
        return f"{symbol} is registered as trading in {currency}"
    suffix = symbol_paths.foreign_listing_suffix(symbol)
    if suffix:
        return f"{symbol} carries the .{suffix} suffix of a non-US exchange, which does not quote in US dollars"
    return None


def usd_scope_refusal(problem: str) -> str:
    return (
        f"REFUSED: {problem}. PortfolioDB is USD-only — it has no FX conversion, so this "
        "position would be added to every total as if its price were in dollars. Record "
        "it in USD (converted price), or pass --allow-non-usd if you accept that."
    )


@contextmanager
def savepoint(conn, name: str):
    """Run a block under a savepoint: on error roll back to it (leaving the
    enclosing transaction usable) and re-raise. The collector and the
    importer use it so one bad row cannot abort everything around it."""
    ident = sql.Identifier(name)
    run(conn, sql.SQL("SAVEPOINT {}").format(ident))
    ok = False
    try:
        yield
        ok = True
    finally:
        if not ok:
            run(conn, sql.SQL("ROLLBACK TO SAVEPOINT {}").format(ident))
        run(conn, sql.SQL("RELEASE SAVEPOINT {}").format(ident))
