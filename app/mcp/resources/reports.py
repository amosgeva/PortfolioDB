"""portfolio://reports/executive/latest + portfolio://philosophy.

The executive report is generated on-demand by reusing exec_report.build_html
unchanged — same HTML the dashboard's sidebar export button produces. The
philosophy resource just streams the markdown the advisor uses.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP

import advisor
import exec_report

from app.mcp.deps import get_conn

log = logging.getLogger(__name__)


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
            log.exception("executive report generation failed")
            return (
                "<!doctype html><meta charset='utf-8'>"
                f"<title>Report unavailable</title>"
                f"<p>Executive report failed: <code>{e}</code></p>"
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
            return f"# Philosophy unavailable\n\n`{e}`"
        if not text:
            return (
                "# Philosophy not configured\n\n"
                f"Drop a `philosophy.md` at `{advisor.PHILOSOPHY_PATH}` "
                "and it will surface here."
            )
        return text
