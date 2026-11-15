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

import re
from typing import Any

import fd_store
from psycopg2 import sql

from app.mcp.deps import explain_db_error, get_conn
from app.mcp.services import common

# Caps on client-controlled sizes (audit M-5).
MAX_ROWS = 100
MAX_NEWS = 200
MAX_NEWS_SYMBOLS = 100

# Vendor columns that hold free text — names, titles, headlines — wrapped as
# untrusted data. The raw upstream JSON is not returned at all (audit M-6).
VENDOR_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "facts": ("name", "industry", "category", "location", "website"),
    "metrics": (),
    "statements": (),
    "earnings": (),
    "filings": ("url",),
    "insiders": ("name", "title", "security_title"),
    "ownership": ("investor",),
    "news": ("title", "source", "url"),
}
# Most numeric line items a statement row carries from its raw payload, and
# the shape a line-item label must have to be returned at all.
MAX_LINE_ITEMS = 200
_LINE_ITEM_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")


def is_etf(symbol: str) -> bool:
    return symbol.upper() in fd_store.ETF_SYMBOLS


# ────────────────────────── single-symbol reads ──────────────────────────


def company_facts(symbol: str) -> dict[str, Any] | None:
    sym = symbol.upper()
    with get_conn() as conn:
        row = fd_store.latest_facts(conn, sym)
    return _vendor(row, "facts")


def financial_metrics(symbol: str) -> dict[str, Any] | None:
    sym = symbol.upper()
    with get_conn() as conn:
        row = fd_store.latest_metrics(conn, sym)
    return _vendor(row, "metrics")


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
    common.check_limit("limit", limit, MAX_ROWS)
    sym = symbol.upper()
    with get_conn() as conn:
        rows = fd_store.recent_financials(conn, sym, statement_type, limit=limit)
    return [_statement(r) for r in rows]


def earnings_history(symbol: str, limit: int = 4) -> list[dict[str, Any]]:
    common.check_limit("limit", limit, MAX_ROWS)
    sym = symbol.upper()
    with get_conn() as conn:
        rows = fd_store.recent_earnings(conn, sym, limit=limit)
    return [_vendor(r, "earnings") for r in rows]


def filings(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    common.check_limit("limit", limit, MAX_ROWS)
    sym = symbol.upper()
    with get_conn() as conn:
        rows = fd_store.recent_filings(conn, sym, limit=limit)
    return [_vendor(r, "filings") for r in rows]


def insider_trades(symbol: str, limit: int = 15) -> list[dict[str, Any]]:
    common.check_limit("limit", limit, MAX_ROWS)
    sym = symbol.upper()
    with get_conn() as conn:
        rows = fd_store.recent_insiders(conn, sym, limit=limit)
    return [_vendor(r, "insiders") for r in rows]


def top_holders(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    common.check_limit("limit", limit, MAX_ROWS)
    sym = symbol.upper()
    with get_conn() as conn:
        rows = fd_store.top_holders(conn, sym, limit=limit)
    return [_vendor(r, "ownership") for r in rows]


def news(
    symbols: list[str] | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """News for one or many symbols. None = held + watchlist universe."""
    common.check_limit("limit", limit, MAX_NEWS)
    if symbols is None:
        symbols = _default_news_universe()
    if not symbols:
        return []
    syms = common.check_symbols(symbols, MAX_NEWS_SYMBOLS)
    with get_conn() as conn:
        rows = fd_store.recent_news(conn, syms, limit=limit)
    return [_vendor(r, "news") for r in rows]


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
                # Usually the table does not exist yet (enrichment never ran).
                # The driver's text names the database; say what it means.
                out["sections"][section] = {"table": tbl, "error": explain_db_error(e)}
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


def _vendor(row: dict[str, Any] | None, section: str) -> dict[str, Any] | None:
    """A vendor row without ``raw``, its free-text columns wrapped."""
    return _scrub(common.vendor_record(row, text_fields=VENDOR_TEXT_FIELDS[section]))


def _statement(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """A financial-statement row: the extracted columns plus the numeric line
    items of the raw payload.

    A statement's line items live only in ``raw``; dropping it outright would
    lose the data the tool exists to serve. Numbers carry no instructions, so
    the numeric scalars are kept (capped) and every string is left behind.
    """
    if row is None:
        return None
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    # Keys are vendor text too: only identifier-shaped labels (the vendor's
    # snake_case field names, at most 64 characters) are kept. Anything else
    # — a sentence, a 2,500-character instruction — is dropped and counted.
    items: dict[str, Any] = {}
    dropped = 0
    for k, v in raw.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        if isinstance(k, str) and _LINE_ITEM_KEY.fullmatch(k):
            items[k] = v
        else:
            dropped += 1
    out = _vendor(row, "statements") or {}
    out["line_items"] = dict(list(items.items())[:MAX_LINE_ITEMS])
    out["line_items_truncated"] = len(items) > MAX_LINE_ITEMS
    out["line_items_dropped_labels"] = dropped
    return out
