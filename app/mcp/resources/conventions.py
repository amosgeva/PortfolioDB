"""portfolio://conventions — engine + dataset conventions an agent should know.

This is a curated subset of CLAUDE.md, written for the agent rather than the
human collaborator. Kept here (not loaded from CLAUDE.md) so the MCP server
doesn't drift if the human-facing file changes wording.
"""

from __future__ import annotations

from fastmcp import FastMCP

_CONVENTIONS_MD = """# PortfolioDB — Conventions for analyses

These rules govern how positions, P&L, and prices are computed across the
PortfolioDB code base. An agent reading data through MCP should respect them
when interpreting numbers or composing analyses.

## Ledger model
- **Append-only.** `instruments`, `lots`, `price_snapshots`, `cash_snapshots`
  are never updated in place. There is no `positions` table — open quantity,
  cost basis, and realized P&L are recomputed from `lots` on every read.
- **Lots encode direction in `side`**, not in sign. `quantity` is always
  positive. A BUY lot adds to position; a SELL lot reduces it.
- **`(symbol, account)` is the matching boundary.** FIFO and average-cost
  engines run per `(symbol, account)`. Cross-account fungibility happens only
  at the merge step (`portfolio.compute_fifo_merged` /
  `portfolio.compute_avg_cost_merged`), which sums per-symbol across accounts.

## P&L engines
- **FIFO** (`app/fifo.py`) — matches oldest BUYs first.
- **Average cost** (`app/avg_cost.py`) — moving weighted average; recomputed
  on every BUY.
- Both engines:
  - **BUY fees inflate cost basis.** `per_share_cost = (price*qty + fees) / qty`.
  - **SELL fees reduce proceeds.** `proceeds_ps = (price*qty − fees) / qty`.
  - **Shorts are not supported.** A SELL that exceeds open BUYs is truncated
    to the available quantity and a warning is logged.
- Money math uses `Decimal` end-to-end inside the engines. Floats appear only
  when converting to a DataFrame for display.

## Prices
- `price_snapshots` is append-only and keyed by `(symbol, ts)`.
- `snapshot_prices.py` runs on weekdays only, between 15:15 and 23:15
  in the reporting timezone — PORTFOLIODB_TZ, default Asia/Jerusalem (matching the run_snapshot.ps1 guard).
- **Latest price** for a symbol = `MAX(ts)` for that symbol; the dashboard
  joins this to positions to compute market value.
- **"Daily change"** is current value vs the most recent snapshot taken before
  reporting-local midnight (yesterday's EOD).
- **"Δ Last snapshot"** is current value vs the second-most-recent snapshot.

## ETFs and fundamentals
- These symbols are treated as ETFs (no FD fundamentals — only news):
  `GLD, IAU, VOO, QQQ, SPY, XLE, XLK, IWM, TLT, IBIT, EZBC`.
  See `fd_store.ETF_SYMBOLS`.
- All Financial Datasets enrichment lives in `fd_*` tables. Each row carries
  `fetched_at`; the full API payload is preserved in a `raw` JSONB column.
- The weekly enrichment job (`fd_weekly_enrichment.py`) is what populates
  these tables. If a symbol has no fundamentals, the enrichment may not have
  been run for it yet.

## KPI definitions (mirror the Streamlit dashboard)
- **AUM** = `Σ market_value(symbols)` + `Σ cash(accounts)` using the latest
  cash row per account.
- **Cost basis** = `Σ open_cost` from the chosen engine.
- **Unrealized P&L** = `Σ (qty × last_price) − open_cost`.
- **Realized P&L** = `Σ realized_pnl` from the chosen engine.
- **Total Return %** = `(realized_pnl + unrealized_pnl) / cost_basis × 100`.
- **Active symbols** = count of symbols with qty > 0.

## Symbol casing
- Symbols are stored and compared in uppercase. Always uppercase user input
  before querying.
"""


def register(mcp: FastMCP) -> None:
    @mcp.resource(
        "portfolio://conventions",
        name="PortfolioDB conventions",
        description="Engine and dataset rules an agent should respect when analyzing data.",
        mime_type="text/markdown",
    )
    def conventions_resource() -> str:
        return _CONVENTIONS_MD
