"""Returns service tests (time-weighted).

No DB: the three data loaders on the returns service are monkeypatched with a
synthetic scenario where a mid-window contribution must NOT inflate the return.
``today`` is injected so period boundaries are deterministic.
"""

from __future__ import annotations

from datetime import date

import pytest

D1, D2, D3, D4 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)

# Buy AAA 10@100 (D1), double the position (D3) — a contribution, not a gain —
# with two +10% legs. True portfolio TWR over the window = 21%. SPY 400 -> 440
# = +10%, so relative = +11%.
_LOTS = [
    {"symbol": "AAA", "side": "BUY", "trade_date": D1, "quantity": 10.0, "price": 100.0, "fees": 0.0},
    {"symbol": "AAA", "side": "BUY", "trade_date": D3, "quantity": 10.0, "price": 110.0, "fees": 0.0},
]
_PRICES = {
    D1: {"AAA": 100.0, "SPY": 400.0},
    D2: {"AAA": 110.0, "SPY": 404.0},
    D3: {"AAA": 110.0, "SPY": 404.0},
    D4: {"AAA": 121.0, "SPY": 440.0},
}


def _patch(monkeypatch, lots=None, prices=None, divs=None):
    from app.mcp.services import returns
    lots = _LOTS if lots is None else lots
    prices = _PRICES if prices is None else prices
    monkeypatch.setattr(returns, "_fetch_lots", lambda *_a, **_kw: lots)
    monkeypatch.setattr(returns, "_price_by_day", lambda *_a, **_kw: prices)
    monkeypatch.setattr(returns, "_fetch_dividends", lambda *_a, **_kw: divs or [])
    return returns


def test_period_returns_twr_neutralises_contribution(env_token, monkeypatch):
    returns = _patch(monkeypatch)
    out = returns.period_returns(today=D4)
    assert out["basis"] == "time_weighted_return"
    p = out["periods"]
    assert p["MAX"] == pytest.approx(21.0)   # contribution does NOT inflate
    assert p["1D"] == pytest.approx(10.0)
    assert p["1Y"] == pytest.approx(21.0)    # <1y history -> inception
    assert p["WTD"] is None                  # no base before this Monday


def test_benchmark_rejects_bad_period(env_token):
    from app.mcp.services import returns
    with pytest.raises(ValueError):
        returns.benchmark_comparison("forever")


# These fixtures span four days, which is below the production alignment
# minimum. They exercise the return math, not the guard, so they lower the
# thresholds explicitly — which also demonstrates the override path.
_NO_GUARD = {"min_observations": 0, "min_alignment_pct": 0.0}


def test_benchmark_comparison_relative(env_token, monkeypatch):
    returns = _patch(monkeypatch)
    out = returns.benchmark_comparison("MAX", today=D4, **_NO_GUARD)
    assert out["status"] == "ok"
    assert out["benchmark_symbol"] == "SPY"
    assert out["portfolio_return_pct"] == pytest.approx(21.0)
    assert out["benchmark_return_pct"] == pytest.approx(10.0)   # SPY 400 -> 440
    assert out["relative_return_pct"] == pytest.approx(11.0)


def test_benchmark_missing_symbol_yields_none(env_token, monkeypatch):
    """A benchmark with no prices at all fails the alignment guard — zero
    aligned days is the most insufficient case there is."""
    returns = _patch(monkeypatch)
    out = returns.benchmark_comparison("MAX", today=D4, symbol="NOPE")
    assert out["status"] == "insufficient_alignment"
    assert out["benchmark_return_pct"] is None
    assert out["relative_return_pct"] is None
    assert out["alignment"]["aligned_observations"] == 0
    assert "NOPE" in out["explanation"]


def test_benchmark_missing_symbol_with_guard_disabled(env_token, monkeypatch):
    """With the guard lowered the portfolio side is still computed; only the
    benchmark half is null, each with its own reason."""
    returns = _patch(monkeypatch)
    out = returns.benchmark_comparison(
        "MAX", today=D4, symbol="NOPE", **_NO_GUARD
    )
    assert out["status"] == "ok"
    assert out["portfolio_return_pct"] == pytest.approx(21.0)
    assert out["benchmark_return_pct"] is None
    assert out["null_reasons"]["benchmark_return_pct"] == "insufficient_benchmark_history"
