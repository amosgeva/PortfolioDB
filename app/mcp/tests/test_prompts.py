"""Tests for MCP prompts — each one should pull live-ish data and bake it
into a self-contained, non-empty prompt body.

We stub each underlying service to a deterministic small payload and assert
the prompt text contains the key facts. The goal is to catch refactors that
silently strip data from a prompt — not to test the model's rendering.
"""

from __future__ import annotations

import asyncio

import pytest


# ────────────────────────── shared scaffolding ──────────────────────────


@pytest.fixture
def render(env_token, monkeypatch):
    """Returns a sync helper to render a prompt by name.

    Patches every service the prompts module touches with simple deterministic
    payloads. Tests can override individual patches.
    """
    from fastmcp import FastMCP
    from app.mcp.prompts import prompts as prompts_module
    import advisor

    # Stub philosophy so it doesn't pull a giant doc.
    monkeypatch.setattr(advisor, "load_philosophy", lambda: "Don't lose money.")

    # Stub services.
    from app.mcp.services import (
        activity as activity_service,
        analytics as analytics_service,
        fundamentals as fundamentals_service,
        kpis as kpis_service,
        pnl as pnl_service,
        positions as positions_service,
        prices as prices_service,
    )

    monkeypatch.setattr(kpis_service, "portfolio_kpis", lambda *a, **kw: {
        "aum": 10000.0, "market_value": 9500.0, "cost_basis": 8000.0,
        "cash": 500.0, "cash_by_account": [],
        "unrealized_pnl": 1500.0, "unrealized_pct": 18.75,
        "realized_pnl": 200.0, "total_return_pct": 21.25,
        "daily_change_usd": 50.0, "daily_change_pct": 0.5,
        "delta_last_snapshot_usd": 5.0,
        "active_symbols": 3, "watchlist_count": 1,
        "method": "fifo", "as_of": "2026-05-20T19:00:00+00:00",
    })

    monkeypatch.setattr(prices_service, "top_movers", lambda *a, **kw: {
        "window": kw.get("window") or (a[0] if a else "snapshot"),
        "gainers": [{"symbol": "NVDA", "delta_usd": 12.5, "delta_pct": 1.0, "qty": 5, "last_price": 165.0, "base_price": 162.5}],
        "losers": [{"symbol": "AAPL", "delta_usd": -3.5, "delta_pct": -0.5, "qty": 10, "last_price": 195.0, "base_price": 195.35}],
    })
    monkeypatch.setattr(prices_service, "latest_price", lambda sym: {
        "symbol": sym.upper(), "last_price": 165.0, "ts": "2026-05-20T19:00:00+00:00",
        "age_seconds": 60, "bid": None, "ask": None, "source": "yfinance",
    })

    monkeypatch.setattr(fundamentals_service, "news", lambda symbols=None, limit=50: [
        {"symbol": "NVDA", "title": "Nvidia ships H200 chips at record pace", "published_at": "2026-05-20"},
        {"symbol": "AAPL", "title": "Apple announces new VR headset", "published_at": "2026-05-19"},
    ])
    monkeypatch.setattr(fundamentals_service, "is_etf", lambda sym: sym.upper() == "GLD")
    monkeypatch.setattr(fundamentals_service, "company_facts", lambda sym: {
        "symbol": sym.upper(), "name": f"{sym.upper()} Corp",
        "sector": "Information Technology", "industry": "Semiconductors",
        "exchange": "NASDAQ", "location": "USA", "website": "https://example.com",
    })
    monkeypatch.setattr(fundamentals_service, "financial_metrics", lambda sym: {
        "market_cap": 1.5e12, "pe_ratio": 30.0, "ps_ratio": 12.0, "ev_ebitda": 25.0,
        "gross_margin": 0.7, "operating_margin": 0.4, "net_margin": 0.3,
        "return_on_equity": 0.5, "return_on_assets": 0.25,
        "revenue_growth": 0.6, "earnings_growth": 0.55,
    })
    monkeypatch.setattr(fundamentals_service, "financial_statements", lambda sym, st, limit=4: [
        {"report_period": "2025-09-30", "fiscal_period": "Q3", "revenue": 5e10, "net_income": 1.5e10},
    ])
    monkeypatch.setattr(fundamentals_service, "earnings_history", lambda sym, limit=4: [
        {"fiscal_period": "Q3", "eps_actual": 1.20, "eps_estimate": 1.10, "eps_surprise": "BEAT"},
    ])
    monkeypatch.setattr(fundamentals_service, "filings", lambda sym, limit=10: [
        {"filing_date": "2025-10-15", "filing_type": "10-Q", "url": "https://sec.example/10q"},
    ])
    monkeypatch.setattr(fundamentals_service, "insider_trades", lambda sym, limit=15: [
        {"transaction_date": "2025-10-01", "name": "CEO", "title": "Chief Executive",
         "transaction_type": "sale", "transaction_shares": 1000,
         "transaction_price_per_share": 160.0},
    ])

    monkeypatch.setattr(positions_service, "current_positions", lambda *a, **kw: [
        {"symbol": "NVDA", "qty": 5.0, "open_cost": 600.0, "market_value": 825.0,
         "weight_pct": 60.0},
        {"symbol": "AAPL", "qty": 10.0, "open_cost": 1800.0, "market_value": 1950.0,
         "weight_pct": 40.0},
    ])

    monkeypatch.setattr(activity_service, "lots", lambda **kw: [
        {"id": 1, "symbol": "NVDA", "account": "IBKR", "side": "BUY",
         "trade_date": "2026-05-15", "quantity": 2.0, "price": 160.0,
         "fees": 0.5, "notes": None},
    ])
    monkeypatch.setattr(activity_service, "cash_history", lambda **kw: [
        {"ts": "2026-05-15T12:00:00+00:00", "account": "IBKR", "cash": 500.0, "note": "deposit"},
    ])

    monkeypatch.setattr(pnl_service, "realized_pnl", lambda *a, **kw: {
        "method": "fifo", "since": None, "until": None, "group_by": kw.get("group_by", "symbol"),
        "total_realized": 200.0, "match_count": 3,
        "rows": [
            {"bucket": "NVDA", "realized_pnl": 150.0, "buy_cost": 500.0,
             "sell_proceeds": 650.0, "matches": 2},
            {"bucket": "AAPL", "realized_pnl": 50.0, "buy_cost": 1000.0,
             "sell_proceeds": 1050.0, "matches": 1},
        ],
    })
    monkeypatch.setattr(pnl_service, "compare_methods", lambda **kw: {
        "fifo_total": 200.0, "avg_total": 180.0, "diff_total": 20.0,
        "rows": [
            {"symbol": "NVDA", "fifo_realized": 150.0, "avg_realized": 140.0, "diff": 10.0},
        ],
    })

    monkeypatch.setattr(analytics_service, "concentration", lambda top_n=10: {
        "top_n": top_n, "total_positions": 2, "total_market_value": 2775.0,
        "top_n_share_pct": 100.0, "single_largest_pct": 60.0,
        "hhi": 0.52, "effective_n": 1.92,
        "rows": [
            {"symbol": "NVDA", "market_value": 1650.0, "weight_pct": 60.0},
            {"symbol": "AAPL", "market_value": 1125.0, "weight_pct": 40.0},
        ],
    })
    monkeypatch.setattr(analytics_service, "sector_allocation", lambda: {
        "total_market_value": 2775.0,
        "rows": [
            {"sector": "Information Technology", "market_value": 2775.0,
             "weight_pct": 100.0, "symbols": ["AAPL", "NVDA"]},
        ],
    })
    monkeypatch.setattr(analytics_service, "correlation_matrix", lambda *a, **kw: {
        "symbols": ["NVDA", "AAPL"], "observations": 60,
        "matrix": {"NVDA": {"AAPL": 0.85}, "AAPL": {"NVDA": 0.85}},
        "pairs": [{"a": "NVDA", "b": "AAPL", "correlation": 0.85}],
        "diversifiers": [{"a": "NVDA", "b": "AAPL", "correlation": 0.85}],
        "clusters": {"NVDA": ["AAPL"], "AAPL": ["NVDA"]},
    })
    monkeypatch.setattr(analytics_service, "drawdown_stats", lambda symbol=None, since=None: {
        "symbol": symbol.upper() if symbol else None, "observations": 100,
        "max_drawdown_pct": -20.0, "current_drawdown_pct": -5.0,
        "peak": 100.0, "peak_ts": "2026-01-01", "trough": 80.0, "trough_ts": "2026-02-01",
        "recovered": False, "since": None,
    })

    # Build a fresh mcp and register the prompts onto it.
    mcp = FastMCP("test")
    prompts_module.register(mcp)

    def _render(name: str, args: dict | None = None) -> str:
        out = asyncio.run(mcp.render_prompt(name, args or {}))
        text = out.messages[0].content.text
        return text

    return _render


