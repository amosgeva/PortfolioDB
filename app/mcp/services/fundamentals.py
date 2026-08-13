"""Fundamentals service — thin wrappers around fd_store read helpers.

All FD enrichment lives in the fd_* tables, populated by
fd_weekly_enrichment.py. We never call the Financial Datasets API from MCP
tools — that would conflate ingestion with serving. If a symbol has no
fundamentals on file, the tool returns null/empty and the caller can
re-run the enrichment job.

ETFs (fd_store.ETF_SYMBOLS) have no fundamentals, only news. The tools
surface that distinction so an agent doesn't waste cycles asking for P/E
on GLD.
"""

from __future__ import annotations

from typing import Any

import fd_store
from psycopg2 import sql

from app.mcp.deps import get_conn
from app.mcp.services import common


def is_etf(symbol: str) -> bool:
    return symbol.upper() in fd_store.ETF_SYMBOLS


# ────────────────────────── single-symbol reads ──────────────────────────


def company_facts(symbol: str) -> dict[str, Any] | None:
    sym = symbol.upper()
    with get_conn() as conn:
        row = fd_store.latest_facts(conn, sym)
    return _scrub(row)


def financial_metrics(symbol: str) -> dict[str, Any] | None:
    sym = symbol.upper()
    with get_conn() as conn:
        row = fd_store.latest_metrics(conn, sym)
    return _scrub(row)


def financial_statements(
    symbol: str, statement_type: str, limit: int = 4
) -> list[dict[str, Any]]:
    if statement_type not in (
        "income_statement", "balance_sheet", "cash_flow_statement"
    ):
        raise ValueError(
            "statement_type must be 'income_statement', 'balance_sheet', "
            "or 'cash_flow_statement'"
        )
    sym = symbol.upper()
    with get_conn() as conn:
        rows = fd_store.recent_financials(conn, sym, statement_type, limit=limit)
    return [_scrub(r) for r in rows]


def earnings_history(symbol: str, limit: int = 4) -> list[dict[str, Any]]:
    sym = symbol.upper()
    with get_conn() as conn:
        rows = fd_store.recent_earnings(conn, sym, limit=limit)
    return [_scrub(r) for r in rows]


def filings(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    sym = symbol.upper()
    with get_conn() as conn:
        rows = fd_store.recent_filings(conn, sym, limit=limit)
    return [_scrub(r) for r in rows]


def insider_trades(symbol: str, limit: int = 15) -> list[dict[str, Any]]:
    sym = symbol.upper()
    with get_conn() as conn:
        rows = fd_store.recent_insiders(conn, sym, limit=limit)
    return [_scrub(r) for r in rows]


def top_holders(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    sym = symbol.upper()
    with get_conn() as conn:
        rows = fd_store.top_holders(conn, sym, limit=limit)
    return [_scrub(r) for r in rows]


def news(
    symbols: list[str] | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """News for one or many symbols. None = held + watchlist universe."""
    if symbols is None:
        symbols = _default_news_universe()
    if not symbols:
        return []
    syms = [s.upper() for s in symbols]
    with get_conn() as conn:
        rows = fd_store.recent_news(conn, syms, limit=limit)
    return [_scrub(r) for r in rows]


# ────────────────────────── freshness ──────────────────────────


# Single source of truth shared with health.fd_freshness.
_FD_TABLES = common.FD_TABLES


def freshness(symbol: str | None = None) -> dict[str, Any]:
    """Last fetched_at per FD table.

    With symbol=None, returns global max(fetched_at) and total row count per
    table. With a symbol, returns those numbers scoped to that symbol — handy
    for deciding whether to ask the user to refresh the enrichment.
    """
    out: dict[str, Any] = {"symbol": symbol.upper() if symbol else None, "sections": {}}
    with get_conn() as conn:
        for section, tbl in _FD_TABLES.items():
            try:
                with conn.cursor() as cur:
                    # tbl is a hardcoded value from _FD_TABLES; compose it as a
                    # quoted identifier instead of formatting it into the SQL.
                    if symbol:
                        cur.execute(
                            sql.SQL("SELECT MAX(fetched_at), COUNT(*) FROM ")
                            + sql.Identifier(tbl)
                            + sql.SQL(" WHERE symbol = %s"),
                            (symbol.upper(),),
                        )
                    else:
                        cur.execute(
                            sql.SQL("SELECT MAX(fetched_at), COUNT(*) FROM ")
                            + sql.Identifier(tbl)
                        )
                    ts, count = cur.fetchone()
            except Exception as e:
                conn.rollback()
                out["sections"][section] = {"error": str(e)}
                continue
            out["sections"][section] = {
                "table": tbl,
                "latest_fetched_at": ts.isoformat() if ts else None,
                "row_count": int(count or 0),
            }
    return out


# ────────────────────────── helpers ──────────────────────────


def _default_news_universe() -> list[str]:
    """Held symbols + watchlist (matches the dashboard News tab default)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH pos AS (
                  SELECT symbol,
                         SUM(CASE WHEN side='BUY' THEN  quantity
                                  WHEN side='SELL' THEN -quantity END) AS qty
                  FROM lots GROUP BY symbol
                )
                SELECT i.symbol
                FROM instruments i
                LEFT JOIN pos p ON p.symbol = i.symbol
                WHERE COALESCE(p.qty, 0) > 0 OR i.watchlist = TRUE
                ORDER BY i.symbol
                """,
            )
            return [r[0] for r in cur.fetchall()]


# Shared with positions — see common.clean_record.
_scrub = common.clean_record
