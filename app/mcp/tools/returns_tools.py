"""Returns MCP tools — multi-period returns + benchmark comparison."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from app.mcp.services import returns as returns_service

Account = Annotated[
    str | None,
    Field(description="One broker account; '(none)' selects lots recorded without one. None = all accounts."),
]


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_period_returns(account: Account = None) -> dict[str, Any]:
        """Time-weighted portfolio return over 1D / WTD / MTD / YTD / 1Y / MAX.

        TWR reconstructs historical holdings and chains daily sub-period
        returns, so a contribution is not counted as a gain nor a sale as a
        loss. Returns {basis, as_of, periods:{period: pct}} where pct is null
        when there isn't enough history for that window, or when no capital
        was at work inside it (everything sold, nothing bought since).

        account: one account's return, from its own trades and income.
        """
        return returns_service.period_returns(account=account)

    @mcp.tool
    def get_benchmark_comparison(
        period: str = "YTD",
        account: Account = None,
        min_observations: Annotated[int | None, Field(ge=2, le=10_000)] = None,
        min_alignment_pct: Annotated[float, Field(ge=0, le=100)] = returns_service.MIN_ALIGNMENT_PCT,
    ) -> dict[str, Any]:
        """Portfolio return vs a benchmark (default SPY) over one period.

        period: '1D' | 'WTD' | 'MTD' | 'YTD' | '1Y' | 'MAX'. The benchmark is a
        price return from snapshots (excludes the benchmark's dividends).
        Returns portfolio_return_pct, benchmark_return_pct, and
        relative_return_pct (portfolio − benchmark). Override the symbol via
        the PORTFOLIODB_BENCHMARK_SYMBOL env var.

        The comparison is refused (status 'insufficient_alignment', null
        returns, an explanation) when the two series share too few observed
        days. The default floor is 2 observations for 1D and WTD — a return
        needs a base day and an end day — and 10 for the longer periods, with
        at least 80% of the portfolio's observed days also observed for the
        benchmark. min_observations / min_alignment_pct lower or raise those
        deliberately.
        """
        return returns_service.benchmark_comparison(
            period,
            account=account,
            min_observations=min_observations,
            min_alignment_pct=min_alignment_pct,
        )
