"""The consolidated portfolio-review endpoint.

One call, one cutoff, every section — replacing the ~10 round trips a review
used to take, each of which independently read "now" and "the latest price".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastmcp import FastMCP

from app.mcp.services import review as review_service


def register(mcp: FastMCP) -> None:

    @mcp.tool
    def get_portfolio_review_snapshot(
        method: str = "fifo",
        account: str | None = None,
        as_of: datetime | None = None,
        benchmark: str | None = None,
        benchmark_period: str = "YTD",
        detail_level: str = "standard",
        correlation_window: str = "3m",
        top_n: int = 10,
        reporting_currency: str = "USD",
    ) -> dict[str, Any]:
        """Complete portfolio review from a single consistent cutoff.

        Prefer this over chaining get_portfolio_kpis + get_positions +
        get_concentration + get_allocation + get_period_returns +
        get_benchmark_comparison + get_drawdown_stats + get_fees_summary +
        get_dividend_summary + get_data_quality. Those each resolve their own
        "now" and re-read the latest price independently, so a review spanning
        a collector run (every 5 minutes) silently mixes two valuations. Here
        every number shares one instant.

        Sections: meta, summary, returns, benchmark, risk, concentration,
        attribution, data_quality, and (unless detail_level='summary') detail.

        Args:
            method: 'fifo' or 'avg' cost basis.
            account: restrict to one broker account. None = all merged.
            as_of: pin the whole review to this instant. None = now.
            benchmark: comparison symbol. None = PORTFOLIODB_BENCHMARK_SYMBOL
                or SPY.
            benchmark_period: 1D | WTD | MTD | YTD | 1Y | MAX.
            detail_level: 'summary' omits the detail block entirely,
                'standard' includes positions, 'full' adds the correlation
                matrix. summary and data_quality are never truncated.
            correlation_window: 1m | 3m | 6m | 1y | all.
            top_n: positions in the concentration breakdown.
            reporting_currency: USD only — anything else is rejected rather
                than silently ignored, because no FX rates are stored.

        Notes on reading the payload:
          - `summary.portfolio_value == invested_market_value + cash`, by
            construction and asserted in the reconciliation tests.
          - `fees_total` is reported but NOT added into `total_economic_pnl`:
            realized and unrealized P&L are already net of fees. Adding it
            double-counts. See docs/methodology.md.
          - `benchmark` may come back `status: insufficient_alignment` with
            null returns and an explanation, when the benchmark and portfolio
            do not share enough observed days. That is a refusal, not a zero.
          - `concentration.scenarios` are deterministic sensitivities labelled
            `basis: analytical_derived` — arithmetic on a stated assumption,
            never forecasts.
          - `data_quality.overall_status` travels with the numbers on purpose.
            Read it before trusting them.
        """
        return review_service.portfolio_review_snapshot(
            method=method,
            account=account,
            as_of=as_of,
            benchmark=benchmark,
            benchmark_period=benchmark_period,
            detail_level=detail_level,
            correlation_window=correlation_window,
            top_n=top_n,
            reporting_currency=reporting_currency,
        )
