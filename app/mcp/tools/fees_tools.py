"""Fees MCP tool — aggregate trading fees paid."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastmcp import FastMCP

from app.mcp.services import fees as fees_service


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_fees_summary(
        since: date | None = None,
        until: date | None = None,
        account: str | None = None,
        method: str = "fifo",
    ) -> dict[str, Any]:
        """Total trading fees paid, with per-symbol / per-account breakdown.

        Fees are already folded into cost basis (BUY) and proceeds (SELL), so
        this is a reporting view — it changes no P&L number. ``fee_drag_pct``
        is total fees as a percentage of current cost basis.

        Args:
            since/until: filter lots by trade date (inclusive).
            account: restrict to one broker account.
            method: cost-basis engine for the fee-drag denominator ('fifo'/'avg').
        """
        return fees_service.fees_summary(
            since=since, until=until, account=account, method=method
        )
