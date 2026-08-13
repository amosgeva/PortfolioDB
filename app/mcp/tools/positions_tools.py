"""Position-related MCP tools (§5.2 of the plan)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastmcp import FastMCP

from app.mcp.services import cutoff as cutoff_service
from app.mcp.services import positions as positions_service


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_positions(
        method: str = "fifo",
        account: str | None = None,
        held_only: bool = True,
        as_of: date | None = None,
    ) -> list[dict[str, Any]]:
        """Portfolio positions, with cost basis, market value, and weight.

        Args:
            method: 'fifo' (default) or 'avg' — which cost-basis engine to use.
            account: filter to one broker account (None = all accounts merged).
            held_only: when true, exclude rows with qty == 0.
            as_of: value the portfolio as it stood at the end of this date —
                lots with trade_date <= it, priced at the last snapshot on or
                before it. None = now.

        Returns:
            One row per symbol: {symbol, qty, open_cost, avg_cost, last_price,
            last_price_ts, market_value, unrealized_pnl, realized_pnl,
            weight_pct}. NaN/missing prices come back as null.
        """
        # as_of pins the price as well as the lots. Filtering holdings to a past
        # date while valuing them at today's price yields a portfolio that never
        # existed, and market_value / unrealized_pnl would be meaningless.
        return positions_service.current_positions(
            method,
            account=account,
            held_only=held_only,
            cutoff=cutoff_service.resolve(as_of) if as_of else None,
        )

    @mcp.tool
    def get_position_detail(
        symbol: str, method: str = "fifo"
    ) -> dict[str, Any]:
        """Drill-down for a single symbol.

        Returns: merged row, per-account breakdown, current open BUY lots
        (FIFO only — empty for avg-cost), and the last 20 trades for that
        symbol across all accounts.
        """
        return positions_service.position_detail(symbol, method)

    @mcp.tool
    def get_open_lots(
        symbol: str | None = None,
        account: str | None = None,
    ) -> list[dict[str, Any]]:
        """Open BUY lots remaining after FIFO matching.

        Useful for picking tax lots before a SELL. Each row reports the
        original BUY's trade_date, the qty still open, and per-share cost.
        """
        return positions_service.open_lots(symbol=symbol, account=account)
