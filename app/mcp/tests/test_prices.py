"""Tests for the prices service — window math + filter shape."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.mcp.services import prices


def test_window_start_unknown_raises():
    with pytest.raises(ValueError):
        prices._window_start("bogus")


def test_window_start_all_returns_none():
    assert prices._window_start("all") is None


def test_window_start_1d_is_roughly_now_minus_1d():
    start = prices._window_start("1d")
    now = datetime.now(timezone.utc)
    delta = (now - start).total_seconds()
    assert 23 * 3600 < delta < 25 * 3600


def test_window_start_ytd_is_jan_1():
    start = prices._window_start("ytd")
    assert start.month == 1
    assert start.day == 1


def test_top_movers_rejects_bad_window():
    with pytest.raises(ValueError):
        prices.top_movers(window="2d")


def test_top_movers_rejects_bad_direction():
    with pytest.raises(ValueError):
        prices.top_movers(direction="sideways")


def test_price_history_rejects_bad_resample(env_token, fake_db, monkeypatch):
    """resample is validated before we ever hit the DB."""
    from app.mcp.deps import get_conn
    # Just ensure get_conn returns SOMETHING — we expect to error before using it.
    with pytest.raises(ValueError):
        prices.price_history("NVDA", date(2026, 1, 1), resample="weekly")


def test_price_change_invalid_window():
    with pytest.raises(ValueError):
        prices.price_change("NVDA", "bogus")


def test_top_movers_with_no_positions(monkeypatch, env_token):
    """No positions → no movers in either direction."""
    from app.mcp.services import positions as positions_service
    monkeypatch.setattr(
        positions_service, "current_positions", lambda *a, **kw: []
    )
    monkeypatch.setattr(prices, "second_latest_price_map", lambda: {})
    monkeypatch.setattr(prices, "prev_day_eod_price_map", lambda: {})
    out = prices.top_movers("snapshot", limit=5, direction="both")
    assert out["gainers"] == []
    assert out["losers"] == []
