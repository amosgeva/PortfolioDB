"""Portfolio drawdown is a loss of investment value, not a change of size.

The portfolio drawdown used to run on the market-value series, which falls
when securities are sold and rises when money is added: a withdrawal read as a
drawdown, a deposit could hide one. It now runs on the time-weighted growth
curve. These feed the same synthetic ledger through both definitions and
assert where they must differ and where they must agree.

The returns helpers are replaced at the module seam; no database.
"""

from __future__ import annotations

from datetime import date

import pytest

D1, D2, D3, D4 = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5)


def _buy(day, qty, price):
    return {"symbol": "AAA", "side": "BUY", "trade_date": day, "quantity": qty, "price": price, "fees": 0.0}


def _sell(day, qty, price):
    return {"symbol": "AAA", "side": "SELL", "trade_date": day, "quantity": qty, "price": price, "fees": 0.0}


def _patch(monkeypatch, lots, prices):
    from app.mcp.services import returns as returns_service

    monkeypatch.setattr(returns_service, "_fetch_lots", lambda cutoff=None: lots)
    monkeypatch.setattr(returns_service, "_price_by_day", lambda cutoff=None: prices)
    monkeypatch.setattr(returns_service, "_fetch_dividends", lambda cutoff=None: [])


def test_a_withdrawal_is_not_a_drawdown(env_token, fake_db, monkeypatch):
    """Two shares at 100, one sold at 100 the next day: the value series halves,
    the investment lost nothing."""
    from app.mcp.services import analytics

    _patch(monkeypatch, [_buy(D1, 2, 100.0), _sell(D2, 1, 100.0)],
           {D1: {"AAA": 100.0}, D2: {"AAA": 100.0}, D3: {"AAA": 100.0}})
    out = analytics.drawdown_stats()
    assert out["basis"] == "twr_growth_curve"
    assert out["max_drawdown_pct"] == pytest.approx(0.0)
    assert out["current_drawdown_pct"] == pytest.approx(0.0)
    assert out["peak"] == pytest.approx(1.0)


def test_a_contribution_does_not_hide_a_drawdown(env_token, fake_db, monkeypatch):
    """Price falls 20% on D2; on D3 the operator buys more at the low, so
    market value ends above where it started. The loss is still −20%."""
    from app.mcp.services import analytics

    _patch(monkeypatch, [_buy(D1, 1, 100.0), _buy(D3, 3, 80.0)],
           {D1: {"AAA": 100.0}, D2: {"AAA": 80.0}, D3: {"AAA": 80.0}})
    out = analytics.drawdown_stats()
    assert out["max_drawdown_pct"] == pytest.approx(-20.0)
    assert out["current_drawdown_pct"] == pytest.approx(-20.0)
    assert out["recovered"] is False


def test_a_genuine_loss_and_recovery_read_the_same_on_both_bases(env_token, fake_db, monkeypatch):
    from app.mcp.services import analytics

    lots = [_buy(D1, 1, 100.0)]
    prices = {D1: {"AAA": 100.0}, D2: {"AAA": 60.0}, D3: {"AAA": 110.0}}
    _patch(monkeypatch, lots, prices)
    twr_out = analytics.drawdown_stats()
    assert twr_out["max_drawdown_pct"] == pytest.approx(-40.0)
    assert twr_out["recovered"] is True
    assert twr_out["trough_ts"] == D2.isoformat()


def test_since_trims_the_curve(env_token, fake_db, monkeypatch):
    from app.mcp.services import analytics

    _patch(monkeypatch, [_buy(D1, 1, 100.0)],
           {D1: {"AAA": 100.0}, D2: {"AAA": 50.0}, D3: {"AAA": 50.0}, D4: {"AAA": 45.0}})
    whole = analytics.drawdown_stats()
    later = analytics.drawdown_stats(since=D3)
    assert whole["max_drawdown_pct"] == pytest.approx(-55.0)
    assert later["max_drawdown_pct"] == pytest.approx(-10.0)
    assert later["observations"] == 2


def test_current_constant_keeps_the_old_market_value_definition(env_token, fake_db, monkeypatch):
    from app.mcp.services import analytics

    series = [(D1, 200.0), (D2, 100.0)]
    monkeypatch.setattr(analytics, "_portfolio_value_series", lambda *a, **kw: series)
    out = analytics.drawdown_stats(holdings_basis="current_constant")
    assert out["basis"] == "market_value_current_constant"
    assert out["max_drawdown_pct"] == pytest.approx(-50.0)


def test_empty_curve_reports_its_basis(env_token, fake_db, monkeypatch):
    from app.mcp.services import analytics

    _patch(monkeypatch, [], {})
    out = analytics.drawdown_stats()
    assert out["observations"] == 0
    assert out["basis"] == "twr_growth_curve"
