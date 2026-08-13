"""MCP prompts — canned analyses with portfolio data pre-baked.

Each prompt function pulls live data via the service layer and embeds it
into the prompt body, so the agent receives one self-contained request
rather than having to chain tool calls itself. The prompts intentionally
mirror the analyses an experienced investor would run (morning brief,
concentration check, pre-trade gut check) so they're useful as
slash-commands inside an MCP client.

Each prompt is decorated with @mcp.prompt and returns either a string or
a list of fastmcp.prompts.Message objects. Strings default to a user-role
message; lists let us pre-seed assistant context where helpful.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from fastmcp import FastMCP
from fastmcp.prompts import Message

from app.mcp.services import (
    activity as activity_service,
    analytics as analytics_service,
    fundamentals as fundamentals_service,
    kpis as kpis_service,
    pnl as pnl_service,
    positions as positions_service,
    prices as prices_service,
)

import advisor


# ────────────────────────── shared helpers ──────────────────────────


def _philosophy_preamble() -> str:
    """Pull the user's philosophy.md if present; empty string otherwise."""
    try:
        text = advisor.load_philosophy()
    except Exception:
        text = ""
    if not text:
        return ""
    return f"\n## User's investment philosophy\n\n{text.strip()}\n"


def _money(x: float | None) -> str:
    if x is None:
        return "—"
    return f"${x:,.2f}"


