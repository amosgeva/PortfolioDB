"""Positions service — single source of truth for position-related MCP tools.

Wraps portfolio.compute_fifo_merged / compute_avg_cost_merged with a layer
that joins each merged row against the latest price snapshot to produce
market value, unrealized P&L, and portfolio weights.

Every tool in tools/positions_tools.py and the KPI service below routes its
position reads through this module so the numbers stay identical to the
Streamlit dashboard.
"""

from __future__ import annotations

import threading
import time

from datetime import date, datetime
from typing import Any

import pandas as pd
from psycopg2 import sql

# Reuse the engines unchanged.
from portfolio import compute_avg_cost_merged, compute_fifo_merged
import corporate_actions
import ledger_inputs
import portfolio_kpis

from app.mcp import deps
from app.mcp.deps import get_conn, snapshot_memo
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
    actions: list | None = None,
) -> list[dict[str, Any]]:
    """Pull lot rows in the column order portfolio.compute_*_merged expects.

    Composed on the parity contract's own query (ledger_inputs.LOTS_SQL /
    LOTS_ORDER) with this reader's filters added; all values stay bound.
    """
    query = ledger_inputs.LOTS_SQL
    params: list[Any] = []
    query, params = common.add_account_filter(query, params, account)
    if symbol is not None:
        query += sql.SQL(" AND symbol = %s")
        params.append(symbol.upper())
    if as_of is not None:
        query += sql.SQL(" AND trade_date <= %s")
        params.append(as_of)
    query += ledger_inputs.LOTS_ORDER

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    lot_rows = [dict(zip(cols, r)) for r in rows]

    # Restate into the units of the observation date, through the same
    # preparation step every other surface uses (app/ledger_inputs.py). With
    # `as_of` only actions on or before that date apply: this used to apply
    # every recorded split to a historical cutoff, so "the day before a 2:1"
    # reported twenty shares against a pre-split quote (re-audit F03, case A).
    # Quantity and price move inversely, so cost basis and realized P&L are
    # unchanged — only share count and per-share cost are corrected.
    if actions is None:
        actions = corporate_actions.fetch_actions(conn)
    return ledger_inputs.prepare(lot_rows, actions, as_of=as_of).lots


def _engine(method: str):
    if common.normalize_method(method) == "fifo":
        return compute_fifo_merged
    return compute_avg_cost_merged


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
#
# Why it is locked: tools run on worker threads, and the evict-then-insert below
# is a compound mutation of a shared dict (audit X-10).
#
# Inside deps.snapshot() the global cache is bypassed entirely: the snapshot's
# reads must all come from its own REPEATABLE READ transaction, and a frame
# cached from an earlier transaction would let one section of a review see
# data the others do not (audit Q-6). The snapshot keeps its own memo instead,
# so a review still computes each frame once.
_FRAME_CACHE: dict[tuple, tuple[float, pd.DataFrame]] = {}
_FRAME_CACHE_MAX = 32
_FRAME_CACHE_TTL_SECONDS = 60.0
_FRAME_CACHE_LOCK = threading.Lock()


def _cache_key(
    method: str, account: str | None, as_of: date | None, cutoff: Cutoff
) -> tuple:
    return (common.normalize_method(method), account, as_of, cutoff.ts)


def clear_frame_cache() -> None:
    """Drop the memoised frames. For tests that mutate the underlying data."""
    with _FRAME_CACHE_LOCK:
        _FRAME_CACHE.clear()


def _cache_get(key: tuple) -> pd.DataFrame | None:
    local = snapshot_memo()
    if local is not None:
        frame = local.get(("positions_frame", key))
        return frame.copy() if frame is not None else None
    with _FRAME_CACHE_LOCK:
        entry = _FRAME_CACHE.get(key)
        if entry is not None and (time.monotonic() - entry[0]) < _FRAME_CACHE_TTL_SECONDS:
            # Copy: callers filter and assign columns on the result, and a
            # mutated cache entry would corrupt every later read.
            return entry[1].copy()
    return None


def _cache_put(key: tuple, frame: pd.DataFrame) -> None:
    local = snapshot_memo()
    if local is not None:
        local[("positions_frame", key)] = frame.copy()
        return
    with _FRAME_CACHE_LOCK:
        if key not in _FRAME_CACHE and len(_FRAME_CACHE) >= _FRAME_CACHE_MAX:
            # Bounded, and insertion-ordered: drop the oldest entry rather than
            # letting a long-lived server accumulate one frame per cutoff.
            _FRAME_CACHE.pop(next(iter(_FRAME_CACHE)))
        _FRAME_CACHE[key] = (time.monotonic(), frame.copy())


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
        cached = _cache_get(key)
        if cached is not None:
            return cached

    effective_as_of = as_of if as_of is not None else (cutoff.trade_date if cutoff else None)
    # One database state for the actions, the lots and the quotes: they are
    # read in three steps and must all be restated with the same actions (QA of
    # PR #126 — a split approved between two reads mixed units). The actions
    # are read once and handed to the lot loader, not re-read.
    with deps.snapshot():
        merged, latest, actions = _read_frame(engine, account, effective_as_of, cutoff)
    return _finish_frame(merged, latest, actions, key)


def _read_frame(engine, account, effective_as_of, cutoff):
    with get_conn() as conn:
        recorded = corporate_actions.fetch_actions(conn)
        lot_rows = _fetch_lots(
            conn, account=account, as_of=effective_as_of, actions=recorded
        )
        # The actions that had happened by the observation date — the same set
        # _fetch_lots restated the lots with — so the quotes below can be put in
        # the same units.
        actions = ledger_inputs.prepare([], recorded, as_of=effective_as_of).actions
    merged = engine(lot_rows)
    # The total-return denominator, from exactly the lots this frame values
    # (same account and date filter, restated alike): docs/methodology.md §12.
    merged.attrs["invested_capital"] = portfolio_kpis.peak_net_invested(lot_rows)

    # Bring the cutoff's price into the frame so callers can compute market
    # value without re-querying. None/NaN price is fine — left join keeps the row.
    latest = prices_service.latest_price_map_with_ts(
        as_of_ts=cutoff.ts if cutoff else None
    )
    return merged, latest, actions