# ────────────────────────── per-prompt tests ──────────────────────────


def test_morning_brief_terse(render):
    text = render("morning_brief", {"style": "terse"})
    assert "morning brief" in text.lower()
    assert "$10,000.00" in text   # AUM from stub
    assert "NVDA" in text         # gainer
    assert "AAPL" in text         # loser
    assert "Don't lose money" in text  # philosophy injected


def test_morning_brief_detailed(render):
    text = render("morning_brief", {"style": "detailed"})
    assert "3-4 short sections" in text  # mode-conditional instruction


def test_morning_brief_rejects_bad_style(render):
    with pytest.raises(Exception):
        render("morning_brief", {"style": "verbose"})


def test_analyze_concentration_risk(render):
    text = render("analyze_concentration_risk", {"top_n": "5"})
    assert "HHI" in text
    assert "0.5200" in text or "0.52" in text
    assert "Information Technology" in text
    assert "Top correlations" in text
    assert "Don't lose money" in text


def test_review_recent_activity(render):
    text = render("review_recent_activity", {"window": "1w"})
    assert "since" in text.lower()
    assert "NVDA" in text          # trade row
    assert "deposit" in text       # cash note carried through
    assert "Realized P&L" in text


def test_review_recent_activity_bad_window(render):
    with pytest.raises(Exception):
        render("review_recent_activity", {"window": "decade"})


