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

import account_names
import corporate_actions
import holdings
import ledger_inputs
import ledger_numbers
import ledger_writes
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
    """The ledger's one symbol rule (``symbol_paths.normalize_symbol``): strip,
    uppercase, and refuse anything not shaped like a ticker. ValueError with a
    message fit to show."""
    return symbol_paths.normalize_symbol(raw)


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
    # Blank is "no account" (NULL); '(none)' — that bucket's label — is refused
    # as a name (account_names; rescore NEW-1).
    acct = account_names.normalize_account(account)
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
    """Insert one lot (and its instrument row) through the shared writer.
    True if a row was inserted, False if the dedupe index already held an
    identical fill — then nothing was written."""
    ledger_writes.ensure_instrument(conn, lot.symbol)
    return ledger_writes.insert_lot(
        conn, symbol=lot.symbol, account=lot.account, side=lot.side, trade_date=lot.trade_date,
        quantity=lot.quantity, price=lot.price, fees=lot.fees, notes=lot.notes,
    )


def duplicate_message(lot: LotInput) -> str:
    """The shared wording for a fill the dedupe index refused (the CLIs say the same)."""
    return ledger_writes.duplicate_lot_message(
        symbol=lot.symbol, account=lot.account, side=lot.side, trade_date=lot.trade_date,
        quantity=lot.quantity, price=lot.price,
    )


def _plain(d: Decimal) -> str:
    return format(d.normalize(), "f")


def lot_warnings(conn, lot: LotInput) -> list[str]:
    """What the operator should see, and confirm, before a lot is recorded.

    * Outside the USD-only scope (``ledger_writes.currency_scope_problem``):
      the CLIs refuse unless told ``--allow-non-usd``; here the operator
      confirms instead.
    * A SELL bigger than the position in that account, or one that starves a
      sale already recorded later (``oversell.check_sell``; audit N-7).
    * A SELL with no account while the shares sit in a named one: FIFO matches
      within one (symbol, account) stream, so such a sale reduces nothing.
      ``account=None`` is the unassigned stream, never "every account".
    * A SELL that closes the last shares of an unwatched symbol with armed
      price alerts, which then stop being checked (audit U-1).
    """
    warnings: list[str] = []
    problem = ledger_writes.currency_scope_problem(conn, lot.symbol)
    if problem:
        warnings.append(
            f"{problem}. PortfolioDB is USD-only — it has no FX conversion, so this position "
            "would be added to every total as if its price were in dollars. Record it in USD "
            "(converted price) unless you accept that."
        )
    if lot.side != "SELL":
        return warnings
    if lot.account is None:
        rows = fetch_all(
            conn,
            "SELECT DISTINCT account FROM lots WHERE symbol = %s AND account IS NOT NULL ORDER BY 1",
            (lot.symbol,),
        )
        held_named = {
            r["account"]: q for r in rows
            if (q := oversell.open_quantity(conn, lot.symbol, r["account"], lot.trade_date)) > 0
        }
        if held_named:
            where = ", ".join(f"{a} ({_plain(q)})" for a, q in held_named.items())
            warnings.append(
                f"No account chosen, but {lot.symbol} is held in {where}. Sales are matched "
                "within one account, so this one will not reduce that position. Pick the "
                "account the shares are in."
            )
    check = oversell.check_sell(conn, lot.symbol, lot.account, lot.trade_date, lot.quantity)
    msg = check.message()
    if msg:
        warnings.append(msg.replace("WARNING: ", ""))
    warnings += _stranded_by_sale(conn, lot)
    return warnings


def held_today_with(conn, lot: LotInput) -> tuple[Decimal, Decimal]:
    """Today's open quantity of ``lot.symbol``, without and with ``lot``.

    Both figures come from one prepared ledger in today's share units, by the
    engines' ordering and truncation rules: the existing lots as
    ``ledger_inputs.load`` restates them, plus the candidate restated with the
    same corporate actions and placed after every lot already on its date
    (where its id will put it) — the construction ``oversell.check_sell``
    uses. A trade-date quantity is never compared with a restated one: a
    backdated sale of 10 before a 2:1 split closes a position that reads 20
    today (QA R3-B1).
    """
    ledger = ledger_inputs.load(conn, symbol=lot.symbol)
    candidate = corporate_actions.adjust_lot_rows(
        [{"id": float("inf"), "symbol": lot.symbol, "account": lot.account, "side": lot.side,
          "trade_date": lot.trade_date, "quantity": lot.quantity, "price": lot.price,
          "fees": lot.fees}],
        ledger.actions,
    )[0]
    before = holdings.open_quantities(ledger.lots).get(lot.symbol, Decimal(0))
    after = holdings.open_quantities(ledger.lots + [candidate]).get(lot.symbol, Decimal(0))
    return before, after


