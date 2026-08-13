"""Analytics MCP tools (§5.7 of the plan)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastmcp import FastMCP

from app.mcp.services import cutoff as cutoff_service
from app.mcp.services import analytics as analytics_service


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_concentration(
        top_n: int = 10, as_of: date | None = None
    ) -> dict[str, Any]:
        """Top-N weights, HHI, % held in top-N, single-name max.

        HHI is Σ wᵢ² over weights expressed as decimals. effective_n = 1/HHI
        — the number of equally-weighted positions that would yield the
        same concentration. Lower = more diversified.

        as_of: measure concentration as it stood at the end of this date —
        holdings and prices both pinned there. None = now.
        """
        return analytics_service.concentration(
            top_n, cutoff=cutoff_service.resolve(as_of) if as_of else None
        )

    @mcp.tool
    def get_sector_allocation() -> dict[str, Any]:
        """Portfolio weight by sector (from instruments.sector, enriched from
        yfinance; fd_company_facts.sector is the fallback).

        ETFs/funds carry a classified sector (e.g. 'ETF', 'Gold', 'Crypto');
        only genuinely un-enriched names roll up under 'Unknown'.
        """
        return analytics_service.sector_allocation()

    @mcp.tool
    def get_allocation(
        dimension: str = "sector", as_of: date | None = None
    ) -> dict[str, Any]:
        """Portfolio market-value weight bucketed by one dimension.

        dimension: 'sector' | 'asset_class' | 'currency' | 'region' | 'account'.
          - sector      → instruments.sector (fd_company_facts.sector fallback)
          - asset_class → instruments.asset_type (stock/ETF/Crypto/Commodity/Bond)
          - currency    → instruments.currency
          - region      → instruments.country (enriched from yfinance)
          - account     → per broker account

        Positions with no value for the dimension roll up under 'Unknown'.
        Returns {dimension, total_market_value, rows:[{key, market_value,
        weight_pct, symbols}]}.

        as_of pins holdings and prices to the end of that date. None = now.
        """
        return analytics_service.allocation_by(
            dimension, cutoff=cutoff_service.resolve(as_of) if as_of else None
        )

    @mcp.tool
    def get_correlation_matrix(
        symbols: list[str] | None = None,
        window: str = "3m",
        min_observations: int = 20,
        resample: str = "daily",
    ) -> dict[str, Any]:
        """Pairwise correlations of daily returns over a window.

        Uses price_snapshots from PortfolioDB — same data the dashboard
        uses, so numbers stay consistent with what the user sees.

        Args:
            symbols: list, or None = currently-held positions.
            window: '1m' | '3m' | '6m' | '1y' | 'all'.
            min_observations: skip pairs with fewer overlapping days.
            resample: 'daily' (default) or 'raw' (rare — usually too noisy).

        Returns: {symbols, observations, matrix, pairs (sorted by |corr|),
        diversifiers (lowest 5), clusters (>0.70)}.
        """
        return analytics_service.correlation_matrix(
            symbols, window, min_observations=min_observations, resample=resample,
        )

    @mcp.tool
    def get_drawdown_stats(
        symbol: str | None = None,
        since: date | None = None,
        holdings_basis: str = "historical",
    ) -> dict[str, Any]:
        """Max + current drawdown. Prices are split-adjusted.

        Args:
            symbol: ticker to analyze. None = whole portfolio.
            since: only consider snapshots at or after this date.
            holdings_basis: for the portfolio series only. 'historical'
                (default) values each point at the holdings actually held
                then. 'current_constant' is the previous behaviour — today's
                quantities held across all of history — kept for comparison
                only, since it back-projects current positions onto a past
                that did not hold them.

        Returns: {max_drawdown_pct, current_drawdown_pct, peak, peak_ts,
        trough, trough_ts, recovered, observations, holdings_basis}.
        """
        return analytics_service.drawdown_stats(
            symbol, since=since, holdings_basis=holdings_basis
        )

    @mcp.tool
    def get_position_weights(method: str = "fifo") -> list[dict[str, Any]]:
        """Per-symbol weight (% of market value). Tiny payload — for quick UI."""
        return analytics_service.position_weights(method)
