"""Activity MCP tools (§5.6 of the plan)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from pydantic import Field

from fastmcp import FastMCP

from app.mcp.services import activity as activity_service


Limit = Annotated[int, Field(ge=1, le=activity_service.MAX_ROWS)]
Account = Annotated[
    str | None,
    Field(description="One broker account; '(none)' selects lots recorded without one. None = all accounts."),
]


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_lots(
        symbol: str | None = None,
        account: Account = None,
        side: str | None = None,
        since: date | None = None,
        until: date | None = None,
        limit: Limit = 200,
    ) -> list[dict[str, Any]]:
        """Raw lot rows with any combination of filters.

        Use this when you need the underlying ledger entries (e.g. to audit
        a cost-basis number or trace a position back to its origins).
        """
        return activity_service.lots(
            symbol=symbol, account=account, side=side,
            since=since, until=until, limit=limit,
        )

    @mcp.tool
    def get_recent_trades(
        limit: Limit = 20, since: date | None = None
    ) -> list[dict[str, Any]]:
        """Most recent BUY/SELL trades across all symbols and accounts."""
        return activity_service.recent_trades(limit=limit, since=since)

    @mcp.tool
    def get_trading_activity(
        group_by: str = "month",
        since: date | None = None,
        until: date | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate trade counts and notional value.

        Args:
            group_by: 'month' | 'symbol' | 'account'.
            since/until: optional trade_date window.
        """
        return activity_service.trading_activity(group_by, since=since, until=until)

    @mcp.tool
    def get_cash_balance(account: str | None = None) -> dict[str, Any]:
        """Current cash. None = merged total across all accounts.

        Totals are USD only. A balance entered in another currency is listed
        with its currency and `included: false`, and described under
        `excluded_non_usd`; it is never added to the dollar total.
        """
        return activity_service.cash_balance(account)

    @mcp.tool
    def get_cash_history(
        account: Account = None,
        since: date | None = None,
        until: date | None = None,
        limit: Limit = 200,
    ) -> list[dict[str, Any]]:
        """Full cash adjustment history, optionally filtered.

        since/until are inclusive reporting-timezone dates. `note` is free
        text, returned as {"untrusted_text": ...}.
        """
        return activity_service.cash_history(
            account, since=since, until=until, limit=limit,
        )
