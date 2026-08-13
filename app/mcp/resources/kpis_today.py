"""portfolio://kpis/today — JSON block of dashboard KPIs."""

from __future__ import annotations

import json

from fastmcp import FastMCP

from app.mcp.services import kpis as kpis_service


def register(mcp: FastMCP) -> None:

    @mcp.resource(
        "portfolio://kpis/today",
        name="Today's KPIs",
        description="Same KPI tiles as the Streamlit dashboard, as JSON.",
        mime_type="application/json",
    )
    def kpis_resource() -> str:
        return json.dumps(kpis_service.portfolio_kpis("fifo"), default=str, indent=2)
