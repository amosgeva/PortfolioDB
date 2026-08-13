"""portfolio://summary — markdown overview agents pull as ambient context."""

from __future__ import annotations

from fastmcp import FastMCP

from app.mcp.services import kpis as kpis_service
from app.mcp.services import positions as positions_service


def _pct(x: float | None) -> str:
    """Signed percentage, or an em dash when the metric is undefined (e.g. no
    cost basis to return on). Never renders an undefined ratio as 0.00%."""
    return "—" if x is None else f"{x:+.2f}%"


def register(mcp: FastMCP) -> None:

    @mcp.resource(
        "portfolio://summary",
        name="PortfolioDB summary",
        description="Markdown overview: KPI snapshot + top 10 positions + cash.",
        mime_type="text/markdown",
    )
    def summary_resource() -> str:
        kpis = kpis_service.portfolio_kpis("fifo")
        positions = positions_service.current_positions("fifo", held_only=True)
        positions.sort(
            key=lambda p: (p.get("market_value") or 0.0),
            reverse=True,
        )

        lines: list[str] = []
        lines.append("# PortfolioDB — Snapshot\n")
        lines.append(f"_As of {kpis['as_of']} (method: {kpis['method']})._\n")

        lines.append("## Totals\n")
        lines.append(f"- **AUM:** ${kpis['aum']:,.2f}")
        lines.append(f"- **Market value:** ${kpis['market_value']:,.2f}")
        lines.append(f"- **Cash:** ${kpis['cash']:,.2f}")
        lines.append(f"- **Cost basis:** ${kpis['cost_basis']:,.2f}")
        lines.append(
            f"- **Unrealized P&L:** ${kpis['unrealized_pnl']:,.2f} "
            f"({_pct(kpis['unrealized_pct'])})"
        )
        lines.append(f"- **Realized P&L:** ${kpis['realized_pnl']:,.2f}")
        lines.append(f"- **Total return:** {_pct(kpis['total_return_pct'])}")
        lines.append(
            f"- **Daily change:** ${kpis['daily_change_usd']:,.2f} "
            f"({_pct(kpis['daily_change_pct'])})"
        )
        lines.append(
            f"- **Δ last snapshot:** ${kpis['delta_last_snapshot_usd']:,.2f}"
        )
        lines.append(
            f"- **Active symbols:** {kpis['active_symbols']}    "
            f"**Watchlist:** {kpis['watchlist_count']}"
        )
        lines.append("")

        if kpis.get("cash_by_account"):
            lines.append("### Cash by account\n")
            for r in kpis["cash_by_account"]:
                lines.append(f"- {r['account']}: ${r['cash']:,.2f}")
            lines.append("")

        if positions:
            top = positions[:10]
            lines.append("## Top 10 positions by market value\n")
            lines.append(
                "| Symbol | Qty | Price | Market Value | Weight | Unrl P&L | Unrl % | Realized |"
            )
            lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
            for p in top:
                mv = p.get("market_value")
                upnl = p.get("unrealized_pnl")
                cost = p.get("open_cost") or 0.0
                upct = (upnl / cost * 100.0) if (upnl is not None and cost) else None
                lines.append(
                    f"| {p['symbol']} | {p['qty']:,.4f} | "
                    f"{('$' + format(p['last_price'], ',.2f')) if p.get('last_price') is not None else '—'} | "
                    f"{('$' + format(mv, ',.2f')) if mv is not None else '—'} | "
                    f"{(format(p.get('weight_pct') or 0.0, ',.2f') + '%') if p.get('weight_pct') is not None else '—'} | "
                    f"{('$' + format(upnl, ',.2f')) if upnl is not None else '—'} | "
                    f"{(format(upct, '+.2f') + '%') if upct is not None else '—'} | "
                    f"${p.get('realized_pnl', 0.0):,.2f} |"
                )
            lines.append("")
        else:
            lines.append("_No held positions._\n")

        return "\n".join(lines)
