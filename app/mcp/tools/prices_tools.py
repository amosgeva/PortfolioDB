"""Price-related MCP tools (§5.3 of the plan)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastmcp import FastMCP

from app.mcp.services import prices as prices_service


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_latest_price(symbol: str) -> dict[str, Any] | None:
        """Latest snapshot for one symbol.

        Returns None if the symbol has no snapshots on file. Includes
        age_seconds so callers can decide whether the quote is fresh.
        """
        return prices_service.latest_price(symbol)

    @mcp.tool
    def get_latest_prices(
        symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Latest snapshot per symbol.

        Args:
            symbols: subset to look up, or None = every symbol with any snapshot.
        """
        return prices_service.latest_prices(symbols)

    @mcp.tool
    def get_price_history(
        symbol: str,
        since: date,
        until: date | None = None,
        resample: str = "raw",
    ) -> list[dict[str, Any]]:
        """Snapshot history for one symbol over a date window.

        Args:
            since: window start (inclusive, reporting-timezone date).
            until: window end (inclusive). None = today.
            resample: 'raw' returns every snapshot, 'daily' keeps the last
                snapshot per reporting-timezone day.
        """
        return prices_service.price_history(symbol, since, until, resample=resample)

    @mcp.tool
    def get_price_change(symbol: str, window: str = "1d") -> dict[str, Any] | None:
        """Price change over a named window.

        Args:
            window: one of '1d', '1w', '1m', '3m', '6m', 'ytd', '1y', 'all'.

        Returns: {from_ts, to_ts, from_price, to_price, change_abs, change_pct}
        or None if either endpoint is missing.
        """
        return prices_service.price_change(symbol, window)

    @mcp.tool
    def get_top_movers(
        window: str = "snapshot",
        limit: int = 5,
        direction: str = "both",
    ) -> dict[str, Any]:
        """Top gainers/losers by portfolio dollar impact.

        Args:
            window: 'snapshot' (since the second-latest snapshot — same as
                the dashboard's "Top Movers" table) or '1d' (vs yesterday's
                reporting-local EOD).
            limit: number of rows per direction.
            direction: 'both' | 'up' | 'down'.
        """
        return prices_service.top_movers(window, limit, direction)

    @mcp.tool
    def get_portfolio_value_history(
        since: date,
        until: date | None = None,
        freq: str = "snapshot",
        holdings_basis: str = "historical",
    ) -> list[dict[str, Any]]:
        """Reconstruct portfolio market value at each historical snapshot ts.

        Prices are split-adjusted. Cash is NOT included.

        Args:
            freq: 'snapshot' for every snapshot, 'daily' for the last
                snapshot per reporting-timezone day.
            holdings_basis: 'historical' (default) values each point at the
                holdings actually held then, reconstructed from the lot
                ledger. 'current_constant' is the previous behaviour —
                today's quantities held across all of history — kept for
                comparison only, since it back-projects current positions
                onto a past that did not hold them.
        """
        return prices_service.portfolio_value_history(
            since, until, freq=freq, holdings_basis=holdings_basis
        )