def _pct(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "—"
    return f"{x:+.2f}%" if signed else f"{x:.2f}%"


def _num(x: float | None, places: int) -> str:
    """Plain number, or an em dash when undefined (e.g. HHI with no priced
    positions, where 0.0000 would read as perfect diversification)."""
    if x is None:
        return "—"
    return f"{x:.{places}f}"


def _kpi_block(k: dict[str, Any]) -> str:
    return (
        f"- AUM: {_money(k['aum'])}\n"
        f"- Market value: {_money(k['market_value'])}  · cash {_money(k['cash'])}  · cost basis {_money(k['cost_basis'])}\n"
        f"- Unrealized P&L: {_money(k['unrealized_pnl'])} ({_pct(k['unrealized_pct'], signed=True)})\n"
        f"- Realized P&L: {_money(k['realized_pnl'])}\n"
        f"- Total return: {_pct(k['total_return_pct'], signed=True)}\n"
        f"- Daily change: {_money(k['daily_change_usd'])} ({_pct(k['daily_change_pct'], signed=True)})\n"
        f"- Δ last snapshot: {_money(k['delta_last_snapshot_usd'])}\n"
        f"- Active symbols: {k['active_symbols']}  · watchlist: {k['watchlist_count']}\n"
        f"- As of: {k['as_of']}\n"
    )


def _window_to_date(window: str) -> date:
    today = date.today()
    table = {
        "1d": today - timedelta(days=1),
        "1w": today - timedelta(days=7),
        "2w": today - timedelta(days=14),
        "1m": today - timedelta(days=30),
        "3m": today - timedelta(days=90),
        "ytd": date(today.year, 1, 1),
    }
    if window not in table:
        raise ValueError(f"window must be one of {sorted(table)}")
    return table[window]


# ────────────────────────── prompts ──────────────────────────


def register(mcp: FastMCP) -> None:

    @mcp.prompt(
        name="morning_brief",
        title="Morning brief",
        description="Today's KPIs + top movers + relevant news, framed for a morning portfolio check-in.",
    )
    def morning_brief(style: str = "terse") -> str:
        """Generate a morning brief in either 'terse' (one paragraph) or 'detailed' (sections) style."""
        if style not in ("terse", "detailed"):
            raise ValueError("style must be 'terse' or 'detailed'")

        kpis = kpis_service.portfolio_kpis("fifo")
        movers = prices_service.top_movers(window="snapshot", limit=3, direction="both")
        prev_movers = prices_service.top_movers(window="1d", limit=3, direction="both")
        news = fundamentals_service.news(limit=5)

        gainers = [f"{m['symbol']} ({_money(m['delta_usd'])})" for m in movers["gainers"]]
        losers = [f"{m['symbol']} ({_money(m['delta_usd'])})" for m in movers["losers"]]
        d_gainers = [f"{m['symbol']} ({_money(m['delta_usd'])})" for m in prev_movers["gainers"]]
        d_losers = [f"{m['symbol']} ({_money(m['delta_usd'])})" for m in prev_movers["losers"]]
        news_lines = [
            f"- [{n.get('symbol')}] {n.get('title') or ''}"
            for n in news[:5]
            if n.get("title")
        ]

        return (
            f"You are the user's portfolio brief writer. Produce a {style} morning brief.\n"
            f"Lead with what moved today and why it matters, not just numbers. If something needs the user's attention "
            f"(concentration spike, large loss, earnings tomorrow), call it out. Don't speculate beyond the data given.\n"
            f"{_philosophy_preamble()}\n"
            f"## Portfolio KPIs\n{_kpi_block(kpis)}\n"
            f"## Top movers since the last snapshot\n"
            f"- Gainers: {', '.join(gainers) if gainers else 'none'}\n"
            f"- Losers : {', '.join(losers) if losers else 'none'}\n\n"
            f"## Top movers vs yesterday's EOD\n"
            f"- Gainers: {', '.join(d_gainers) if d_gainers else 'none'}\n"
            f"- Losers : {', '.join(d_losers) if d_losers else 'none'}\n\n"
            f"## Recent news\n"
            + ("\n".join(news_lines) if news_lines else "_no news on file_") + "\n\n"
            f"Write the brief now. Use Markdown. "
            f"{'Keep it to one short paragraph.' if style == 'terse' else 'Use 3-4 short sections.'}"
        )

    @mcp.prompt(
        name="analyze_concentration_risk",
        title="Concentration risk analysis",
        description="Concentration metrics + sector allocation + correlation clusters, framed as a risk review.",
    )
    def analyze_concentration_risk(top_n: int = 10) -> str:
        conc = analytics_service.concentration(top_n=top_n)
        sectors = analytics_service.sector_allocation()
        corr = analytics_service.correlation_matrix(window="3m", min_observations=20)

        top_rows = "\n".join(
            f"- {r['symbol']}: {_money(r['market_value'])} ({_pct(r['weight_pct'])})"
            for r in conc["rows"]
        )
        sector_rows = "\n".join(
            f"- {r['sector']}: {_money(r['market_value'])} ({_pct(r['weight_pct'])}) — {', '.join(r['symbols'])}"
            for r in sectors["rows"]
        )
        top_pairs = "\n".join(
            f"- {p['a']} / {p['b']}: {p['correlation']:+.3f}"
            for p in corr["pairs"][:10]
        )
        clusters = "\n".join(
            f"- {sym}: correlated with {', '.join(others)}"
            for sym, others in sorted(corr["clusters"].items())
        )

        return (
            "You are the user's risk analyst. Assess concentration and redundancy risk in this portfolio.\n"
            "Focus on: (1) single-name concentration, (2) sector tilt, (3) symbols that are effectively redundant "
            "(high correlation), (4) what's missing as a diversifier. Recommend at most three concrete actions. "
            "Don't recommend buying or selling specific names — only adjustments to weights or asset types.\n"
            f"{_philosophy_preamble()}\n"
            f"## Concentration\n"
            f"- HHI: {_num(conc['hhi'], 4)}  (effective N = {_num(conc['effective_n'], 2)})\n"
            f"- Single largest: {_pct(conc['single_largest_pct'])}\n"
            f"- Top {top_n} share: {_pct(conc['top_n_share_pct'])} of {_money(conc.get('total_market_value'))}\n\n"
            f"### Top {top_n} positions\n{top_rows or '_none_'}\n\n"
            f"## Sector allocation\n{sector_rows or '_none_'}\n\n"
            f"## Top correlations (3m daily, |corr| sorted)\n{top_pairs or '_no data_'}\n\n"
            f"## Redundancy clusters (>0.70)\n{clusters or '_none_'}\n"
        )

    @mcp.prompt(
        name="review_recent_activity",
        title="Review recent activity",
        description="Summarize trades, cash moves, and realized P&L over a window.",
    )
    def review_recent_activity(window: str = "1w") -> str:
        since = _window_to_date(window)
        trades = activity_service.lots(since=since, limit=200)
        cash_hist = activity_service.cash_history(since=since, limit=50)
        realized = pnl_service.realized_pnl("fifo", since=since, group_by="symbol")

        trade_lines = [
            f"- {t['trade_date']} {t['side']:4s} {t['symbol']:6s} "
            f"qty={t['quantity']:.4f} @ {_money(t['price'])} "
            f"(fees {_money(t.get('fees', 0))}) "
            f"[{t.get('account') or '(none)'}]"
            for t in trades
        ]
        cash_lines = [
            f"- {c['ts']} {c['account']}: {_money(c['cash'])}"
            + (f"  — {c['note']}" if c.get("note") else "")
            for c in cash_hist
        ]
        realized_rows = "\n".join(
            f"- {r['bucket']}: {_money(r['realized_pnl'])} ({r['matches']} matches)"
            for r in realized["rows"]
        )

        return (
            f"You are reviewing the user's portfolio activity since {since.isoformat()} (window: {window}).\n"
            "Summarize: what was bought, what was sold, net cash movement, and realized P&L. "
            "Call out anything unusual (a big single trade, abnormal fees, a closed position). "
            "Suggest follow-ups only if the data warrants — e.g., \"this trade left X with no offset for Y\".\n"
            f"{_philosophy_preamble()}\n"
            f"## Trades ({len(trades)})\n"
            + ("\n".join(trade_lines) if trade_lines else "_no trades in window_") + "\n\n"
            f"## Cash changes ({len(cash_hist)})\n"
            + ("\n".join(cash_lines) if cash_lines else "_no cash adjustments in window_") + "\n\n"
            f"## Realized P&L by symbol — total {_money(realized['total_realized'])}, "
            f"{realized['match_count']} matches\n"
            f"{realized_rows or '_no realized matches in window_'}\n"
        )

    @mcp.prompt(
        name="compare_methods_brief",
        title="FIFO vs avg-cost diff",
        description="Side-by-side FIFO and average-cost realized P&L with explanation.",
    )
    def compare_methods_brief() -> str:
        diff = pnl_service.compare_methods()
        rows_lines = "\n".join(
            f"- {r['symbol']}: FIFO {_money(r['fifo_realized'])}  "
            f"AVG {_money(r['avg_realized'])}  "
            f"diff {_money(r['diff'])}"
            for r in sorted(diff["rows"], key=lambda x: abs(x["diff"]), reverse=True)[:10]
        )
        return (
            "You are explaining the difference between FIFO and average-cost realized P&L for the user's portfolio. "
            "Identify the symbols where the two methods diverge most, explain why (e.g., \"FIFO closed the older, "
            "lower-cost lot first; avg-cost smoothed the basis\"), and note tax implications only if asked.\n\n"
            f"## Totals\n"
            f"- FIFO realized: {_money(diff['fifo_total'])}\n"
            f"- AVG realized:  {_money(diff['avg_total'])}\n"
            f"- Diff: {_money(diff['diff_total'])}\n\n"
            f"## Top divergences (by |diff|)\n{rows_lines or '_no divergences_'}\n"
        )

    @mcp.prompt(
        name="pre_trade_check",
        title="Pre-trade gut check",
        description="Compute the post-trade portfolio impact of a hypothetical BUY/SELL before you click submit.",
    )
    def pre_trade_check(
        symbol: str, side: str, qty: float, price: float | None = None
    ) -> str:
        side_u = side.upper()
        if side_u not in ("BUY", "SELL"):
            raise ValueError("side must be 'BUY' or 'SELL'")
        if qty <= 0:
            raise ValueError("qty must be > 0")

        sym = symbol.upper()
        kpis = kpis_service.portfolio_kpis("fifo")
        positions = positions_service.current_positions("fifo", held_only=False)
        pos = next((p for p in positions if p["symbol"] == sym), None)

        if price is None:
            latest = prices_service.latest_price(sym)
            price = latest["last_price"] if latest else None

        existing_qty = float(pos["qty"]) if pos else 0.0
        existing_mv = float(pos.get("market_value") or 0.0) if pos else 0.0
        existing_weight = float(pos.get("weight_pct") or 0.0) if pos else 0.0
        existing_cost = float(pos.get("open_cost") or 0.0) if pos else 0.0

        notional = (qty * price) if price is not None else None
        if side_u == "BUY":
            new_qty = existing_qty + qty
            new_cost = existing_cost + (notional or 0.0)
        else:  # SELL
            new_qty = existing_qty - qty
            # SELL doesn't change open_cost directly — engine re-computes.
            new_cost = existing_cost * (new_qty / existing_qty) if existing_qty else 0.0

        new_mv = (new_qty * price) if price is not None else None
        delta_mv = (new_mv - existing_mv) if new_mv is not None else None

        # Approx new weight assuming total MV moves by delta.
        new_total_mv = (kpis["market_value"] + (delta_mv or 0.0))
        new_weight = (
            (new_mv / new_total_mv * 100.0)
            if (new_mv is not None and new_total_mv)
            else None
        )

        cash = kpis["cash"]
        cash_after = (cash - notional) if (side_u == "BUY" and notional is not None) else (
            (cash + notional) if (side_u == "SELL" and notional is not None) else cash
        )

        cost_basis_shorts = (
            "SELL exceeds your existing open quantity — PortfolioDB does not support shorts; the engine will truncate."
            if (side_u == "SELL" and qty > existing_qty)
            else ""
        )

        return (
            "You are gut-checking a proposed trade for the user. Flag anything that warrants pausing: insufficient "
            "cash, concentration breach, fully closing a winning position, conflict with the user's philosophy. "
            "Be concise — bullet points. If the trade looks fine, say so plainly.\n"
            f"{_philosophy_preamble()}\n"
            f"## Proposed trade\n"
            f"- {side_u} {qty:.4f} {sym} @ {_money(price)}  (notional {_money(notional)})\n\n"
            f"## Current position\n"
            f"- qty: {existing_qty:.4f}  cost basis: {_money(existing_cost)}  "
            f"market value: {_money(existing_mv)}  weight: {_pct(existing_weight)}\n\n"
            f"## After this trade\n"
            f"- qty: {new_qty:.4f}\n"
            f"- approx cost basis: {_money(new_cost)}\n"
            f"- approx market value: {_money(new_mv)}\n"
            f"- approx new weight: {_pct(new_weight)}\n"
            f"- cash after: {_money(cash_after)}  (was {_money(cash)})\n"
            + (f"\n{cost_basis_shorts}\n" if cost_basis_shorts else "")
            + "\n## Portfolio context\n"
            f"- AUM {_money(kpis['aum'])}  · cash {_money(kpis['cash'])}  · active {kpis['active_symbols']}\n"
        )

    @mcp.prompt(
        name="fundamentals_brief",
        title="Fundamentals brief",
        description="One-page brief on a held symbol: facts, valuation, earnings, filings, insider activity, news.",
    )
    def fundamentals_brief(symbol: str) -> str:
        sym = symbol.upper()
        if fundamentals_service.is_etf(sym):
            news = fundamentals_service.news([sym], limit=10)
            news_lines = "\n".join(
                f"- {n.get('published_at', '')}  {n.get('title', '')}"
                for n in news
            )
            return (
                f"{sym} is an ETF. Financial Datasets does not carry fundamentals for ETFs — only news.\n\n"
                f"## Recent news ({len(news)})\n"
                f"{news_lines or '_none on file_'}\n"
            )

        facts = fundamentals_service.company_facts(sym)
        metrics = fundamentals_service.financial_metrics(sym)
        income = fundamentals_service.financial_statements(sym, "income_statement", limit=4)
        earnings = fundamentals_service.earnings_history(sym, limit=4)
        filings = fundamentals_service.filings(sym, limit=5)
        insiders = fundamentals_service.insider_trades(sym, limit=10)
        news = fundamentals_service.news([sym], limit=5)

        facts_block = (
            f"- Name: {facts.get('name', '—')}  · {facts.get('exchange', '—')}\n"
            f"- Sector / industry: {facts.get('sector', '—')} / {facts.get('industry', '—')}\n"
            f"- Country: {facts.get('location', '—')}\n"
            f"- Website: {facts.get('website', '—')}\n"
        ) if facts else "_no profile data on file_"

        metrics_block = (
            f"- Market cap: {_money(metrics.get('market_cap'))}\n"
            f"- P/E: {metrics.get('pe_ratio', '—')}  · P/S: {metrics.get('ps_ratio', '—')}  · EV/EBITDA: {metrics.get('ev_ebitda', '—')}\n"
            f"- Margins: gross {_pct((metrics.get('gross_margin') or 0)*100)}  · "
            f"op {_pct((metrics.get('operating_margin') or 0)*100)}  · "
            f"net {_pct((metrics.get('net_margin') or 0)*100)}\n"
            f"- ROE: {_pct((metrics.get('return_on_equity') or 0)*100)}  · "
            f"ROA: {_pct((metrics.get('return_on_assets') or 0)*100)}\n"
            f"- Revenue growth: {_pct((metrics.get('revenue_growth') or 0)*100, signed=True)}  · "
            f"EPS growth: {_pct((metrics.get('earnings_growth') or 0)*100, signed=True)}\n"
        ) if metrics else "_no metrics on file_"

        income_lines = "\n".join(
            f"- {s.get('report_period', '?')} ({s.get('fiscal_period', '?')}): "
            f"revenue {_money(s.get('revenue'))}, net income {_money(s.get('net_income'))}"
            for s in income
        )
        earnings_lines = "\n".join(
            f"- {e.get('fiscal_period', '?')}: EPS actual {e.get('eps_actual', '—')} vs est {e.get('eps_estimate', '—')} "
            f"({e.get('eps_surprise', '—')})"
            for e in earnings
        )
        filings_lines = "\n".join(
            f"- {f.get('filing_date', '?')}  {f.get('filing_type', '?')} — {f.get('url', '')}"
            for f in filings
        )
        insider_lines = "\n".join(
            f"- {i.get('transaction_date', '?')}  {i.get('name', '?')} ({i.get('title', '?')}): "
            f"{i.get('transaction_type', '?')} {i.get('transaction_shares', '?')} @ {_money(i.get('transaction_price_per_share'))}"
            for i in insiders
        )
        news_lines = "\n".join(f"- [{n.get('published_at', '?')}] {n.get('title', '')}" for n in news)

        return (
            f"You are writing a one-page fundamentals brief on {sym}. "
            "Lead with what would change a thesis — a margin compression, an earnings miss trend, heavy insider "
            "selling, or a regulatory filing. Don't restate the data; interpret it.\n"
            f"{_philosophy_preamble()}\n"
            f"## Profile\n{facts_block}\n"
            f"## Valuation & quality\n{metrics_block}\n"
            f"## Recent quarterly income\n{income_lines or '_no statements on file_'}\n\n"
            f"## Earnings beats/misses\n{earnings_lines or '_no earnings on file_'}\n\n"
            f"## Recent filings\n{filings_lines or '_no filings on file_'}\n\n"
            f"## Recent insider activity\n{insider_lines or '_no insider activity on file_'}\n\n"
            f"## Recent news\n{news_lines or '_no news on file_'}\n"
        )

    @mcp.prompt(
        name="drawdown_review",
        title="Drawdown review",
        description="Portfolio drawdown + per-symbol top-N drawdowns + attribution context.",
    )
    def drawdown_review(top_n: int = 5, since: date | None = None) -> str:
        port = analytics_service.drawdown_stats(symbol=None, since=since)
        positions = positions_service.current_positions("fifo", held_only=True)
        positions.sort(key=lambda p: float(p.get("market_value") or 0.0), reverse=True)
        top = positions[:top_n]

        per_symbol = []
        for p in top:
            sym = p["symbol"]
            stats = analytics_service.drawdown_stats(symbol=sym, since=since)
            per_symbol.append({
                "symbol": sym,
                "weight_pct": float(p.get("weight_pct") or 0.0),
                "max_dd": stats["max_drawdown_pct"],
                "current_dd": stats["current_drawdown_pct"],
                "recovered": stats["recovered"],
            })

        per_symbol_lines = "\n".join(
            f"- {r['symbol']:6s} weight {_pct(r['weight_pct'])}  "
            f"max DD {r['max_dd']:+.2f}%  current {r['current_dd']:+.2f}%  recovered={r['recovered']}"
            for r in per_symbol
        )

        portfolio_note = (
            "NOTE: portfolio drawdown is computed by replaying CURRENT held quantities against historical snapshots. "
            "If the user's holdings have changed materially over the window, these numbers conflate price moves with "
            "position changes — interpret with care."
        )

        return (
            "You are reviewing drawdowns. Identify which symbols contribute most to the current portfolio drawdown "
            "(weight × current_dd) and whether the user's biggest historical losses have recovered. Flag holdings still "
            "deep in drawdown with high weight as worth a second look.\n"
            f"{_philosophy_preamble()}\n"
            f"## Portfolio-level drawdown ({port['observations']} observations)\n"
            f"- Max drawdown:     {port['max_drawdown_pct']:+.2f}%   "
            f"(peak {_money(port['peak'])} → trough {_money(port['trough'])})\n"
            f"- Current drawdown: {port['current_drawdown_pct']:+.2f}%\n"
            f"- Recovered: {port['recovered']}\n"
            f"\n{portfolio_note}\n\n"
            f"## Top {top_n} holdings by weight — drawdown stats\n{per_symbol_lines or '_no holdings_'}\n"
        )
