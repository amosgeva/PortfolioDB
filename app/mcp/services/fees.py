"""Fees service — aggregate trading fees paid across the lot ledger.

Fees are already folded into cost basis (BUY) and proceeds (SELL) by the
FIFO/avg engines (see ``app/fifo.py``), so this module changes no existing
metric — it is pure reporting on top of the raw ``lots.fees`` column.

``fee_drag_pct`` expresses total fees as a percentage of the *current* FIFO
cost basis (the same denominator the KPI tiles use), i.e. how much of the
money you hold at cost has been spent on fees.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from psycopg2 import sql

from app.mcp.deps import get_conn
from app.mcp.services import positions as positions_service
from app.mcp.services.cutoff import Cutoff


def fees_summary(
    *,
    since: date | None = None,
    until: date | None = None,
    account: str | None = None,
    method: str = "fifo",
    cutoff: Cutoff | None = None,
) -> dict[str, Any]:
    """Total fees paid + per-symbol / per-account breakdown + fee drag.

    Args:
        since/until: filter lots by ``trade_date`` (inclusive). None = unbounded.
        account: restrict to one broker account. None = all accounts.
        method: cost-basis engine for the fee-drag denominator ('fifo'/'avg').
    """
    # A cutoff bounds the fee window too, so the numerator and the cost-basis
    # denominator below describe the same point in time.
    effective_until = until if until is not None else (cutoff.trade_date if cutoff else None)
    rows = _fetch_fee_rows(since=since, until=effective_until, account=account)

    total = 0.0
    count = 0
    by_symbol: dict[str, dict[str, Any]] = defaultdict(lambda: {"fees": 0.0, "count": 0})
    by_account: dict[str, dict[str, Any]] = defaultdict(lambda: {"fees": 0.0, "count": 0})
    for sym, acct, fee in rows:
        fee = float(fee)
        total += fee
        paid = fee > 0
        if paid:
            count += 1
        bs = by_symbol[sym]
        bs["fees"] += fee
        bs["count"] += 1 if paid else 0
        key = acct or "(none)"
        ba = by_account[key]
        ba["fees"] += fee
        ba["count"] += 1 if paid else 0

    cost_basis = float(
        positions_service.positions_summary(method, account=account, cutoff=cutoff)["cost_basis"]
    )
    fee_drag_pct = (total / cost_basis * 100.0) if cost_basis else 0.0

    return {
        "since": since.isoformat() if since else None,
        "until": effective_until.isoformat() if effective_until else None,
        "account": account,
        "total_fees": total,
        "fee_count": count,
        "cost_basis": cost_basis,
        "fee_drag_pct": fee_drag_pct,
        "by_symbol": [
            {"symbol": s, "fees": v["fees"], "count": v["count"]}
            for s, v in sorted(
                by_symbol.items(), key=lambda kv: kv[1]["fees"], reverse=True
            )
        ],
        "by_account": [
            {"account": a, "fees": v["fees"], "count": v["count"]}
            for a, v in sorted(
                by_account.items(), key=lambda kv: kv[1]["fees"], reverse=True
            )
        ],
    }


# ────────────────────────── helpers ──────────────────────────


def _fetch_fee_rows(
    *, since: date | None, until: date | None, account: str | None
) -> list[tuple[str, str | None, Any]]:
    """Pull (symbol, account, fees) for matching lots.

    Composed from psycopg2.sql fragments; all user values stay bound via %s.
    """
    query = sql.SQL("SELECT symbol, account, fees FROM lots WHERE 1=1")
    params: list[Any] = []
    if since is not None:
        query += sql.SQL(" AND trade_date >= %s")
        params.append(since)
    if until is not None:
        query += sql.SQL(" AND trade_date <= %s")
        params.append(until)
    if account is not None:
        query += sql.SQL(" AND account = %s")
        params.append(account)
    query += sql.SQL(" ORDER BY symbol, COALESCE(account,'')")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return [(r[0], r[1], r[2]) for r in cur.fetchall()]
