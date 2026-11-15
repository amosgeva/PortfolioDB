"""Activity service — lot lookups, recent trades, cash history."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from psycopg2 import sql

from app.mcp.deps import get_conn
from app.mcp.services import common
from app.mcp.services.cutoff import LOCAL_TZ

# Largest page a caller may ask for. The ledger is personal-scale; a request
# for more is a mistake or an attempt to make the server do a lot of work.
MAX_ROWS = 1000


def _day_bounds(since: date | None, until: date | None) -> tuple[datetime | None, datetime | None]:
    """[since 00:00, until+1 00:00) in the reporting timezone, as instants.

    A bare date compared with a timestamptz is midnight at the *start* of that
    day in the server's zone, so ``until=2026-08-01`` used to stop at the first
    instant of the 1st and drop the whole day it named (audit X-10). Both ends
    are now whole reporting-timezone days, ``until`` inclusive.
    """
    if since is not None and until is not None and since > until:
        raise ValueError(f"since ({since}) is after until ({until})")
    lo = datetime.combine(since, time.min, tzinfo=LOCAL_TZ) if since is not None else None
    hi = (
        datetime.combine(until + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
        if until is not None else None
    )
    return lo, hi


# ────────────────────────── lots ──────────────────────────


def lots(
    *,
    symbol: str | None = None,
    account: str | None = None,
    side: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Raw lot rows filtered by any combination of fields."""
    common.check_limit("limit", limit, MAX_ROWS)
    if since is not None and until is not None and since > until:
        raise ValueError(f"since ({since}) is after until ({until})")
    if side is not None and side.upper() not in ("BUY", "SELL"):
        raise ValueError("side must be 'BUY' or 'SELL'")
    # Build the statement from psycopg2.sql fragments so the query is never a
    # formatted/concatenated raw string. Every user value is still bound via %s.
    query = sql.SQL(
        "SELECT id, symbol, account, side, trade_date, quantity, price, fees, notes, created_at "
        "FROM lots WHERE 1=1"
    )
    params: list[Any] = []
    if symbol is not None:
        query += sql.SQL(" AND symbol = %s")
        params.append(symbol.upper())
    query, params = common.add_account_filter(query, params, account)
    if side is not None:
        query += sql.SQL(" AND side = %s")
        params.append(side.upper())
    if since is not None:
        query += sql.SQL(" AND trade_date >= %s")
        params.append(since)
    if until is not None:
        query += sql.SQL(" AND trade_date <= %s")
        params.append(until)
    query += sql.SQL(" ORDER BY trade_date DESC, id DESC LIMIT %s")
    params.append(limit)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
    return [_lot_row(dict(zip(cols, r))) for r in rows]


def recent_trades(limit: int = 20, since: date | None = None) -> list[dict[str, Any]]:
    """Most recent BUY/SELL trades across all symbols."""
    return lots(since=since, limit=limit)


