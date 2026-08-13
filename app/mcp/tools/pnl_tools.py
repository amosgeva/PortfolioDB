"""P&L MCP tools (§5.4 of the plan)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastmcp import FastMCP

from app.mcp.services import cutoff as cutoff_service
from app.mcp.services import pnl as pnl_service


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_realized_pnl(
        method: str = "fifo",
        since: date | None = None,
        until: date | None = None,
        group_by: str = "symbol",
        account: str | None = None,
    ) -> dict[str, Any]:
        """Realized P&L from SELL lots in [since, until], optionally grouped.

        Note: the BUY side of a match is never filtered by date — a SELL in
        2026 is allowed to consume BUYs from 2022. Only the SELL date is
        compared against the window. This matches how tax reporting works.

        Args:
            method: 'fifo' or 'avg'.
            group_by: 'none' | 'symbol' | 'account' | 'month'.
            account: filter to one broker account.
        """
        return pnl_service.realized_pnl(
            method, since=since, until=until, group_by=group_by, account=account,
        )

    @mcp.tool
    def get_unrealized_pnl(
        method: str = "fifo",
        account: str | None = None,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Per-symbol unrealized P&L plus totals.

        Args:
            as_of: value the portfolio as it stood at the end of this date —
                lots with trade_date <= it, priced at the last snapshot on or
                before it. None = now.
        """
        # Prices are pinned alongside the lots — see get_positions.
        return pnl_service.unrealized_pnl(
            method,
            account=account,
            cutoff=cutoff_service.resolve(as_of) if as_of else None,
        )

    @mcp.tool
    def get_pnl_by_symbol(method: str = "fifo") -> list[dict[str, Any]]:
        """Combined realized + unrealized P&L per symbol, sorted by total."""
        return pnl_service.pnl_by_symbol(method)

    @mcp.tool
    def get_pnl_summary(method: str = "fifo") -> dict[str, Any]:
        """Totals row: market value, cost basis, realized, unrealized, return %."""
        return pnl_service.pnl_summary(method)

    @mcp.tool
    def compare_methods(
        since: date | None = None,
        until: date | None = None,
    ) -> dict[str, Any]:
        """Side-by-side FIFO vs avg-cost realized P&L, per symbol and total."""
        return pnl_service.compare_methods(since=since, until=until)

    @mcp.tool
    def get_trade_quality(
        method: str = "fifo",
        since: date | None = None,
        until: date | None = None,
        account: str | None = None,
        group_by: str = "none",
    ) -> dict[str, Any]:
        """Realized-trade quality after costs — win rate, payoff, profit factor.

        A *trade* is one closing SELL lot, not a parcel: FIFO can split one
        SELL across several BUY lots, and those parcels can land on opposite
        sides of breakeven, so counting them separately would report a win rate
        for events that were never separate decisions. Parcels appear as
        `match_count`, and holding periods are bucketed per parcel because that
        is the level at which a holding period exists.

        IMPORTANT — fees are NOT an additional cost to subtract. `realized_pnl`
        from every endpoint in this server is already net of fees: the engines
        fold BUY fees into cost basis and net SELL fees out of proceeds. The
        `fees` field here decomposes that same number, so
        `gross_realized_pnl - fees == net_realized_pnl`. Adding
        get_fees_summary's total to a realized figure double-counts.

        Args:
            group_by: 'none' | 'symbol' | 'account' | 'month' | 'holding_bucket'.
            since/until: filter by SELL date. Only the sell side is filtered —
                the BUY it matches may be far older, as in a tax report.

        Returns gross/net realized P&L, fees, trade and parcel counts, win rate,
        average gain/loss, payoff ratio, profit factor, buy/sell/traded notional,
        fee-to-notional and fee-to-gross-profit ratios, holding-period buckets,
        and null_reasons for any undefined metric.

        Holding buckets are unavailable under method='avg', which pools every
        purchase and so has no per-parcel buy date.
        """
        return pnl_service.trade_quality(
            method,
            since=since,
            until=until,
            account=account,
            group_by=group_by,
        )
