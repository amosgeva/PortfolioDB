"""Data-quality tools — per-symbol trust diagnostics.

`get_health` answers "is the server up". This answers "can I trust the numbers
I am about to read, and if not, which ones".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastmcp import FastMCP

from app.mcp.services import cutoff as cutoff_service
from app.mcp.services import data_quality as dq_service


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_data_quality(
        method: str = "fifo",
        as_of: datetime | None = None,
        materiality_pct: float = dq_service.DEFAULT_MATERIALITY_PCT,
        cash_max_age_days: int = dq_service.DEFAULT_CASH_MAX_AGE_DAYS,
    ) -> dict[str, Any]:
        """Per-symbol data-quality diagnostics. Read this before trusting totals.

        Checks every symbol the collector targets (held or watchlisted) for:
        missing price, stale price, missing cost basis, missing sector/country/
        asset_type, price history starting after the first trade, orphaned SELL
        lots (which the FIFO engine truncates with only a log warning, silently
        understating realized P&L), suspected duplicate lots, zero-price BUYs,
        and unrecorded split-shaped price steps. Plus collector liveness and
        manual-cash staleness at portfolio level.

        Staleness is judged against the collector's own runs, not the wall
        clock: a symbol is stale when a snapshot run succeeded but returned no
        price for it. The measured weekend gap is 64.2h, so any hour-based
        threshold either fires every Monday or cannot detect a mid-week outage.

        Args:
            method: 'fifo' or 'avg' — cost-basis engine for the weight and
                cost-basis checks.
            as_of: diagnose the data as it stood at this instant. None = now.
            materiality_pct: a position at or above this share of market value
                is material. Correctness issues (orphan_sell, missing cost
                basis, suspected split, impossible value) are material at any
                size, because a wrong number is wrong regardless of position.
            cash_max_age_days: manual cash balances older than this are stale.

        Returns:
            {meta, overall_status, overall_explanation, collector, counts,
            symbols, material_issues, minor_issues}. Statuses are
            complete | partial | stale | unavailable | inconsistent, worst-wins.
            `overall_status` is never 'complete' while a material holding has
            stale or missing data, or any holding has a correctness issue.
        """
        return dq_service.portfolio_data_quality(
            cutoff=cutoff_service.resolve(as_of) if as_of else None,
            method=method,
            materiality_pct=materiality_pct,
            cash_max_age_days=cash_max_age_days,
        )
