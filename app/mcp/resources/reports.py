"""portfolio://reports/executive/latest + portfolio://philosophy.

The executive report is generated on-demand by reusing exec_report.build_html
unchanged — same HTML the dashboard's sidebar export button produces. The
philosophy resource just streams the markdown the advisor uses.
"""

from __future__ import annotations

import html
import logging

from fastmcp import FastMCP

import advisor
import exec_report

from app.mcp.deps import get_conn
from app.mcp.errors import client_message

log = logging.getLogger(__name__)


def _reason(exc: BaseException) -> str:
    """A client-safe one-liner for ``exc``: a database category or a generic note."""
    return client_message(exc) or "an internal error occurred; details are in the server log."


def register(mcp: FastMCP) -> None:

    @mcp.resource(
        "portfolio://reports/executive/latest",
        name="Executive report",
        description="On-demand HTML executive report (same as the dashboard's export).",
        mime_type="text/html",
    )
    def exec_report_resource() -> str:
        try:
            with get_conn() as conn:
                return exec_report.build_html(conn)
        except Exception as e:
            # The exception is logged in full here; the client gets a category,
            # never the driver's text or a traceback (audit M-7).
            log.exception("executive report generation failed")
            return (
                "<!doctype html><meta charset='utf-8'>"
                "<title>Report unavailable</title>"
                f"<p>Executive report failed: {html.escape(_reason(e))}</p>"
            )

    @mcp.resource(
        "portfolio://philosophy",
        name="Investment philosophy",
        description="The user's investment philosophy (philosophy.md), if present.",
        mime_type="text/markdown",
    )
    def philosophy_resource() -> str:
        try:
            text = advisor.load_philosophy()
        except Exception as e:
            log.exception("philosophy load failed")
            return f"# Philosophy unavailable\n\n{_reason(e)}"
        if not text:
            return (
                "# Philosophy not configured\n\n"
                f"Drop a `philosophy.md` at `{advisor.PHILOSOPHY_PATH}` "
                "and it will surface here."
            )
        return text
