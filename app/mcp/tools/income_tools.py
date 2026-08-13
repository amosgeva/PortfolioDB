"""Income MCP tools — dividends / interest summaries and yield-on-cost."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastmcp import FastMCP

from app.mcp.services import income as income_service


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_income(
        since: date | None = None,
        until: date | None = None,
        group_by: str = "symbol",
        account: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Income received (dividends / interest / cap-gain distributions).

        Income is tracked separately from trades and never affects cost basis.

        Args:
            group_by: 'none' | 'symbol' | 'account' | 'month' | 'kind'.
            kind: filter to 'DIVIDEND' | 'INTEREST' | 'CAP_GAIN_DIST'.
        """
        return income_service.income_summary(
            since=since, until=until, group_by=group_by, account=account, kind=kind
        )

    @mcp.tool
    def get_dividend_summary(account: str | None = None) -> dict[str, Any]:
        """Dividend totals by symbol plus trailing-12-month yield on cost.

        Combines income_summary(kind='DIVIDEND', group_by='symbol') with
        yield_on_cost (TTM dividends ÷ current cost basis).
        """
        return {
            "by_symbol": income_service.income_summary(
                group_by="symbol", account=account, kind="DIVIDEND"
            ),
            "yield_on_cost": income_service.yield_on_cost(account=account),
        }
