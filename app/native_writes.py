"""Every database write the native dashboard pages make, and the checks before them.

Kept apart from the rendering in ``modern2_native.py`` so each write can be
tested against a real database without a browser, and so the rules below hold
for every call site rather than for whichever form remembered them:

* **One transaction per user action** (audit N-6). Adding a lot used to commit
  the instrument row and the lot separately; saving Settings committed once
  per field. :func:`transaction` wraps the pooled connection so a failure
  leaves nothing half-written.
* **Decimal end to end.** Quantities, prices, fees, cash and alert levels
  arrive as text and go through ``ledger_numbers`` — never ``float`` — so what
  the operator typed is what is stored.
* **Duplicates are reported, not swallowed** (N-2). The dedupe index makes a
  second identical fill a no-op; :func:`add_lot` returns whether a row was
  actually inserted, from the statement's row count.
* **Deletion leaves a record** (A-9). :func:`delete_lot` removes the lot and
  writes the full row, the time and a reason to ``lot_deletions`` in one
  statement.

None of these functions checks the password: the page calls
``native_auth.require_unlocked`` before opening the transaction. They take a
connection and leave committing to :func:`transaction`.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import ledger_numbers
import oversell
import symbol_paths
from db import fetch_all, run

# ── transactions ────────────────────────────────────────────────────────────


@contextmanager
def transaction(get_conn: Callable, put_conn: Callable) -> Iterator:
    """A pooled connection holding one transaction: commit on success, roll
    back on any error, and always return the connection to the pool."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ── input parsing ───────────────────────────────────────────────────────────


def normalize_symbol(raw) -> str:
    """Uppercase, strip, and refuse anything that is not a symbol.

    ``symbol_paths.normalize_symbol`` is the shared rule once it exists; until
    then this applies the same allowlist through ``is_safe_symbol``. Either
    way the error is a ValueError with a message fit to show.
    """
    shared = getattr(symbol_paths, "normalize_symbol", None)
    if shared is not None:
        return shared(raw)
    sym = (raw or "").strip().upper() if isinstance(raw, str) else ""
    if not sym:
        raise ValueError("Symbol is required.")
    if not symbol_paths.is_safe_symbol(sym):
        raise ValueError(
            f"{sym!r} is not a symbol: use A–Z, 0–9 and . - = ^ _ "
            "(up to 32 characters, starting with a letter, digit or ^)."
        )
    return sym


def parse_fees(raw) -> Decimal:
    text = "" if raw is None else str(raw).strip()
    return ledger_numbers.parse_decimal(text or "0", field="fees", minimum=0)


def parse_cash(raw) -> Decimal:
    return ledger_numbers.parse_decimal(raw, field="cash", minimum=0)


def parse_level(raw) -> Decimal:
    return ledger_numbers.parse_decimal(raw, field="level", minimum=0, exclusive_minimum=True)


@dataclass(frozen=True)
class LotInput:
    symbol: str
    account: str | None
    side: str
    trade_date: date
    quantity: Decimal
    price: Decimal
    fees: Decimal
    notes: str | None


def parse_lot(*, symbol, account, side, trade_date, quantity, price, fees, notes) -> LotInput:
    """Validate the Add Lot form. Raises ValueError with a showable message."""
    if side not in ("BUY", "SELL"):
        raise ValueError(f"Side must be BUY or SELL, got {side!r}.")
    if not isinstance(trade_date, date):
        raise ValueError("Trade date is required.")
    price_d = ledger_numbers.parse_price(price)
    if price_d <= 0:
        raise ValueError("Price must be greater than 0.")
    acct = (account or "").strip() or None
    return LotInput(
        symbol=normalize_symbol(symbol),
        account=acct,
        side=side,
        trade_date=trade_date,
        quantity=ledger_numbers.parse_quantity(quantity),
        price=price_d,
        fees=parse_fees(fees),
        notes=(notes or "").strip() or None,
    )


# ── lots ────────────────────────────────────────────────────────────────────


