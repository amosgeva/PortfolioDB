"""Returns MCP tools — multi-period returns + benchmark comparison."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from app.mcp.services import returns as returns_service


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_period_returns() -> dict[str, Any]:
        """Time-weighted portfolio return over 1D / WTD / MTD / YTD / 1Y / MAX.

        TWR reconstructs historical holdings and chains daily sub-period
        returns, so deposits and trade timing are neutralised (a contribution
        is not counted as a gain). Returns {basis, as_of, periods:{period: pct}}
        where pct is null when there isn't enough history for that window.
        """
        return returns_service.period_returns()

    @mcp.tool
    def get_benchmark_comparison(period: str = "YTD") -> dict[str, Any]:
        """Portfolio return vs a benchmark (default SPY) over one period.

        period: '1D' | 'WTD' | 'MTD' | 'YTD' | '1Y' | 'MAX'. The benchmark is a
        price return from snapshots (excludes the benchmark's dividends).
        Returns portfolio_return_pct, benchmark_return_pct, and
        relative_return_pct (portfolio − benchmark). Override the symbol via
        the PORTFOLIODB_BENCHMARK_SYMBOL env var.
        """
        return returns_service.benchmark_comparison(period)
