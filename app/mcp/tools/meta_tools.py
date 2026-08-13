"""Meta / discovery tools: list_symbols, list_accounts, get_schema, get_health.

These are the minimal tools an agent should have to orient itself before
calling anything else.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from app.mcp.deps import get_conn
from app.mcp.services import health as health_service


def register(mcp: FastMCP) -> None:
    """Register meta tools on the given FastMCP instance."""

    @mcp.tool
    def list_symbols(
        held_only: bool = False,
        watchlist_only: bool = False,
    ) -> list[dict[str, Any]]:
        """List instruments known to PortfolioDB.

        Each row reports whether the symbol has an open position (qty > 0 from
        the lots ledger), and whether it is flagged as a watchlist-only symbol.

        Args:
            held_only: When true, return only symbols with qty > 0.
            watchlist_only: When true, return only symbols flagged watchlist=TRUE.

        Returns:
            List of dicts: {symbol, name, asset_type, exchange, currency,
            watchlist, has_open_position}.
        """
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH pos AS (
                      SELECT symbol,
                             SUM(CASE WHEN side='BUY'  THEN  quantity
                                      WHEN side='SELL' THEN -quantity
                                      ELSE 0 END) AS qty
                      FROM lots
                      GROUP BY symbol
                    )
                    SELECT i.symbol, i.name, i.asset_type, i.exchange, i.currency,
                           i.watchlist,
                           COALESCE(p.qty, 0) > 0 AS has_open_position
                    FROM instruments i
                    LEFT JOIN pos p ON p.symbol = i.symbol
                    ORDER BY i.symbol
                    """,
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["has_open_position"] = bool(d["has_open_position"])
            d["watchlist"] = bool(d["watchlist"])
            if held_only and not d["has_open_position"]:
                continue
            if watchlist_only and not d["watchlist"]:
                continue
            out.append(d)
        return out

    @mcp.tool
    def list_accounts() -> list[dict[str, Any]]:
        """List broker accounts known to PortfolioDB.

        Aggregates from both lots.account and cash_snapshots.account, so an
        account that holds only cash (no positions) is still reported.

        Returns:
            List of dicts: {account, position_lot_count, has_cash, latest_cash_ts}.
        """
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH lot_accts AS (
                      SELECT COALESCE(account, '(none)') AS account,
                             COUNT(*) AS lot_count
                      FROM lots
                      GROUP BY 1
                    ),
                    cash_accts AS (
                      SELECT account, MAX(ts) AS latest_ts
                      FROM cash_snapshots
                      GROUP BY account
                    )
                    SELECT COALESCE(l.account, c.account) AS account,
                           COALESCE(l.lot_count, 0)       AS lot_count,
                           c.latest_ts                    AS latest_cash_ts
                    FROM lot_accts l
                    FULL OUTER JOIN cash_accts c ON c.account = l.account
                    ORDER BY account
                    """,
                )
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for account, lot_count, latest_cash_ts in rows:
            out.append({
                "account": account,
                "position_lot_count": int(lot_count or 0),
                "has_cash": latest_cash_ts is not None,
                "latest_cash_ts": latest_cash_ts.isoformat() if latest_cash_ts else None,
            })
        return out

    @mcp.tool
    def get_schema() -> dict[str, Any]:
        """Return the PortfolioDB data dictionary.

        Describes the four core ledger tables (instruments, lots,
        price_snapshots, cash_snapshots), the snapshot_runs health log, the
        advisor tables, and the fd_* enrichment tables. Use this before
        composing analyses so the agent knows what columns are available.
        """
        # Static description — kept here (not derived) so we can include rules
        # that aren't expressible in column metadata (append-only, no shorts,
        # FIFO scoping, etc.). Resource portfolio://schema returns the same
        # content as Markdown.
        return _SCHEMA_DICT

    @mcp.tool
    def get_health() -> dict[str, Any]:
        """Return server + database + snapshot + FD enrichment health.

        Use this to check whether the latest price snapshot is fresh, whether
        the FD weekly enrichment has run recently, and whether the database
        is reachable.
        """
        return health_service.server_health()


_SCHEMA_DICT: dict[str, Any] = {
    "overview": (
        "PortfolioDB is an append-only, lot-based personal portfolio ledger on "
        "Postgres. Positions, cost basis, and realized P&L are not stored — "
        "they are always recomputed from the lots table on read."
    ),
    "conventions": {
        "append_only": "instruments, lots, price_snapshots, cash_snapshots are append-only by design.",
        "fifo_scoping": "FIFO matching is per (symbol, account); cross-account fungibility happens only at merge time.",
        "shorts": "Shorts are NOT supported. A SELL that exceeds open BUYs is truncated and logged.",
        "fees": "BUY fees inflate cost basis. SELL fees reduce proceeds.",
        "money": "Engines use Decimal end-to-end; floats appear only at display layers.",
        "symbols": "Symbols are stored UPPERCASE; CLIs/tools uppercase user input before query.",
        "snapshot_window": "snapshot_prices.py only runs on weekdays 15:15–23:15 in the reporting timezone (PORTFOLIODB_TZ).",
    },
    "tables": {
        "instruments": {
            "purpose": "Symbol registry; the watchlist flag marks non-held symbols we still snapshot.",
            "primary_key": "symbol",
            "columns": {
                "symbol": "TEXT, uppercase ticker.",
                "name": "TEXT, human-readable name (nullable).",
                "asset_type": "TEXT, enriched from yfinance: stock/ETF/Crypto/Commodity/Bond (defaults to 'stock' until enriched).",
                "currency": "TEXT, defaults to 'USD'.",
                "exchange": "TEXT, nullable.",
                "sector": "TEXT, enriched from yfinance (nullable); fd_company_facts.sector is the fallback.",
                "country": "TEXT, region/domicile enriched from yfinance (nullable).",
                "watchlist": "BOOLEAN, true = include in price snapshots even without an open position.",
                "created_at": "TIMESTAMPTZ.",
                "updated_at": "TIMESTAMPTZ.",
            },
        },
        "lots": {
            "purpose": "Every BUY/SELL trade. Direction encoded in side; quantity always positive.",
            "primary_key": "id",
            "unique_index": "(symbol, COALESCE(account,''), trade_date, quantity, price) — best-effort dedupe for CSV imports.",
            "columns": {
                "id": "BIGSERIAL.",
                "symbol": "TEXT, FK -> instruments.symbol.",
                "account": "TEXT, nullable. Broker / sub-account name.",
                "side": "TEXT CHECK (BUY|SELL).",
                "trade_date": "DATE.",
                "quantity": "NUMERIC(20,8), always > 0 — direction is in side.",
                "price": "NUMERIC(20,8), per-share, >= 0.",
                "fees": "NUMERIC(20,8), >= 0.",
                "notes": "TEXT.",
                "created_at": "TIMESTAMPTZ.",
            },
        },
        "price_snapshots": {
            "purpose": "Time-series quotes. Written by snapshot_prices.py.",
            "primary_key": "(symbol, ts)",
            "columns": {
                "ts": "TIMESTAMPTZ, snapshot time (UTC).",
                "symbol": "TEXT, FK -> instruments.symbol.",
                "last_price": "NUMERIC(20,8).",
                "bid": "NUMERIC(20,8), nullable.",
                "ask": "NUMERIC(20,8), nullable.",
                "source": "TEXT, default 'yfinance'.",
                "session": "TEXT, nullable.",
                "created_at": "TIMESTAMPTZ.",
            },
        },
        "cash_snapshots": {
            "purpose": "Manual cash balances per account (no broker pull). Latest row per account wins.",
            "primary_key": "id",
            "columns": {
                "id": "BIGSERIAL.",
                "account": "TEXT, default '(merged)'.",
                "cash": "NUMERIC(20,2).",
                "note": "TEXT.",
                "ts": "TIMESTAMPTZ, defaults to now().",
            },
        },
        "snapshot_runs": {
            "purpose": "One row per snapshot_prices.py invocation. Surfaced by get_health.",
            "primary_key": "id",
            "columns": {
                "id": "BIGSERIAL.",
                "ts_start": "TIMESTAMPTZ.",
                "ts_end": "TIMESTAMPTZ, nullable while running.",
                "status": "TEXT CHECK (running|ok|partial|failed).",
                "symbols_total": "INTEGER.",
                "symbols_ok": "INTEGER.",
                "symbols_failed": "INTEGER.",
                "error": "TEXT, populated on partial/failed.",
            },
        },
        "chat_log": {
            "purpose": "Append-only chat log used by the Advisor tab.",
            "columns": {
                "id": "BIGSERIAL.",
                "ts": "TIMESTAMPTZ.",
                "role": "TEXT CHECK (user|assistant).",
                "content": "TEXT.",
                "conversation_id": "TEXT, defaults 'default'.",
            },
        },
        "advisor_briefs": {
            "purpose": "Structured briefs produced by app/advisor.py (morning/eod/adhoc).",
            "columns": {
                "id": "BIGSERIAL.",
                "ts": "TIMESTAMPTZ.",
                "kind": "TEXT CHECK (morning|eod|adhoc).",
                "total_value": "NUMERIC(20,2).",
                "payload": "JSONB, full brief.",
            },
        },
    },
    "fd_tables": {
        "purpose": (
            "Financial Datasets enrichment, populated by fd_weekly_enrichment.py "
            "and read via app/fd_store.py. Hot columns are extracted; the full "
            "API payload lives in the raw JSONB column on each row."
        ),
        "names": [
            "fd_company_facts",
            "fd_financial_metrics",
            "fd_financial_statements",
            "fd_earnings",
            "fd_filings",
            "fd_insider_trades",
            "fd_institutional_ownership",
            "fd_news",
        ],
        "freshness": "Each row carries fetched_at; get_health reports the latest per table.",
    },
    "engines": {
        "fifo": (
            "app/fifo.py: per-(symbol, account) FIFO matching. Returns "
            "FifoResult(open_qty, open_cost, realized_pnl, matches, open_buys)."
        ),
        "avg_cost": (
            "app/avg_cost.py: moving weighted-average cost. Simpler totals only."
        ),
        "merge": (
            "app/portfolio.py: compute_fifo_merged / compute_avg_cost_merged group "
            "lot rows by (symbol, account), run the engine, and merge per symbol. "
            "Single source of truth for the dashboard and all MCP position tools."
        ),
    },
}