def add_lot(conn, lot: LotInput) -> bool:
    """Insert one lot (and its instrument row). True if a row was inserted,
    False if the dedupe index already held an identical fill."""
    run(conn, "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT (symbol) DO NOTHING", (lot.symbol,))
    inserted = run(
        conn,
        """
        INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (lot.symbol, lot.account, lot.side, lot.trade_date, lot.quantity, lot.price, lot.fees, lot.notes),
    )
    return inserted == 1


def _plain(d: Decimal) -> str:
    return format(d.normalize(), "f")


def sell_warnings(conn, lot: LotInput) -> list[str]:
    """What the operator should see before a SELL is recorded (audit N-7).

    Two mistakes this catches, both of which FIFO would otherwise absorb
    silently: a sale bigger than the position in that account, and a sale with
    no account while the shares sit in a named one — FIFO matches within one
    (symbol, account) stream, so such a sale reduces nothing.
    """
    if lot.side != "SELL":
        return []
    warnings: list[str] = []
    if lot.account is None:
        rows = fetch_all(
            conn,
            "SELECT DISTINCT account FROM lots WHERE symbol = %s AND account IS NOT NULL ORDER BY 1",
            (lot.symbol,),
        )
        named = {
            r["account"]: oversell.open_quantity(conn, lot.symbol, r["account"], lot.trade_date)
            for r in rows
        }
        held_named = {a: q for a, q in named.items() if q > 0}
        # oversell.open_quantity(account=None) is every account together (the
        # loader treats None as "no filter"), so the no-account stream is the
        # total less the named ones.
        total = oversell.open_quantity(conn, lot.symbol, None, lot.trade_date)
        held_none = total - sum(named.values(), Decimal(0))
        if held_named:
            where = ", ".join(f"{a} ({_plain(q)})" for a, q in held_named.items())
            warnings.append(
                f"No account chosen, but {lot.symbol} is held in {where}. Sales are matched "
                "within one account, so this one will not reduce that position. Pick the "
                "account the shares are in."
            )
        if lot.quantity > held_none:
            warnings.append(
                f"Selling {_plain(lot.quantity)} {lot.symbol} with no account, but the ledger "
                f"holds {_plain(held_none)} with no account as of {lot.trade_date.isoformat()}. "
                "Shorts are not supported: the excess is dropped and realized P&L understated."
            )
        return warnings
    msg = oversell.oversell_warning(conn, lot.symbol, lot.account, lot.trade_date, lot.quantity)
    if msg:
        warnings.append(msg.removeprefix("WARNING: "))
    return warnings


def find_lot(conn, lot_id: int) -> dict | None:
    rows = fetch_all(
        conn,
        "SELECT id, symbol, account, side, trade_date, quantity, price, fees, notes FROM lots WHERE id = %s",
        (lot_id,),
    )
    return rows[0] if rows else None


def delete_lot(conn, lot_id: int, reason: str) -> int | None:
    """Delete a lot and record it in ``lot_deletions``, atomically.

    One statement, so there is no window in which the lot is gone and the
    record is not (or the reverse). Returns the audit row id, or None when no
    lot had that id.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Give a reason for the deletion; it is kept with the deleted row.")
    rows = fetch_all(
        conn,
        """
        WITH gone AS (DELETE FROM lots WHERE id = %s RETURNING *)
        INSERT INTO lot_deletions(lot_id, lot, reason)
        SELECT id, to_jsonb(gone), %s FROM gone
        RETURNING id
        """,
        (lot_id, reason),
    )
    return rows[0]["id"] if rows else None


def recent_deletions(conn, limit: int = 10) -> list[dict]:
    return fetch_all(
        conn,
        "SELECT id, lot_id, lot, reason, deleted_at FROM lot_deletions ORDER BY deleted_at DESC, id DESC LIMIT %s",
        (limit,),
    )


# ── watchlist, cash ─────────────────────────────────────────────────────────


def set_watchlist(conn, symbol: str, on: bool) -> None:
    run(conn, "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT (symbol) DO NOTHING", (symbol,))
    run(conn, "UPDATE instruments SET watchlist = %s, updated_at = now() WHERE symbol = %s", (on, symbol))


def record_cash(conn, account: str, cash: Decimal, note: str | None) -> None:
    account = (account or "").strip()
    if not account:
        raise ValueError("Account is required.")
    run(conn, "INSERT INTO cash_snapshots(account, cash, note) VALUES (%s, %s, %s)",
        (account, cash, (note or "").strip() or None))


# ── price alerts ────────────────────────────────────────────────────────────


def add_alert(conn, symbol: str, direction: str, level: Decimal, note: str | None,
              also_watch: bool = False) -> int:
    """Arm a persisted price alert. The symbol must be one the ledger knows —
    an alert on a symbol nobody collects could never fire — unless
    ``also_watch`` adds it to the watchlist in the same transaction."""
    if direction not in ("above", "below"):
        raise ValueError(f"Direction must be above or below, got {direction!r}.")
    known = fetch_all(conn, "SELECT watchlist FROM instruments WHERE symbol = %s", (symbol,))
    if also_watch:
        set_watchlist(conn, symbol, True)
    elif not known:
        raise ValueError(
            f"{symbol} is not in the ledger, so no price is collected for it. Tick "
            "'also add to the watchlist' to start collecting it."
        )
    rows = fetch_all(
        conn,
        "INSERT INTO price_alerts(symbol, direction, level, note) VALUES (%s, %s, %s, %s) RETURNING id",
        (symbol, direction, level, (note or "").strip() or None),
    )
    return rows[0]["id"]


def deactivate_alert(conn, alert_id: int) -> bool:
    return run(conn, "UPDATE price_alerts SET active = FALSE WHERE id = %s AND active", (alert_id,)) == 1


def list_alerts(conn) -> list[dict]:
    return fetch_all(
        conn,
        """
        SELECT id, symbol, direction, level, note, active, created_at
        FROM price_alerts ORDER BY active DESC, created_at DESC, id DESC
        """,
    )


# ── settings, chat ──────────────────────────────────────────────────────────


def save_settings(conn, changes: Mapping[str, str | None]) -> None:
    """Apply every Settings-page change in the caller's transaction: a value
    upserts the row, None deletes it so `.env` / the default applies again."""
    for key, value in changes.items():
        if value is None:
            run(conn, "DELETE FROM settings WHERE key = %s", (key,))
        else:
            run(
                conn,
                """
                INSERT INTO settings(key, value, updated_at) VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (key, value),
            )


def reset_chat(conn, conversation_id: str = "default") -> int:
    return run(conn, "DELETE FROM chat_log WHERE conversation_id = %s", (conversation_id,))


def latest_chat(conn, limit: int = 100, conversation_id: str = "default") -> tuple[list[dict], int]:
    """The newest ``limit`` messages in conversation order, and the total.

    The page used to take the *first* hundred (ORDER BY id LIMIT 100), so from
    message 101 a new exchange vanished on the next rerun (audit N-3).
    """
    rows = fetch_all(
        conn,
        """
        SELECT role, content FROM (
          SELECT id, role, content FROM chat_log
          WHERE conversation_id = %s ORDER BY id DESC LIMIT %s
        ) newest ORDER BY id ASC
        """,
        (conversation_id, limit),
    )
    total = fetch_all(conn, "SELECT count(*) AS n FROM chat_log WHERE conversation_id = %s", (conversation_id,))
    return rows, int(total[0]["n"]) if total else 0


# ── exports ─────────────────────────────────────────────────────────────────

# A cell a spreadsheet would read as a formula (audit A-4). Notes are free text
# and can arrive from an imported broker CSV.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Neutralise a text cell a spreadsheet would evaluate. Numbers pass
    through untouched — a negative quantity is data, not a formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_LEAD):
        return "'" + value
    return value


def lots_csv(rows: list[Mapping]) -> bytes:
    import csv

    if not rows:
        return b""
    fields = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(fields)
    for r in rows:
        writer.writerow([csv_safe(r[f]) for f in fields])
    return buf.getvalue().encode("utf-8")
