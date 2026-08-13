"""KPI tools (§5.5 of the plan).

`get_portfolio_kpis` returns the exact set of tiles shown on the Streamlit
dashboard's first three KPI rows — guarded by the parity test in
app/mcp/tests/test_kpi_parity.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastmcp import FastMCP

from app.mcp.services import kpis as kpis_service


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_portfolio_kpis(
        method: str = "fifo",
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Every KPI tile shown on the Streamlit dashboard, from one instant.

        Args:
            method: 'fifo' or 'avg' cost basis.
            as_of: pin every figure to this instant instead of now. Lots are
                filtered by its local date and all three price maps read at or
                before it, so market value, daily change and the snapshot
                delta describe the same moment. Naive input is read as
                the reporting timezone (PORTFOLIODB_TZ). None = now.

        Returns the KPI fields plus `meta` (as_of, timezone,
        reporting_currency, cost_basis_method, coverage_start/end,
        schema_version, app_version) and `null_reasons` for any undefined
        ratio.

        Definitions match streamlit_app.py:507-579 verbatim. See the
        portfolio://conventions resource for the formulas.
        """
        return kpis_service.portfolio_kpis(method, as_of=as_of)
