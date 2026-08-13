"""portfolio://positions/current — JSON dump of current FIFO-merged positions."""

from __future__ import annotations

import json

from fastmcp import FastMCP

from app.mcp.services import positions as positions_service


def register(mcp: FastMCP) -> None:

    @mcp.resource(
        "portfolio://positions/current",
        name="Current positions (FIFO)",
        description="JSON list of currently-held positions with market value and unrealized P&L.",
        mime_type="application/json",
    )
    def positions_resource() -> str:
        rows = positions_service.current_positions("fifo", held_only=True)
        return json.dumps({"method": "fifo", "positions": rows}, default=str, indent=2)
