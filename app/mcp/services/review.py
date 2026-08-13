"""Consolidated portfolio review — every section from one cutoff.

Assembling a review used to take ~10 MCP round trips, each independently calling
``datetime.now()`` and independently re-reading "the latest price". The collector
writes every five minutes, so a review that straddled a run mixed two valuations
and the totals stopped reconciling — invisibly, because each response looked
fine on its own.

This is **composition, not new arithmetic**. Every number comes from the service
that already owned it, called once with a shared ``Cutoff``. If a figure here
disagrees with the endpoint it came from, that is a bug in this module, not a
second opinion — the reconciliation tests assert exactly that.

Sections, and where each comes from:

    summary        positions + kpis (cash, income) + fees
    returns        returns.period_returns, xirr, plus derived value change
    benchmark      returns.benchmark_comparison, incl. its alignment guard
    risk           analytics.drawdown_stats + returns.volatility + correlation
    concentration  analytics.concentration + allocation_by + stress_scenarios
    attribution    pnl.trade_quality + fees + income
    data_quality   data_quality.portfolio_data_quality

`summary` and `data_quality` are never truncated by ``detail_level``: a caller
asking for a smaller payload is asking for less detail, not for the headline
number or the warning that it cannot be trusted.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.mcp.deps import get_conn
from app.mcp.services import analytics as analytics_service
from app.mcp.services import cutoff as cutoff_service
from app.mcp.services import data_quality as dq_service
from app.mcp.services import fees as fees_service
from app.mcp.services import income as income_service
from app.mcp.services import pnl as pnl_service
from app.mcp.services import positions as positions_service
from app.mcp.services import returns as returns_service
from app.mcp.services.cutoff import Cutoff

import xirr as xirr_module

DETAIL_LEVELS = ("summary", "standard", "full")

# Allocation dimensions worth carrying in every review. 'currency' is omitted:
# every instrument is USD, so it is always a single 100% row.
REVIEW_DIMENSIONS = ("sector", "asset_class", "region", "account")

# Rows kept in the detail block before truncation kicks in.
MAX_DETAIL_ROWS = 100


def portfolio_review_snapshot(
    *,
    method: str = "fifo",
    account: str | None = None,
    as_of: datetime | date | None = None,
    cutoff: Cutoff | None = None,
    benchmark: str | None = None,
    benchmark_period: str = "YTD",
    detail_level: str = "standard",
    correlation_window: str = "3m",
    top_n: int = 10,
    reporting_currency: str = "USD",
) -> dict[str, Any]:
    """One consistent portfolio review.

    Args:
        detail_level: 'summary' drops the detail block, 'standard' adds
            positions and allocation rows, 'full' adds the correlation matrix.
        reporting_currency: accepted only as 'USD'. Rejected rather than
            silently ignored — there is no FX table, so any other value would
            produce numbers that look converted and are not.
    """
    if detail_level not in DETAIL_LEVELS:
        raise ValueError(f"detail_level must be one of {DETAIL_LEVELS}")
    if reporting_currency.upper() != cutoff_service.REPORTING_CURRENCY:
        raise ValueError(
            f"reporting_currency must be {cutoff_service.REPORTING_CURRENCY}: "
            f"no FX rates are stored, so no conversion is possible."
        )

    cutoff = cutoff or cutoff_service.resolve(as_of)

    positions = positions_service.current_positions(
        method, account=account, held_only=True, cutoff=cutoff
    )
    # Built once and shared: `risk` and the `full` detail block both need it,
    # and it is one of the more expensive reads in the payload.
    correlation = analytics_service.correlation_matrix(
        [p["symbol"] for p in positions], window=correlation_window, cutoff=cutoff
    )
    totals = positions_service.positions_summary(
        method, account=account, cutoff=cutoff
    )
    cash, cash_by_account = _cash(cutoff, account)
    income = income_service.income_summary(
        group_by="none", until=cutoff.trade_date, account=account
    )
    fees = fees_service.fees_summary(
        account=account, method=method, cutoff=cutoff
    )

    summary = _summary(totals, cash, income, fees, positions)
    review: dict[str, Any] = {
        "meta": cutoff_service.meta(
            cutoff,
            method=method,
            account=account,
            detail_level=detail_level,
        ),
        "summary": summary,
        "returns": _returns(cutoff, summary, method, account),
        "benchmark": returns_service.benchmark_comparison(
            benchmark_period, symbol=benchmark, cutoff=cutoff
        ),
        "risk": _risk(correlation, cutoff),
        "concentration": _concentration(cutoff, top_n, correlation_window, correlation),
        "attribution": _attribution(cutoff, method, account, fees, income),
        "data_quality": _data_quality(cutoff, method),
    }

    if detail_level != "summary":
        review["detail"] = _detail(positions, correlation, detail_level)
    return review


# ────────────────────────── sections ──────────────────────────


def _summary(
    totals: dict[str, Any],
    cash: float,
    income: dict[str, Any],
    fees: dict[str, Any],
    positions: list[dict[str, Any]],
) -> dict[str, Any]:
    invested = float(totals["market_value"])
    portfolio_value = invested + cash
    realized = float(totals["realized_pnl"])
    unrealized = float(totals["unrealized_pnl"])
    income_total = float(income["total_income"])

    return {
        # Reconciles by construction: portfolio_value == invested + cash.
        "portfolio_value": portfolio_value,
        "invested_market_value": invested,
        "cash": cash,
        "cash_weight_pct": (cash / portfolio_value * 100.0) if portfolio_value else None,
        "cost_basis": float(totals["cost_basis"]),
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "income_total": income_total,
        # Reported for transparency, NOT added to the total below: realized and
        # unrealized are already net of fees. See docs/methodology.md.
        "fees_total": float(fees["total_fees"]),
        "total_economic_pnl": realized + unrealized + income_total,
        "total_economic_pnl_formula": "realized + unrealized + income (all already net of fees)",
        "position_count": len(positions),
        "null_reasons": (
            {} if portfolio_value else {"cash_weight_pct": "no_portfolio_value"}
        ),
    }


def _returns(
    cutoff: Cutoff, summary: dict[str, Any], method: str, account: str | None
) -> dict[str, Any]:
    twr = returns_service.period_returns(cutoff=cutoff)
    mwr = _xirr(cutoff, summary, account)

    cost_basis = summary["cost_basis"]
    return_on_cost = (
        (summary["realized_pnl"] + summary["unrealized_pnl"] + summary["income_total"])
        / cost_basis * 100.0
        if cost_basis else None
    )

    return {
        "twr": twr["periods"],
        "twr_basis": twr["basis"],
        "observations": twr["observations"],
        "coverage": twr["coverage"],
        "xirr_investment": mwr,
        "return_on_cost_pct": return_on_cost,
        "simple_value_change": {
            "note": (
                "Change in portfolio value is not a return: it mixes market "
                "movement with contributions. Use twr for performance and "
                "xirr_investment for the money-weighted figure."
            ),
        },
        "methodology": {
            "twr": "Holdings reconstructed per snapshot day; daily sub-period returns chained, so contributions are neutralised.",
            "xirr": "Annualised rate discounting trades, income and closing value to zero. Invested capital only.",
            "return_on_cost": "(realized + unrealized + income) / cost basis. A cumulative ratio, not annualised.",
            "reference": "docs/methodology.md",
        },
        "coverage_quality": _coverage_quality(cutoff, twr),
        "null_reasons": (
            {} if cost_basis else {"return_on_cost_pct": "no_cost_basis"}
        ),
    }


def _xirr(
    cutoff: Cutoff, summary: dict[str, Any], account: str | None
) -> dict[str, Any]:
    """Money-weighted return on invested capital.

    Portfolio-level MWR is impossible here — no external-flow ledger — so this
    is deliberately scoped to the trades themselves, with the closing market
    value standing in for the open position.
    """
    lots = returns_service._fetch_lots(cutoff)
    if account is not None:
        # _fetch_lots does not filter by account; do it here rather than
        # widening a loader used by the whole returns path.
        lots = [l for l in lots if l.get("account") == account]
    dividends = returns_service._fetch_dividends(cutoff)
    return xirr_module.from_ledger(
        lots=lots,
        income=[{"pay_date": d["pay_date"], "amount": d["amount"]} for d in dividends],
        closing_value=summary["invested_market_value"],
        closing_date=cutoff.trade_date,
    )


def _coverage_quality(cutoff: Cutoff, twr: dict[str, Any]) -> dict[str, Any]:
    """Whether the return series can actually answer what was asked of it."""
    first_trade = _first_trade_date(cutoff)
    coverage_start = cutoff.coverage_start
    gap = (
        (coverage_start - first_trade).days
        if (first_trade and coverage_start and coverage_start > first_trade)
        else 0
    )
    return {
        "observations": twr["observations"],
        "coverage_start": coverage_start.isoformat() if coverage_start else None,
        "first_trade": first_trade.isoformat() if first_trade else None,
        "untracked_days_before_coverage": gap,
        "status": "partial" if gap > 0 else "complete",
        "note": (
            f"Price coverage begins {coverage_start} but trading began "
            f"{first_trade}; no return figure can span the {gap} days before "
            f"coverage. Every 'inception' figure is inception-of-coverage."
            if gap > 0 else
            "Price coverage spans the whole trading history."
        ),
    }


def _risk(corr: dict[str, Any], cutoff: Cutoff) -> dict[str, Any]:
    drawdown = analytics_service.drawdown_stats(cutoff=cutoff)
    vol = returns_service.volatility(cutoff=cutoff)
    return {
        "max_drawdown_pct": drawdown["max_drawdown_pct"],
        "current_drawdown_pct": drawdown["current_drawdown_pct"],
        "drawdown_peak_ts": drawdown.get("peak_ts"),
        "drawdown_trough_ts": drawdown.get("trough_ts"),
        "recovered": drawdown.get("recovered"),
        "holdings_basis": drawdown.get("holdings_basis"),
        "volatility": vol,
        "correlation_summary": {
            "window": corr["window"],
            "observations": corr["observations"],
            "symbols": len(corr["symbols"]),
            "most_correlated": corr["pairs"][:5],
            "diversifiers": corr["diversifiers"],
            "clusters": corr["clusters"],
        },
    }


def _concentration(
    cutoff: Cutoff, top_n: int, correlation_window: str,
    correlation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conc = analytics_service.concentration(top_n, cutoff=cutoff)
    allocations = {
        dim: analytics_service.allocation_by(dim, cutoff=cutoff)["rows"]
        for dim in REVIEW_DIMENSIONS
    }
    return {
        "largest_weight_pct": conc["single_largest_pct"],
        "top_n": conc["top_n"],
        "top_n_cumulative_pct": conc["top_n_share_pct"],
        "hhi": conc["hhi"],
        "effective_n": conc["effective_n"],
        "total_positions": conc["total_positions"],
        "top_positions": conc["rows"],
        "by_sector": allocations["sector"],
        "by_asset_class": allocations["asset_class"],
        "by_region": allocations["region"],
        "by_account": allocations["account"],
        "scenarios": analytics_service.stress_scenarios(
            cutoff=cutoff, correlation_window=correlation_window,
            correlation=correlation,
        ),
        "null_reasons": conc.get("null_reasons", {}),
    }


def _attribution(
    cutoff: Cutoff,
    method: str,
    account: str | None,
    fees: dict[str, Any],
    income: dict[str, Any],
) -> dict[str, Any]:
    quality = pnl_service.trade_quality(
        method, account=account, cutoff=cutoff
    )
    return {
        "realized": quality,
        "fees": {
            "total_fees": fees["total_fees"],
            "fee_count": fees["fee_count"],
            "fee_drag_pct": fees["fee_drag_pct"],
            "by_symbol": fees["by_symbol"][:10],
            "note": (
                "Already reflected in realized and unrealized P&L — the engines "
                "fold BUY fees into cost basis and net SELL fees out of "
                "proceeds. Do not subtract again."
            ),
        },
        "income": {
            "total_income": income["total_income"],
            "total_tax_withheld": income["total_tax_withheld"],
            "count": income["count"],
            "note": "Additive return term, never part of cost basis.",
        },
    }


def _data_quality(cutoff: Cutoff, method: str) -> dict[str, Any]:
    """Trimmed to the verdict and the issues — the full per-symbol table stays
    behind get_data_quality, but the warning always travels with the numbers."""
    dq = dq_service.portfolio_data_quality(cutoff=cutoff, method=method)
    return {
        "overall_status": dq["overall_status"],
        "overall_explanation": dq["overall_explanation"],
        "collector": {
            "status": dq["collector"]["status"],
            "message": dq["collector"]["message"],
        },
        "counts": dq["counts"],
        "material_issues": dq["material_issues"],
        "minor_issues": dq["minor_issues"],
        "detail_endpoint": "get_data_quality",
    }


def _detail(
    positions: list[dict[str, Any]],
    corr: dict[str, Any],
    detail_level: str,
) -> dict[str, Any]:
    """Bounded detail. Truncation is always declared, never silent."""
    rows = sorted(
        positions, key=lambda p: (p.get("market_value") or 0.0), reverse=True
    )
    kept = rows[:MAX_DETAIL_ROWS]

    detail: dict[str, Any] = {
        "positions": {
            "rows": kept,
            "total_rows": len(rows),
            "returned_rows": len(kept),
            "truncated": len(kept) < len(rows),
            "sort": "market_value desc",
        },
    }

    if detail_level == "full":
        detail["correlation_matrix"] = {
            "symbols": corr["symbols"],
            "matrix": corr["matrix"],
            "observations": corr["observations"],
            "window": corr["window"],
        }
    else:
        detail["correlation_matrix"] = {
            "omitted": True,
            "reason": "detail_level must be 'full' to include the matrix",
        }
    return detail


# ────────────────────────── helpers ──────────────────────────


def _cash(cutoff: Cutoff, account: str | None) -> tuple[float, list[dict[str, Any]]]:
    """Cash at the cutoff, per account. Mirrors kpis._cash_totals."""
    as_of_ts = cutoff.ts
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (account) account, cash, ts
                FROM cash_snapshots
                WHERE ts <= %s
                ORDER BY account, ts DESC
                """,
                (as_of_ts,),
            )
            rows = cur.fetchall()
    out = [
        {"account": acct or "(merged)", "cash": float(amount),
         "ts": ts.isoformat() if ts else None}
        for acct, amount, ts in rows
        if account is None or acct == account
    ]
    return sum(r["cash"] for r in out), out


def _first_trade_date(cutoff: Cutoff) -> date | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(trade_date) FROM lots WHERE trade_date <= %s",
                (cutoff.trade_date,),
            )
            row = cur.fetchone()
            return row[0] if row else None
