"""Income service — dividends / interest / cap-gain distributions.

Income lives in its own append-only table (never in ``lots``), so it does not
touch cost basis or the FIFO engine. It contributes to return as a *separate*
additive term: see ``kpis.py`` for ``income_total`` and
``total_return_with_income_pct``.

USD only (audit Q-8, docs/methodology.md §12/§14): the ledger reports in USD
and stores no FX rates, so an income row recorded in another currency is left
out of every total here and listed under ``excluded_non_usd`` instead of being
added at face value as dollars — the same rule as app/portfolio_kpis.py.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from psycopg2 import sql

import portfolio_kpis

from app.mcp.deps import get_conn
from app.mcp.services import common
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

    rows, other = _usd_and_other(
        _fetch_income(since=since, until=until, account=account, kind=kind)
    )
    total = sum(r["amount"] for r in rows)
    total_tax = sum(r["tax_withheld"] for r in rows)

    base = {
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "total_income": float(total),
        "total_tax_withheld": float(total_tax),
        "count": len(rows),
        "currency": portfolio_kpis.BASE_CURRENCY,
        "excluded_non_usd": portfolio_kpis.currency_warnings(income=other),
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
    rows, other = _usd_and_other(
        _fetch_income(since=since, until=today, account=account, kind=None)
    )
    ttm = float(sum(r["amount"] for r in rows))
    cost_basis = float(
        positions_service.positions_summary(
            method, account=account, cutoff=cutoff, include_income=False
        )["cost_basis"]
    )
    return {
        "since": since.isoformat(),
        "until": today.isoformat(),
        "ttm_income": ttm,
        "cost_basis": cost_basis,
        "yield_on_cost_pct": (ttm / cost_basis * 100.0) if cost_basis else 0.0,
        "excluded_non_usd": portfolio_kpis.currency_warnings(income=other),
    }


def _usd_and_other(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(USD rows, rows in another currency) — portfolio_kpis' split."""
    return portfolio_kpis.split_by_currency(rows)


def usd_income_total(*, until: date | None, account: str | None) -> tuple[float, list[dict[str, Any]]]:
    """USD income received by ``until`` for ``account`` (None = all), and the
    warnings for what was left out. The income term of total return."""
    rows, other = _usd_and_other(
        _fetch_income(since=None, until=until, account=account, kind=None)
    )
    return float(sum(r["amount"] for r in rows)), portfolio_kpis.currency_warnings(income=other)


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
        "SELECT symbol, account, kind, pay_date, amount, tax_withheld, currency "
        "FROM income WHERE 1=1"
    )
    params: list[Any] = []
    if since is not None:
        query += sql.SQL(" AND pay_date >= %s")
        params.append(since)
    if until is not None:
        query += sql.SQL(" AND pay_date <= %s")
        params.append(until)
    query, params = common.add_account_filter(query, params, account)
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
                rec["amount"] = float(rec["amount"]) if rec.get("amount") is not None else 0.0
                rec["tax_withheld"] = (
                    float(rec["tax_withheld"]) if rec.get("tax_withheld") is not None else 0.0
                )
                out.append(rec)
            return out
