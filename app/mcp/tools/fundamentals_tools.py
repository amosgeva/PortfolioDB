"""Fundamentals MCP tools (§5.8 of the plan)."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from app.mcp.services import fundamentals as fundamentals_service


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_company_facts(symbol: str) -> dict[str, Any] | None:
        """Profile fields from fd_company_facts (name, sector, industry, exchange, …).

        Returns None for ETFs (Financial Datasets does not carry fundamentals
        for them — only news) and for symbols that haven't been enriched yet.
        """
        return fundamentals_service.company_facts(symbol)

    @mcp.tool
    def get_financial_metrics(symbol: str) -> dict[str, Any] | None:
        """Valuation + quality snapshot (P/E, P/S, margins, ROE, growth, …).

        Same row the Streamlit Fundamentals tab renders.
        """
        return fundamentals_service.financial_metrics(symbol)

    @mcp.tool
    def get_financial_statements(
        symbol: str,
        statement_type: str,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        """Recent financial statement rows.

        Args:
            statement_type: 'income_statement' | 'balance_sheet' | 'cash_flow_statement'.
            limit: most-recent N quarters/annuals.
        """
        return fundamentals_service.financial_statements(symbol, statement_type, limit)

    @mcp.tool
    def get_earnings_history(symbol: str, limit: int = 4) -> list[dict[str, Any]]:
        """Recent earnings beats/misses with EPS and revenue surprise."""
        return fundamentals_service.earnings_history(symbol, limit)

    @mcp.tool
    def get_filings(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        """Recent SEC filings (filing_type + filing_date + URL)."""
        return fundamentals_service.filings(symbol, limit)

    @mcp.tool
    def get_insider_trades(symbol: str, limit: int = 15) -> list[dict[str, Any]]:
        """Recent insider transactions (name, role, type, shares, value)."""
        return fundamentals_service.insider_trades(symbol, limit)

    @mcp.tool
    def get_top_holders(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        """Top institutional holders for the latest 13F report period on file."""
        return fundamentals_service.top_holders(symbol, limit)

    @mcp.tool
    def get_news(
        symbols: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """News feed merged across symbols.

        Args:
            symbols: subset or None = held + watchlist universe (matches the
                dashboard News tab default).
        """
        return fundamentals_service.news(symbols, limit)

    @mcp.tool
    def get_fd_freshness(symbol: str | None = None) -> dict[str, Any]:
        """Last-fetched timestamp + row count per FD table.

        Pass a symbol to scope the numbers to that ticker — useful before
        recommending the user re-run fd_weekly_enrichment.py for one name.
        """
        return fundamentals_service.freshness(symbol)
