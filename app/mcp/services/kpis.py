"""KPI service — replicates the inline math in streamlit_app.py:507-579.

These are the numbers on the dashboard's three KPI rows. Reproduced here
verbatim so an MCP client gets exactly the same totals the user sees in
Streamlit. The parity test in app/mcp/tests/test_kpi_parity.py guards
against drift.

Formulas (one-line summary each — see streamlit_app.py for the original):
  market_value      = Σ (qty × last_price) for positions with a snapshot
  cost_basis        = Σ open_cost across all FIFO-merged positions
  unrealized_pnl    = market_value − cost_basis  (NaN rows skipped via .sum(skipna=True))
  unrealized_pct    = unrealized_pnl / cost_basis × 100
  realized_pnl      = Σ realized_pnl across all FIFO-merged positions
  cash              = Σ latest cash per account
  aum               = market_value + cash
  total_return_pct  = (realized_pnl + unrealized_pnl) / cost_basis × 100
  daily_change_usd  = Σ qty × (last_price − prev_eod_price)  — symbols with prev_eod
  daily_change_pct  = daily_change_usd / Σ (qty × prev_eod_price) × 100
  Δ last snapshot $ = Σ qty × (last_price − second_latest_price)
  active_symbols    = count of positions with qty > 0 (in merged view)
  watchlist_count   = SELECT COUNT(*) FROM instruments WHERE watchlist=TRUE
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.mcp.deps import get_conn
from app.mcp.services import cutoff as cutoff_service
from app.mcp.services import income as income_service
from app.mcp.services import positions as positions_service
from app.mcp.services import prices as prices_service
from app.mcp.services.common import is_nan
from app.mcp.services.cutoff import Cutoff


def portfolio_kpis(
    method: str = "fifo",
    *,
    as_of: datetime | date | None = None,
    cutoff: Cutoff | None = None,
) -> dict[str, Any]:
    """Return every KPI tile shown on the dashboard, all from one instant.

    This payload reads three separate price maps — the cutoff price, the
    previous snapshot, and the previous day's close. Unpinned, each is fetched
    independently, so a snapshot landing mid-request leaves market_value on the
    new prices and daily_change on the old ones. The cutoff pins all three.

    Pass ``cutoff`` when composing with other services so every number shares
    one instant; pass ``as_of`` for a one-off historical read; pass neither for
    the previous live behaviour.
    """
    cutoff = cutoff or cutoff_service.resolve(as_of)
    df = positions_service.positions_dataframe(method, cutoff=cutoff)

    if df.empty:
        return _empty_kpis(method, cutoff)

    # Replicate streamlit_app.py:524-536 exactly.
    market_value = float(df["market_value"].sum(skipna=True))
    cost_basis = float(df["open_cost"].sum())
    unrealized_pnl = float(df["unrealized_pnl"].sum(skipna=True))
    unrealized_pct = _ratio_pct(unrealized_pnl, cost_basis)
    realized_pnl = float(df["realized_pnl"].sum()) if "realized_pnl" in df.columns else 0.0
    active_symbols = int((df["qty"] > 0).sum())

    cash, by_account = _cash_totals(cutoff)
    aum = market_value + cash
    watchlist_count = _watchlist_count()
    total_return_pct = _ratio_pct(realized_pnl + unrealized_pnl, cost_basis)
    # Income (dividends/interest) is a SEPARATE, additive return term — the
    # existing total_return_pct above is intentionally left unchanged.
    income_total = _income_total(cutoff)
    total_return_with_income_pct = _ratio_pct(
        realized_pnl + unrealized_pnl + income_total, cost_basis
    )

    # Daily change vs the prior day's EOD (reporting timezone) — only counts symbols
    # that have a snapshot from before the cutoff's day. Both comparison maps
    # are pinned to the same cutoff as market_value above, so the deltas
    # describe one instant rather than straddling a snapshot.
    prev_map = prices_service.prev_day_eod_price_map(as_of_ts=cutoff.ts)
    second_map = prices_service.second_latest_price_map(as_of_ts=cutoff.ts)
    daily_change_usd = 0.0
    delta_last_snapshot_usd = 0.0
    prev_total_value = 0.0
    for _, row in df.iterrows():
        sym = row["symbol"]
        qty = float(row["qty"])
        last = 0.0 if is_nan(row["last_price"]) else float(row["last_price"])
        prev = prev_map.get(sym)
        if prev is not None:
            daily_change_usd += qty * (last - prev)
            prev_total_value += qty * prev
        second = second_map.get(sym)
        if second is not None:
            delta_last_snapshot_usd += qty * (last - second)
    daily_change_pct = _ratio_pct(daily_change_usd, prev_total_value)

    return {
        "method": method,
        # Kept at the top level for backward compatibility; `meta.as_of` is the
        # same instant and is what a composing caller should read.
        "as_of": cutoff.ts.isoformat(),
        "meta": cutoff_service.meta(cutoff, method=method),
        "null_reasons": _null_reasons(
            cost_basis=cost_basis, prev_total_value=prev_total_value
        ),
        "aum": aum,
        "market_value": market_value,
        "cost_basis": cost_basis,
        "cash": cash,
        "cash_by_account": by_account,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pct": unrealized_pct,
        "realized_pnl": realized_pnl,
        "total_return_pct": total_return_pct,
        "income_total": income_total,
        "total_return_with_income_pct": total_return_with_income_pct,
        "daily_change_usd": daily_change_usd,
        "daily_change_pct": daily_change_pct,
        "delta_last_snapshot_usd": delta_last_snapshot_usd,
        "active_symbols": active_symbols,
        "watchlist_count": watchlist_count,
    }


# ────────────────────────── helpers ──────────────────────────


def _ratio_pct(numerator: float, denominator: float) -> float | None:
    """numerator/denominator as a percentage, or None when it is undefined.

    A zero denominator means the question has no answer — there is no cost
    basis to return *on*, or no prior valuation to move *from*. Returning 0.0
    there is a lie an agent cannot detect: it is indistinguishable from a
    genuinely flat period. See null_reasons on the payload for which case fired.
    """
    if not denominator:
        return None
    return numerator / denominator * 100.0


def _null_reasons(*, cost_basis: float, prev_total_value: float) -> dict[str, str]:
    """Why any null percentage in this payload is null. Empty when all defined."""
    reasons: dict[str, str] = {}
    if not cost_basis:
        for field in ("unrealized_pct", "total_return_pct", "total_return_with_income_pct"):
            reasons[field] = "no_cost_basis"
    if not prev_total_value:
        reasons["daily_change_pct"] = "no_prior_price"
    return reasons


def _empty_kpis(method: str, cutoff: Cutoff) -> dict[str, Any]:
    """Payload when no positions exist.

    Absolute totals are genuinely 0.0 — the portfolio really does hold nothing.
    The percentages are None: with no cost basis there is no return to report,
    and claiming 0% would be indistinguishable from a flat year.
    """
    cash, by_account = _cash_totals(cutoff)
    return {
        "method": method,
        "as_of": cutoff.ts.isoformat(),
        "meta": cutoff_service.meta(cutoff, method=method),
        "null_reasons": _null_reasons(cost_basis=0.0, prev_total_value=0.0),
        "aum": cash,
        "market_value": 0.0,
        "cost_basis": 0.0,
        "cash": cash,
        "cash_by_account": by_account,
        "unrealized_pnl": 0.0,
        "unrealized_pct": None,
        "realized_pnl": 0.0,
        "total_return_pct": None,
        "income_total": _income_total(cutoff),
        "total_return_with_income_pct": None,
        "daily_change_usd": 0.0,
        "daily_change_pct": None,
        "delta_last_snapshot_usd": 0.0,
        "active_symbols": 0,
        "watchlist_count": _watchlist_count(),
    }


def _income_total(cutoff: Cutoff | None = None) -> float:
    """Income (dividends/interest/distributions) received up to the cutoff.

    A separate, additive return term — never part of cost basis.
    """
    return float(
        income_service.income_summary(
            group_by="none",
            until=cutoff.trade_date if cutoff else None,
        )["total_income"]
    )


def _cash_totals(cutoff: Cutoff | None = None) -> tuple[float, list[dict[str, Any]]]:
    """Σ cash per account as at the cutoff — mirrors streamlit_app.py:539-551."""
    as_of_ts = cutoff.ts if cutoff else None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                # Latest row per account — a global ORDER BY ts LIMIT N would
                # drop accounts whose newest snapshot falls outside the N rows.
                """
                SELECT DISTINCT ON (account) account, cash, ts
                FROM cash_snapshots
                WHERE (%s::timestamptz IS NULL OR ts <= %s)
                ORDER BY account, ts DESC
                """,
                (as_of_ts, as_of_ts),
            )
            rows = cur.fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for acct, cash, ts in rows:
        key = acct or "(merged)"
        if key not in latest:
            latest[key] = {"account": key, "cash": float(cash), "ts": ts.isoformat() if ts else None}
    total = sum(r["cash"] for r in latest.values())
    return total, list(latest.values())


def _watchlist_count() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM instruments WHERE watchlist = TRUE")
            return int(cur.fetchone()[0])
