"""FastMCP server entry point for PortfolioDB.

Mounts the MCP Streamable HTTP transport at /mcp/ and an unauthenticated
/healthz probe. All MCP-protocol traffic is gated by a static Bearer token
(see app/mcp/auth.py); /healthz is intentionally open so a tunnel / load
balancer can health-check without credentials.

Run modes:
    1. uvicorn app.mcp.server:asgi --host 0.0.0.0 --port 8765
       (used by run_mcp.ps1 — preferred for dev)
    2. python -m app.mcp.server
       (FastMCP runs its own uvicorn under the hood)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Make the sibling app/ modules importable when this file is run as a script.
_APP_DIR = Path(__file__).resolve().parent.parent
if str(_APP_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_APP_DIR.parent))

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.mcp.auth import build_verifier
from app.mcp.deps import close_pool, init_pool
from app.mcp.resources import conventions as conventions_resource
from app.mcp.prompts import prompts as prompts_module
from app.mcp.resources import kpis_today as kpis_today_resource
from app.mcp.resources import positions_current as positions_current_resource
from app.mcp.resources import reports as reports_resource
from app.mcp.resources import schema as schema_resource
from app.mcp.resources import summary as summary_resource
from app.mcp.services import health as health_service
from app.mcp.tools import activity_tools
from app.mcp.tools import analytics_tools
from app.mcp.tools import fees_tools
from app.mcp.tools import fundamentals_tools
from app.mcp.tools import income_tools
from app.mcp.tools import kpis_tools
from app.mcp.tools import meta_tools
from app.mcp.tools import pnl_tools
from app.mcp.tools import positions_tools
from app.mcp.tools import prices_tools
from app.mcp.tools import quality_tools
from app.mcp.tools import returns_tools
from app.mcp.tools import review_tools

logging.basicConfig(
    level=os.getenv("PORTFOLIODB_MCP_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("portfoliodb.mcp")


def build_server() -> FastMCP:
    """Construct the FastMCP server with all Phase 1 registrations wired in."""
    verifier = build_verifier()

    mcp = FastMCP(
        name="PortfolioDB",
        instructions=(
            "PortfolioDB is an append-only, lot-based personal portfolio "
            "ledger on Postgres. For any broad question about how the "
            "portfolio is doing, call get_portfolio_review_snapshot first: it "
            "returns every section from one consistent cutoff, where chaining "
            "the individual tools would mix valuations from different "
            "moments. Read portfolio://schema and "
            "portfolio://conventions before calling tools — they describe "
            "the data model, FIFO/avg-cost engines, KPI definitions, and "
            "the (symbol, account) scoping rule. portfolio://summary is a "
            "good first read for ambient context. Use get_portfolio_kpis "
            "for dashboard-parity totals, get_positions for the holdings "
            "list, get_realized_pnl / get_unrealized_pnl for breakdowns, "
            "and get_price_history or get_top_movers for price moves. "
            "For risk/diversification: get_concentration, "
            "get_sector_allocation, get_correlation_matrix, "
            "get_drawdown_stats. For fundamentals (non-ETF symbols): "
            "get_company_facts, get_financial_metrics, "
            "get_earnings_history, get_filings, get_insider_trades, "
            "get_top_holders, get_news. get_health surfaces snapshot + FD "
            "freshness; get_data_quality is the per-symbol version and is "
            "worth reading before you trust any total — it names which "
            "symbols have stale, missing or self-contradictory data and why. "
            "Most read tools accept as_of to pin every figure to one instant "
            "(lots and prices together), which is what makes separate calls "
            "reconcile. Pre-built analyses live as prompts — "
            "morning_brief, analyze_concentration_risk, "
            "review_recent_activity, compare_methods_brief, "
            "pre_trade_check, fundamentals_brief, drawdown_review — "
            "use them when the user asks for a canonical analysis rather "
            "than chaining tool calls."
        ),
        auth=verifier,
    )

    meta_tools.register(mcp)
    positions_tools.register(mcp)
    prices_tools.register(mcp)
    quality_tools.register(mcp)
    pnl_tools.register(mcp)
    kpis_tools.register(mcp)
    returns_tools.register(mcp)
    review_tools.register(mcp)
    activity_tools.register(mcp)
    analytics_tools.register(mcp)
    fees_tools.register(mcp)
    fundamentals_tools.register(mcp)
    income_tools.register(mcp)

    schema_resource.register(mcp)
    conventions_resource.register(mcp)
    summary_resource.register(mcp)
    positions_current_resource.register(mcp)
    kpis_today_resource.register(mcp)
    reports_resource.register(mcp)

    prompts_module.register(mcp)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        """Unauthenticated liveness probe.

        Returns 200 with a small JSON payload describing DB reachability and
        the last snapshot run's status. Intentionally cheap — just one
        SELECT 1 + one row from snapshot_runs.
        """
        try:
            payload = health_service.server_health()
        except Exception as e:
            log.exception("healthz failed")
            return JSONResponse(
                {"ok": False, "error": str(e)},
                status_code=503,
            )
        status_code = 200 if payload.get("ok") else 503
        return JSONResponse(payload, status_code=status_code)

    return mcp


# --- Module-level singletons used by the uvicorn launcher --------------------

mcp_server: FastMCP = build_server()

# Eagerly initialize the pool so the first request doesn't pay the
# connection-setup cost. Safe to call repeatedly.
try:
    init_pool()
except Exception:
    log.exception("Initial DB pool warm-up failed; will retry on first request")


def _shutdown() -> None:
    close_pool()


# Streamable HTTP transport at /mcp/. FastMCP returns a Starlette ASGI app
# the uvicorn launcher can hand to its server.
asgi = mcp_server.http_app(transport="streamable-http")


def main() -> None:
    """python -m app.mcp.server — convenience runner for ad-hoc starts.

    Binds localhost by default. This runner is what someone types to try the
    server out, and a default that reaches the LAN is the wrong one for a
    process that answers questions about your ledger — bearer auth sits in
    front either way, but the blast radius of a misconfigured token should
    not be the whole network. Set PORTFOLIODB_MCP_HOST=0.0.0.0 to serve other
    hosts deliberately; the shipped compose file does exactly that, and passes
    --host to uvicorn itself rather than coming through here.
    """
    host = os.getenv("PORTFOLIODB_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("PORTFOLIODB_MCP_PORT", "8765"))
    log.info("Starting PortfolioDB MCP on %s:%s", host, port)
    try:
        mcp_server.run(
            transport="streamable-http",
            host=host,
            port=port,
        )
    finally:
        _shutdown()


if __name__ == "__main__":
    main()