def test_compare_methods_brief(render):
    text = render("compare_methods_brief", {})
    assert "FIFO realized" in text
    assert "AVG realized" in text
    assert "$20.00" in text          # diff_total from stub


def test_pre_trade_check_buy(render):
    text = render("pre_trade_check", {
        "symbol": "NVDA", "side": "BUY", "qty": "1.0", "price": "165.0",
    })
    assert "BUY 1.0000 NVDA" in text
    assert "notional $165.00" in text
    assert "Don't lose money" in text


def test_pre_trade_check_sell_exceeds_warns(render):
    text = render("pre_trade_check", {
        "symbol": "NVDA", "side": "SELL", "qty": "1000.0", "price": "165.0",
    })
    assert "exceeds your existing open quantity" in text


def test_pre_trade_check_rejects_bad_side(render):
    with pytest.raises(Exception):
        render("pre_trade_check", {"symbol": "NVDA", "side": "SHORT", "qty": "1.0"})


def test_pre_trade_check_rejects_zero_qty(render):
    with pytest.raises(Exception):
        render("pre_trade_check", {"symbol": "NVDA", "side": "BUY", "qty": "0.0"})


def test_fundamentals_brief_for_stock(render):
    text = render("fundamentals_brief", {"symbol": "NVDA"})
    assert "NVDA Corp" in text
    assert "Semiconductors" in text
    assert "10-Q" in text          # filings rendered
    assert "CEO" in text            # insider rendered


def test_fundamentals_brief_for_etf_shortcircuits(render):
    text = render("fundamentals_brief", {"symbol": "GLD"})
    assert "ETF" in text
    # ETF branch should NOT include the valuation block.
    assert "Valuation & quality" not in text


def test_drawdown_review(render):
    text = render("drawdown_review", {"top_n": "2"})
    assert "Portfolio-level drawdown" in text
    assert "Top 2 holdings" in text
    assert "-20.00%" in text        # from stub max_drawdown_pct
