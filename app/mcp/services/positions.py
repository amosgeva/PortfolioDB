"""Positions service — single source of truth for position-related MCP tools.

Wraps portfolio.compute_fifo_merged / compute_avg_cost_merged with a layer
that joins each merged row against the latest price snapshot to produce
market value, unrealized P&L, and portfolio weights.

Every tool in tools/positions_tools.py and the KPI service below routes its
position reads through this module so the numbers stay identical to the
Streamlit dashboard.
"""

from __future__ import annotations

import time

from datetime import date
from typing import Any

import pandas as pd
from psycopg2 import sql

# Reuse the engines unchanged.
from portfolio import compute_avg_cost_merged, compute_fifo_merged
import corporate_actions

from app.mcp.deps import get_conn
from app.mcp.services import common
from app.mcp.services import prices as prices_service
from app.mcp.services.cutoff import Cutoff

# ────────────────────────── lot loaders ──────────────────────────


def _fetch_lots(
    conn,
    *,
    account: str | None = None,
    symbol: str | None = None,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Pull lot rows in the column order portfolio.compute_*_merged expects."""
    # Compose from psycopg2.sql fragments rather than concatenating a raw
    # string; all user-supplied filters remain bound via %s.
    query = sql.SQL(
        "SELECT id, symbol, account, side, trade_date, quantity, price, fees "
        "FROM lots "
        "WHERE 1=1"
    )
    params: list[Any] = []
    if account is not None:
        query += sql.SQL(" AND account = %s")
        params.append(account)
    if symbol is not None:
        query += sql.SQL(" AND symbol = %s")
        params.append(symbol.upper())
    if as_of is not None:
        query += sql.SQL(" AND trade_date <= %s")
        params.append(as_of)
    query += sql.SQL(" ORDER BY symbol, COALESCE(account,''), trade_date, id")

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    lot_rows = [dict(zip(cols, r)) for r in rows]

    # Restate into post-split units before the engines see them. Quantity and
    # price move inversely, so cost basis and realized P&L are unchanged — only
    # share count and per-share cost are corrected.
    return corporate_actions.adjust_lot_rows(
        lot_rows, corporate_actions.fetch_actions(conn)
    )


def _engine(method: str):
    method = method.lower()
    if method == "fifo":
        return compute_fifo_merged
    if method in ("avg", "avg_cost"):
        return compute_avg_cost_merged
    raise ValueError(f"Unknown method '{method}'. Use 'fifo' or 'avg'.")


# ────────────────────────── public API ──────────────────────────


# Memoised positions frames, keyed by the inputs that determine them.
#
# Why it is safe: a Cutoff pins the data, so the same cutoff instant means the
# same lots and the same prices — the guarantee the cutoff exists to provide.
# Nothing is cached when cutoff is None, since "now" moves.
#
# Why it matters: a single review calls current_positions eight to ten times
# (the summary, concentration, four allocation dimensions, stress scenarios,
# data quality), each re-querying and re-running the FIFO engine. Caching halves
# the endpoint's latency.
#
# Why it expires: "same cutoff means same data" holds only while history is
# immutable, and this repo backfills — import_csv_history.py and the dated
# add_*.ps1 scripts insert lots with old trade dates. A row added behind a
# cutoff would otherwise be masked for the life of the process. A short TTL
# bounds that to seconds, well inside the 5-minute snapshot cadence, while still
# covering the burst of calls within one request.
_FRAME_CACHE: dict[tuple, tuple[float, pd.DataFrame]] = {}
_FRAME_CACHE_MAX = 32
_FRAME_CACHE_TTL_SECONDS = 60.0


def _cache_key(
    method: str, account: str | None, as_of: date | None, cutoff: Cutoff
) -> tuple:
    return (method, account, as_of, cutoff.ts)


def clear_frame_cache() -> None:
    """Drop the memoised frames. For tests that mutate the underlying data."""
    _FRAME_CACHE.clear()


def positions_dataframe(
    method: str = "fifo",
    *,
    account: str | None = None,
    as_of: date | None = None,
    cutoff: Cutoff | None = None,
) -> pd.DataFrame:
    """Return the merged positions DataFrame including market-value columns.

    Mirrors the streamlit_app pipeline at lines 507-522: load lots, run
    engine, left-join latest price, derive market_value and unrealized_pnl.

    A ``cutoff`` pins *both* halves at one instant — lots by its trade_date and
    prices by its ts. That pairing is the point: filtering lots to a past date
    while valuing them at today's prices produces a number that never existed.
    An explicit ``as_of`` still works and wins over the cutoff's trade_date,
    for callers that deliberately want one without the other.
    """
    engine = _engine(method)
    key = _cache_key(method, account, as_of, cutoff) if cutoff is not None else None
    if key is not None:
        entry = _FRAME_CACHE.get(key)
        if entry is not None and (time.monotonic() - entry[0]) < _FRAME_CACHE_TTL_SECONDS:
            # Copy: callers filter and assign columns on the result, and a
            # mutated cache entry would corrupt every later read.
            return entry[1].copy()

    effective_as_of = as_of if as_of is not None else (cutoff.trade_date if cutoff else None)
    with get_conn() as conn:
        lot_rows = _fetch_lots(conn, account=account, as_of=effective_as_of)
    merged = engine(lot_rows)

    # Bring the cutoff's price into the frame so callers can compute market
    # value without re-querying. None/NaN price is fine — left join keeps the row.
    latest = prices_service.latest_price_map_with_ts(
        as_of_ts=cutoff.ts if cutoff else None
    )
    merged["last_price"] = merged["symbol"].map(
        lambda s: latest.get(s, {}).get("last_price")
    )
    merged["last_price_ts"] = merged["symbol"].map(
        lambda s: latest.get(s, {}).get("ts")
    )

    merged["last_price"] = pd.to_numeric(merged["last_price"], errors="coerce")
    merged["qty"] = pd.to_numeric(merged["qty"], errors="coerce")
    merged["open_cost"] = pd.to_numeric(merged["open_cost"], errors="coerce")
    merged["market_value"] = merged["qty"] * merged["last_price"]
    merged["unrealized_pnl"] = merged["market_value"] - merged["open_cost"]

    # Portfolio weight against total market value (NaN rows excluded).
    total_mv = float(merged["market_value"].sum(skipna=True))
    merged["weight_pct"] = (
        (merged["market_value"] / total_mv * 100.0) if total_mv else 0.0
    )

    if key is not None:
        if len(_FRAME_CACHE) >= _FRAME_CACHE_MAX:
            # Bounded, and insertion-ordered: drop the oldest entry rather than
            # letting a long-lived server accumulate one frame per cutoff.
            _FRAME_CACHE.pop(next(iter(_FRAME_CACHE)))
        _FRAME_CACHE[key] = (time.monotonic(), merged.copy())
    return merged


def current_positions(
    method: str = "fifo",
    *,
    account: str | None = None,
    held_only: bool = True,
    as_of: date | None = None,
    cutoff: Cutoff | None = None,
) -> list[dict[str, Any]]:
    """Position rows as plain dicts ready for serialization."""
    df = positions_dataframe(method, account=account, as_of=as_of, cutoff=cutoff)
    if held_only:
        df = df[df["qty"] > 0]
    return _df_to_records(df)


def positions_summary(
    method: str = "fifo",
    *,
    account: str | None = None,
    as_of: date | None = None,
    cutoff: Cutoff | None = None,
) -> dict[str, Any]:
    """Aggregate totals across positions (used by KPIs and pnl summary)."""
    df = positions_dataframe(method, account=account, as_of=as_of, cutoff=cutoff)
    if df.empty:
        # Absolute totals are truly zero; the percentages are undefined without
        # a cost basis, so they are null rather than a misleading 0.0.
        return {
            "market_value": 0.0,
            "cost_basis": 0.0,
            "unrealized_pnl": 0.0,
            "unrealized_pct": None,
            "realized_pnl": 0.0,
            "active_symbols": 0,
            "total_return_pct": None,
            "null_reasons": {
                "unrealized_pct": "no_cost_basis",
                "total_return_pct": "no_cost_basis",
            },
        }
    market_value = float(df["market_value"].sum(skipna=True))
    cost_basis = float(df["open_cost"].sum())
    unrealized_pnl = float(df["unrealized_pnl"].sum(skipna=True))
    realized_pnl = float(df["realized_pnl"].sum()) if "realized_pnl" in df.columns else 0.0
    active = int((df["qty"] > 0).sum())
    unrealized_pct = (unrealized_pnl / cost_basis * 100.0) if cost_basis else None
    total_return_pct = (
        (realized_pnl + unrealized_pnl) / cost_basis * 100.0 if cost_basis else None
    )
    return {
        "market_value": market_value,
        "cost_basis": cost_basis,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pct": unrealized_pct,
        "realized_pnl": realized_pnl,
        "active_symbols": active,
        "total_return_pct": total_return_pct,
        "null_reasons": (
            {}
            if cost_basis
            else {"unrealized_pct": "no_cost_basis", "total_return_pct": "no_cost_basis"}
        ),
    }


def position_detail(symbol: str, method: str = "fifo") -> dict[str, Any]:
    """Per-symbol drill-down: merged numbers + per-account breakdown + open lots."""
    sym = symbol.upper()
    merged_row = next(
        (r for r in current_positions(method, held_only=False) if r["symbol"] == sym),
        None,
    )
    breakdown = _per_account_breakdown(sym, method)
    open_lots_list = open_lots(symbol=sym) if method == "fifo" else []
    trades = _symbol_trades(sym)
    return {
        "symbol": sym,
        "method": method,
        "merged": merged_row,
        "per_account": breakdown,
        "open_lots": open_lots_list,
        "recent_trades": trades,
    }


def _per_account_breakdown(symbol: str, method: str) -> list[dict[str, Any]]:
    """Run the engine per (symbol, account) so each account is reported separately.

    Derives the account list from the lots themselves (one DB round-trip) so
    a single SELECT covers both the account enumeration and the engine input.
    """
    engine = _engine(method)
    with get_conn() as conn:
        all_lots = _fetch_lots(conn, symbol=symbol)
    by_account: dict[str | None, list[dict[str, Any]]] = {}
    for r in all_lots:
        by_account.setdefault(r["account"], []).append(r)

    out: list[dict[str, Any]] = []
    for acct, lots in sorted(by_account.items(), key=lambda kv: (kv[0] is None, kv[0] or "")):
        df = engine(lots)
        if df.empty:
            continue
        row = df.iloc[0].to_dict()
        row["account"] = acct if acct is not None else "(none)"
        out.append(_clean_record(row))
    return out


def _symbol_trades(symbol: str, limit: int = 20) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, symbol, account, side, trade_date, quantity, price, fees, notes
                FROM lots WHERE symbol = %s
                ORDER BY trade_date DESC, id DESC
                LIMIT %s
                """,
                (symbol, limit),
            )
            cols = [d[0] for d in cur.description]
            return [_clean_record(dict(zip(cols, r))) for r in cur.fetchall()]