def _as_datetime(ts) -> datetime | None:
    """A quote timestamp as a datetime: the latest-price map hands one over,
    and an ISO string (as a serialised record carries) is parsed rather than
    silently skipping the restatement."""
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None
    return None


def _finish_frame(merged, latest, actions, key):
    # A quote is in the units of the moment it was observed. The latest quote
    # at or before the cutoff is normally after every applicable action and
    # needs nothing; a *stale* one — last observed before an ex-date that the
    # cutoff is past — is in the old units and would be joined to restated
    # shares. Restate it with the actions that lie between it and the cutoff.
    if actions:
        stamped = [
            (_as_datetime(v["ts"]), sym, float(v["last_price"]))
            for sym, v in latest.items()
            if v.get("last_price") is not None and _as_datetime(v.get("ts")) is not None
        ]
        for _ts, sym, price in corporate_actions.adjust_price_points(stamped, actions):
            latest[sym] = {**latest[sym], "last_price": price}
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
        _cache_put(key, merged)
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
    include_income: bool = True,
) -> dict[str, Any]:
    """Aggregate totals across positions, by the dashboard's definitions.

    The same definitions the dashboard, the Advisor and the reports compute
    through app/portfolio_kpis.py (docs/methodology.md §12 and §14):

    * market value and unrealized P&L over **priced** rows only; a held
      position with no quote is not a loss — its open cost is reported as
      ``unpriced_cost_basis`` and its symbol in ``unpriced_symbols``;
    * ``unrealized_pct`` over the **priced** cost basis it was earned on;
    * ``total_return_pct`` = (realized + unrealized) / peak net invested
      capital of the same account- and date-filtered lots, and
      ``total_return_with_income_pct`` adds USD income received by the same
      date. The old ratio divided by the cost still open, so it exploded after
      a large sale and read 0% after a full one (audit F-5 / Q-4).

    ``include_income=False`` skips the income read for callers that only need
    the valuation (allocation buckets, fee drag); the with-income figure is
    then None.
    """
    df = positions_dataframe(method, account=account, as_of=as_of, cutoff=cutoff)
    invested = df.attrs.get("invested_capital", portfolio_kpis.ZERO)
    rollup = portfolio_kpis.rollup(df.to_dict(orient="records") if not df.empty else [])

    income_total = None
    income_excluded: list[dict[str, Any]] = []
    if include_income:
        from app.mcp.services import income as income_service

        until = as_of if as_of is not None else (cutoff.trade_date if cutoff else None)
        income_total, income_excluded = income_service.usd_income_total(
            until=until, account=account
        )

    total_return = portfolio_kpis.total_return_pct(
        rollup["realized_pnl"], rollup["unrealized_pnl"], invested
    )
    total_with_income = (
        portfolio_kpis.total_return_pct(
            rollup["realized_pnl"], rollup["unrealized_pnl"], invested,
            portfolio_kpis.to_dec(income_total) or portfolio_kpis.ZERO,
        )
        if income_total is not None else None
    )
    f = portfolio_kpis.as_float
    null_reasons: dict[str, str] = {}
    if rollup["unrealized_pct"] is None:
        null_reasons["unrealized_pct"] = (
            "no_priced_cost_basis" if rollup["cost_basis"] else "no_cost_basis"
        )
    if total_return is None:
        null_reasons["total_return_pct"] = "no_invested_capital"
        if include_income:
            null_reasons["total_return_with_income_pct"] = "no_invested_capital"
    return {
        "market_value": f(rollup["market_value"]),
        "cost_basis": f(rollup["cost_basis"]),
        "priced_cost_basis": f(rollup["priced_cost_basis"]),
        "unpriced_cost_basis": f(rollup["unpriced_cost_basis"]),
        "unpriced_symbols": rollup["unpriced_symbols"],
        "unrealized_pnl": f(rollup["unrealized_pnl"]),
        "unrealized_pct": f(rollup["unrealized_pct"]),
        "realized_pnl": f(rollup["realized_pnl"]),
        "active_symbols": int((df["qty"] > 0).sum()) if not df.empty else 0,
        "invested_capital": f(invested),
        "total_return_pct": f(total_return),
        "total_return_with_income_pct": f(total_with_income),
        "total_return_partial": bool(rollup["unpriced_symbols"]),
        "total_return_basis": portfolio_kpis.TOTAL_RETURN_BASIS,
        "income_total": income_total,
        "income_excluded_non_usd": income_excluded,
        "null_reasons": null_reasons,
    }


def position_detail(symbol: str, method: str = "fifo") -> dict[str, Any]:
    """Per-symbol drill-down: merged numbers + per-account breakdown + open lots."""
    sym = symbol.upper()
    method = common.normalize_method(method)
    merged_row = next(
        (r for r in current_positions(method, held_only=False) if r["symbol"] == sym),
        None,
    )
    breakdown = _per_account_breakdown(sym, method)
    # Open lots are a FIFO concept: avg-cost has no lot identity to report.
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
        row["account"] = common.account_label(acct)
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
            rows = [_clean_record(dict(zip(cols, r))) for r in cur.fetchall()]
    for r in rows:
        # Free text a person typed — or a broker CSV carried in (audit M-6).
        r["notes"] = common.untrusted(r.get("notes"))
    return rows


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