def _stranded_by_sale(conn, lot: LotInput) -> list[str]:
    """A sale that closes the last shares of an unwatched symbol stops its
    price collection, and with it every armed alert on it."""
    watched = fetch_all(conn, "SELECT watchlist FROM instruments WHERE symbol = %s", (lot.symbol,))
    if watched and watched[0]["watchlist"]:
        return []
    before, after = held_today_with(conn, lot)
    if before <= 0 or after > 0:
        return []
    n = armed_alert_count(conn, lot.symbol)
    if not n:
        return []
    return [
        f"This sale closes the position in {lot.symbol}, which is not on the watchlist, so its "
        f"price stops being collected and {n} armed alert{'s' if n != 1 else ''} on it can no "
        "longer fire. Add it to the watchlist to keep them working."
    ]


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
    ledger_writes.ensure_instrument(conn, symbol)
    run(conn, "UPDATE instruments SET watchlist = %s, updated_at = now() WHERE symbol = %s", (on, symbol))


def record_cash(conn, account: str, cash: Decimal, note: str | None) -> None:
    account = account_names.normalize_account(account)
    if not account:
        raise ValueError("Account is required.")
    run(conn, "INSERT INTO cash_snapshots(account, cash, note) VALUES (%s, %s, %s)",
        (account, cash, (note or "").strip() or None))


# ── price alerts ────────────────────────────────────────────────────────────


# The collector's own rule for which symbols it fetches
# (snapshot_prices.held_or_watched_symbols): a positive open quantity by the engines'
# rules (ledger_inputs.open_symbols), or the watchlist flag. The SQL mirror of
# that open quantity is lot_open_quantities() in schema.sql, which a database
# test holds to the Python engines (audit R-1); it is used here so the alert
# list can join on it.
_NET_QTY_SQL = """
    SELECT COALESCE(SUM(quantity), 0) AS qty FROM lot_open_quantities() WHERE symbol = %s
"""


def net_quantity(conn, symbol: str) -> Decimal:
    return Decimal(str(fetch_all(conn, _NET_QTY_SQL, (symbol,))[0]["qty"]))


def is_collected(conn, symbol: str) -> bool:
    """Whether the price collector fetches ``symbol``: held (net quantity
    above zero) or watchlisted. An alert on anything else can never fire."""
    row = fetch_all(conn, "SELECT watchlist FROM instruments WHERE symbol = %s", (symbol,))
    if not row:
        return False
    return bool(row[0]["watchlist"]) or net_quantity(conn, symbol) > 0


def armed_alert_count(conn, symbol: str) -> int:
    return int(fetch_all(
        conn, "SELECT count(*) AS n FROM price_alerts WHERE symbol = %s AND active", (symbol,)
    )[0]["n"])


def alerts_stranded_by_unwatch(conn, symbol: str) -> int:
    """Armed alerts that would stop being checked if ``symbol`` left the
    watchlist: nonzero only when the watchlist is its last reason to be
    collected."""
    if net_quantity(conn, symbol) > 0:
        return 0
    return armed_alert_count(conn, symbol)


def add_alert(conn, symbol: str, direction: str, level: Decimal, note: str | None,
              also_watch: bool = False) -> int:
    """Arm a persisted price alert on a symbol the collector fetches.

    Being in ``instruments`` is not enough: a sold holding, a symbol taken off
    the watchlist, or an instrument-only row is not collected, and an alert on
    it would sit "armed" forever. ``also_watch`` puts the symbol on the
    watchlist in the same transaction, which makes it collected.
    """
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
    elif not is_collected(conn, symbol):
        raise ValueError(
            f"{symbol} is in the ledger but its price is not collected: it is neither held "
            "nor on the watchlist, so this alert could never fire. Tick 'also add to the "
            "watchlist' to start collecting it."
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
    """Every alert, armed first, each with whether its symbol is collected
    (the same rule as is_collected), so the page can say when an armed alert
    cannot fire."""
    return fetch_all(
        conn,
        """
        WITH pos AS (
          SELECT symbol, SUM(quantity) AS qty FROM lot_open_quantities() GROUP BY symbol
        )
        SELECT a.id, a.symbol, a.direction, a.level, a.note, a.active, a.created_at,
               (COALESCE(p.qty, 0) > 0 OR COALESCE(i.watchlist, FALSE)) AS collected
        FROM price_alerts a
        LEFT JOIN instruments i ON i.symbol = a.symbol
        LEFT JOIN pos p ON p.symbol = a.symbol
        ORDER BY a.active DESC, a.created_at DESC, a.id DESC
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
