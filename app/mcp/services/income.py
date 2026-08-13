"""Income service — dividends / interest / cap-gain distributions.

Income lives in its own append-only table (never in ``lots``), so it does not
touch cost basis or the FIFO engine. It contributes to return as a *separate*
additive term: see ``kpis.py`` for ``income_total`` and
``total_return_with_income_pct`` (the existing ``total_return_pct`` is left
unchanged).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from psycopg2 import sql

from app.mcp.deps import get_conn
from app.mcp.services import positions as positions_service
from app.mcp.services.cutoff import Cutoff

_GROUP_BY = ("none", "symbol", "account", "month", "kind")


def income_summary(
    *,
    since: date | None = None,
    until: date | None = None,
    group_by: str = "symbol",
    account: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """Total income received in [since, until], optionally grouped.

    Args:
        group_by: 'none' | 'symbol' | 'account' | 'month' | 'kind'.
        kind: filter to one of DIVIDEND / INTEREST / CAP_GAIN_DIST.
    """
    if group_by not in _GROUP_BY:
        raise ValueError(f"group_by must be one of {_GROUP_BY}")

    rows = _fetch_income(since=since, until=until, account=account, kind=kind)
    total = sum(r["amount"] for r in rows)
    total_tax = sum(r["tax_withheld"] for r in rows)

    base = {
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "total_income": float(total),
        "total_tax_withheld": float(total_tax),
        "count": len(rows),
    }
    if group_by == "none":
        return base

    buckets: dict[Any, dict[str, Any]] = defaultdict(
        lambda: {"income": 0.0, "tax_withheld": 0.0, "count": 0}
    )
    for r in rows:
        key = _bucket_key(r, group_by)
        b = buckets[key]
        b["income"] += r["amount"]
        b["tax_withheld"] += r["tax_withheld"]
        b["count"] += 1

    out_rows = [
        {
            "bucket": k,
            "income": float(v["income"]),
            "tax_withheld": float(v["tax_withheld"]),
            "count": v["count"],
        }
        for k, v in sorted(buckets.items(), key=lambda kv: kv[1]["income"], reverse=True)
    ]
    return {**base, "group_by": group_by, "rows": out_rows}


def yield_on_cost(
    *,
    account: str | None = None,
    method: str = "fifo",
    today: date | None = None,
    cutoff: Cutoff | None = None,
) -> dict[str, Any]:
    """Trailing-12-month income as a percentage of cost basis at the cutoff.

    The trailing window ends at the cutoff too, so the income numerator and the
    cost-basis denominator describe the same point in time.
    """
    today = today or (cutoff.trade_date if cutoff else date.today())
    since = today - timedelta(days=365)
    rows = _fetch_income(since=since, until=today, account=account, kind=None)
    ttm = float(sum(r["amount"] for r in rows))
    cost_basis = float(
        positions_service.positions_summary(method, account=account, cutoff=cutoff)["cost_basis"]
    )
    return {
        "since": since.isoformat(),
        "until": today.isoformat(),
        "ttm_income": ttm,
        "cost_basis": cost_basis,
        "yield_on_cost_pct": (ttm / cost_basis * 100.0) if cost_basis else 0.0,
    }


# ────────────────────────── helpers ──────────────────────────


def _bucket_key(r: dict[str, Any], group_by: str) -> Any:
    if group_by == "symbol":
        return r["symbol"]
    if group_by == "account":
        return r["account"] or "(none)"
    if group_by == "kind":
        return r["kind"]
    if group_by == "month":
        pd = r["pay_date"]
        return pd.strftime("%Y-%m") if pd else "unknown"
    return None


def _fetch_income(
    *,
    since: date | None,
    until: date | None,
    account: str | None,
    kind: str | None,
) -> list[dict[str, Any]]:
    """Pull income rows; all user values bound via %s through sql composition."""
    query = sql.SQL(
        "SELECT symbol, account, kind, pay_date, amount, tax_withheld "
        "FROM income WHERE 1=1"
    )
    params: list[Any] = []
    if since is not None:
        query += sql.SQL(" AND pay_date >= %s")
        params.append(since)
    if until is not None:
        query += sql.SQL(" AND pay_date <= %s")
        params.append(until)
    if account is not None:
        query += sql.SQL(" AND account = %s")
        params.append(account)
    if kind is not None:
        query += sql.SQL(" AND kind = %s")
        params.append(kind)
    query += sql.SQL(" ORDER BY pay_date")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [d[0] for d in cur.description]
            out = []
            for row in cur.fetchall():
                rec = dict(zip(cols, row))
                rec["amount"] = float(rec["amount"]) if rec["amount"] is not None else 0.0
                rec["tax_withheld"] = (
                    float(rec["tax_withheld"]) if rec["tax_withheld"] is not None else 0.0
                )
                out.append(rec)
            return out
