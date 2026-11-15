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
# Aliased: this module's own entry point is also called portfolio_kpis.
import portfolio_kpis as kpi_defs

from app.mcp.services import cash as cash_service
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

    # The dashboard's definitions, through the same module it uses
    # (app/portfolio_kpis.py via positions_summary): priced/unpriced split,
    # unrealized % over priced cost, total return over peak net invested,
    # USD-only income and cash (docs/methodology.md §12, §14).
    totals = positions_service.positions_summary(method, cutoff=cutoff, include_income=False)
    market_value = totals["market_value"]
    cost_basis = totals["cost_basis"]
    unrealized_pnl = totals["unrealized_pnl"]
    realized_pnl = totals["realized_pnl"]
    active_symbols = totals["active_symbols"]
    income_total = _income_total(cutoff)
    d = kpi_defs.to_dec
    total_return_with_income_pct = kpi_defs.as_float(kpi_defs.total_return_pct(
        d(realized_pnl), d(unrealized_pnl),
        d(totals["invested_capital"]) or kpi_defs.ZERO, d(income_total),
    ))

    cash, by_account = _cash_totals(cutoff)
    aum = market_value + cash
    watchlist_count = _watchlist_count()

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
        "null_reasons": {
            **totals["null_reasons"],
            **({} if total_return_with_income_pct is not None
               else {"total_return_with_income_pct": "no_invested_capital"}),
            **({} if prev_total_value else {"daily_change_pct": "no_prior_price"}),
        },
        "aum": aum,
        "market_value": market_value,
        "cost_basis": cost_basis,
        "priced_cost_basis": totals["priced_cost_basis"],
        "unpriced_cost_basis": totals["unpriced_cost_basis"],
        "unpriced_symbols": totals["unpriced_symbols"],
        "cash": cash,
        "cash_by_account": by_account,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pct": totals["unrealized_pct"],
        "realized_pnl": realized_pnl,
        "invested_capital": totals["invested_capital"],
        "total_return_pct": totals["total_return_pct"],
        "total_return_partial": totals["total_return_partial"],
        "total_return_basis": totals["total_return_basis"],
        "income_total": income_total,
        "total_return_with_income_pct": total_return_with_income_pct,
        "excluded_non_usd": _excluded_non_usd(cutoff, by_account),
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
        "priced_cost_basis": 0.0,
        "unpriced_cost_basis": 0.0,
        "unpriced_symbols": [],
        "invested_capital": 0.0,
        "total_return_partial": False,
        "excluded_non_usd": _excluded_non_usd(cutoff, by_account),
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
    """USD income (dividends/interest/distributions) received up to the cutoff.

    A separate, additive return term — never part of cost basis. Income in
    another currency is excluded (see _excluded_non_usd).
    """
    total, _excluded = income_service.usd_income_total(
        until=cutoff.trade_date if cutoff else None, account=None
    )
    return total


def _cash_totals(cutoff: Cutoff | None = None) -> tuple[float, list[dict[str, Any]]]:
    """USD cash at the cutoff, and every account's latest balance (services/cash.py)."""
    info = cash_service.latest_cash(cutoff.ts if cutoff else None)
    return info["total"], info["by_account"]


def _excluded_non_usd(
    cutoff: Cutoff | None, by_account: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """What the dollar totals leave out: non-USD income and cash (methodology §14)."""
    _total, income_excluded = income_service.usd_income_total(
        until=cutoff.trade_date if cutoff else None, account=None
    )
    other_cash = [
        {"account": r.get("account"), "currency": r.get("currency"), "cash": r.get("cash")}
        for r in by_account if r.get("included") is False
    ]
    return list(income_excluded) + kpi_defs.currency_warnings(cash=other_cash)


def _watchlist_count() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM instruments WHERE watchlist = TRUE")
            return int(cur.fetchone()[0])