def open_lots(
    *, symbol: str | None = None, account: str | None = None
) -> list[dict[str, Any]]:
    """Remaining open BUY lots after FIFO matching, grouped by (symbol, account)."""
    from collections import defaultdict
    from decimal import Decimal

    from fifo import Lot, run_fifo

    with get_conn() as conn:
        lots = _fetch_lots(conn, account=account, symbol=symbol)

    # Group by (symbol, account) — same scoping the engine uses.
    grouped: dict[tuple[str, str | None], list[Lot]] = defaultdict(list)
    for r in lots:
        grouped[(r["symbol"], r["account"])].append(
            Lot(
                id=int(r["id"]),
                symbol=r["symbol"],
                account=r["account"],
                side=r["side"],
                trade_date=r["trade_date"],
                quantity=Decimal(str(r["quantity"])),
                price=Decimal(str(r["price"])),
                fees=Decimal(str(r["fees"])),
            )
        )

    out: list[dict[str, Any]] = []
    for (sym, acct), stream in grouped.items():
        result = run_fifo(stream)
        for ob in result.open_buys:
            out.append({
                "symbol": sym,
                "account": acct,
                "buy_lot_id": ob.buy_lot_id,
                "trade_date": ob.trade_date.isoformat(),
                "qty_remaining": float(ob.qty_remaining),
                "per_share_cost": float(ob.per_share_cost),
                "open_cost": float(ob.qty_remaining * ob.per_share_cost),
            })
    # Sort by symbol then trade_date for stable output.
    out.sort(key=lambda r: (r["symbol"], r["trade_date"], r["buy_lot_id"]))
    return out


# ────────────────────────── helpers ──────────────────────────


def _df_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Pandas → list[dict], with NaN→None and Timestamps→ISO strings."""
    if df.empty:
        return []
    records = df.to_dict(orient="records")
    return [_clean_record(r) for r in records]


# Shared with fundamentals — see common.clean_record.
_clean_record = common.clean_record
