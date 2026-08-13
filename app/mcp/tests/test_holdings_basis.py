"""Historical series value the past at the holdings actually held then.

`drawdown_stats` and `portfolio_value_history` used to multiply *today's* share
counts by historical prices. The old behaviour stays reachable via
holdings_basis='current_constant' for comparison, but is no longer the default.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest


class TestValidation:
    def test_drawdown_rejects_unknown_basis(self, env_token, fake_db):
        from app.mcp.services import analytics
        with pytest.raises(ValueError, match="holdings_basis"):
            analytics.drawdown_stats(holdings_basis="whatever")

    def test_value_history_rejects_unknown_basis(self, env_token, fake_db):
        from app.mcp.services import prices
        with pytest.raises(ValueError, match="holdings_basis"):
            prices.portfolio_value_history(
                date(2026, 1, 1), holdings_basis="whatever"
            )

    def test_value_history_still_validates_freq(self, env_token, fake_db):
        from app.mcp.services import prices
        with pytest.raises(ValueError, match="freq"):
            prices.portfolio_value_history(date(2026, 1, 1), freq="hourly")


class TestSeriesUsesHistoricalHoldings:
    """End-to-end through _portfolio_value_series with the DB layer faked."""

    def _patch(self, monkeypatch, lots, price_rows):
        from app.mcp.services import analytics, prices as prices_service

        monkeypatch.setattr(
            prices_service, "_value_lots", lambda conn, actions: lots
        )
        monkeypatch.setattr(
            prices_service, "_current_quantities", lambda: {"BBB": 5.0}
        )
        monkeypatch.setattr(
            analytics.corporate_actions, "fetch_actions", lambda conn: []
        )

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, *a, **kw): pass
            def fetchall(self): return price_rows

        class Conn:
            def cursor(self, *a, **kw): return Cur()
            def rollback(self): pass

        from contextlib import contextmanager

        @contextmanager
        def fake_conn():
            yield Conn()

        monkeypatch.setattr(analytics, "get_conn", fake_conn)

    def test_historical_basis_excludes_not_yet_bought_positions(
        self, env_token, fake_db, monkeypatch
    ):
        ts = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
        lots = [
            {"symbol": "AAA", "side": "BUY", "trade_date": date(2026, 1, 10),
             "quantity": 10.0},
            {"symbol": "BBB", "side": "BUY", "trade_date": date(2026, 2, 10),
             "quantity": 5.0},
        ]
        # BBB already has a quote in January, before it was owned.
        rows = [(ts, "AAA", 100.0), (ts, "BBB", 50.0)]
        self._patch(monkeypatch, lots, rows)

        from app.mcp.services import analytics
        series = analytics._portfolio_value_series(None)

        # AAA only: BBB is not bought until February.
        assert series[0][1] == pytest.approx(1000.0)

    def test_current_constant_basis_reproduces_the_old_number(
        self, env_token, fake_db, monkeypatch
    ):
        ts = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
        lots = [
            {"symbol": "AAA", "side": "BUY", "trade_date": date(2026, 1, 10),
             "quantity": 10.0},
            {"symbol": "BBB", "side": "BUY", "trade_date": date(2026, 2, 10),
             "quantity": 5.0},
        ]
        rows = [(ts, "AAA", 100.0), (ts, "BBB", 50.0)]
        self._patch(monkeypatch, lots, rows)

        from app.mcp.services import analytics
        series = analytics._portfolio_value_series(
            None, holdings_basis="current_constant"
        )

        # The old behaviour: today's holdings (BBB only) priced in January,
        # a month in which BBB was not owned.
        assert series[0][1] == pytest.approx(250.0)


def test_drawdown_reports_which_basis_it_used(env_token, fake_db, monkeypatch):
    from app.mcp.services import analytics

    monkeypatch.setattr(analytics, "_portfolio_value_series", lambda *a, **kw: [])
    out = analytics.drawdown_stats()
    assert out["holdings_basis"] == "historical"


def test_single_symbol_drawdown_has_no_holdings_basis(env_token, fake_db, monkeypatch):
    """A per-symbol price drawdown does not depend on holdings at all."""
    from app.mcp.services import analytics

    monkeypatch.setattr(analytics, "_symbol_price_series", lambda *a, **kw: [])
    out = analytics.drawdown_stats("NVDA")
    assert out["holdings_basis"] is None