def trading_activity(
    group_by: str = "month",
    *,
    since: date | None = None,
    until: date | None = None,
) -> list[dict[str, Any]]:
    """Aggregate trade counts and notional value by month / symbol / account.

    `notional` = Σ (quantity × price). Fees not included — they're tracked
    separately because they roll into cost basis or proceeds via the engine.
    """
    if group_by not in ("month", "symbol", "account"):
        raise ValueError("group_by must be 'month', 'symbol', or 'account'")

    # group_expr is selected from a fixed whitelist of trusted SQL fragments
    # (never user text), so wrapping it in sql.SQL is safe; it is composed into
    # the statement rather than formatted into a raw string.
    group_expr = {
        "month": sql.SQL("to_char(date_trunc('month', trade_date), 'YYYY-MM')"),
        "symbol": sql.SQL("symbol"),
        "account": sql.SQL("COALESCE(account, '(none)')"),
    }[group_by]

    params: list[Any] = []
    query = (
        sql.SQL("SELECT ")
        + group_expr
        + sql.SQL(
            """ AS bucket,
               COUNT(*)                                          AS trades,
               COUNT(*) FILTER (WHERE side = 'BUY')              AS buys,
               COUNT(*) FILTER (WHERE side = 'SELL')             AS sells,
               COALESCE(SUM(CASE WHEN side='BUY'  THEN quantity*price END), 0) AS buy_notional,
               COALESCE(SUM(CASE WHEN side='SELL' THEN quantity*price END), 0) AS sell_notional,
               COALESCE(SUM(fees), 0)                                          AS total_fees,
               MIN(trade_date)                                   AS first_trade,
               MAX(trade_date)                                   AS last_trade
        FROM lots
        WHERE 1=1
    """
        )
    )
    if since is not None:
        query += sql.SQL(" AND trade_date >= %s")
        params.append(since)
    if until is not None:
        query += sql.SQL(" AND trade_date <= %s")
        params.append(until)
    query += sql.SQL(" GROUP BY ") + group_expr + sql.SQL(" ORDER BY bucket")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "bucket": r["bucket"],
            "trades": int(r["trades"]),
            "buys": int(r["buys"]),
            "sells": int(r["sells"]),
            "buy_notional": float(r["buy_notional"]),
            "sell_notional": float(r["sell_notional"]),
            "total_fees": float(r["total_fees"]),
            "first_trade": r["first_trade"].isoformat() if r.get("first_trade") else None,
            "last_trade": r["last_trade"].isoformat() if r.get("last_trade") else None,
        })
    return out


# ────────────────────────── cash ──────────────────────────


def cash_balance(account: str | None = None) -> dict[str, Any]:
    """Current cash. None = merged across all accounts (sum of latest per account)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                # Latest row per account — a global ORDER BY ts LIMIT N would
                # drop accounts whose newest snapshot falls outside the N rows.
                """
                SELECT DISTINCT ON (account) account, cash, ts
                FROM cash_snapshots
                ORDER BY account, ts DESC
                """,
            )
            rows = cur.fetchall()

    latest: dict[str, dict[str, Any]] = {}
    for acct, cash, ts in rows:
        key = acct or "(merged)"
        if key not in latest:
            latest[key] = {"account": key, "cash": float(cash), "ts": ts.isoformat() if ts else None}

    if account is not None:
        rec = latest.get(account)
        return rec or {"account": account, "cash": 0.0, "ts": None}

    total = sum(r["cash"] for r in latest.values())
    return {
        "account": None,  # merged
        "total_cash": float(total),
        "by_account": list(latest.values()),
    }


def cash_history(
    account: str | None = None,
    *,
    since: date | None = None,
    until: date | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    # cash_snapshots in production has columns (ts, account, cash, currency,
    # note) — there is no `id` (the schema.sql file in the repo is drifted).
    # Query only what exists.
    common.check_limit("limit", limit, MAX_ROWS)
    lo, hi = _day_bounds(since, until)
    query = sql.SQL("SELECT ts, account, cash, note FROM cash_snapshots WHERE 1=1")
    params: list[Any] = []
    query, params = common.add_account_filter(query, params, account)
    if lo is not None:
        query += sql.SQL(" AND ts >= %s")
        params.append(lo)
    if hi is not None:
        query += sql.SQL(" AND ts < %s")
        params.append(hi)
    query += sql.SQL(" ORDER BY ts DESC LIMIT %s")
    params.append(limit)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    return [{
        "ts": r[0].isoformat() if r[0] else None,
        "account": r[1],
        "cash": float(r[2]),
        # Free text typed by the operator (audit M-6).
        "note": common.untrusted(r[3]),
    } for r in rows]


# ────────────────────────── helpers ──────────────────────────


def _lot_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(r["id"]),
        "symbol": r["symbol"],
        "account": r.get("account"),
        "side": r["side"],
        "trade_date": r["trade_date"].isoformat() if r.get("trade_date") else None,
        "quantity": float(r["quantity"]) if r.get("quantity") is not None else None,
        "price": float(r["price"]) if r.get("price") is not None else None,
        "fees": float(r["fees"]) if r.get("fees") is not None else 0.0,
        # Free text: typed by the operator or carried in from a broker CSV.
        "notes": common.untrusted(r.get("notes")),
        "created_at": (
            r["created_at"].isoformat()
            if isinstance(r.get("created_at"), datetime)
            else None
        ),
    }
