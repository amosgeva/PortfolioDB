"""The collector's market window — the guard the container scheduler relies on.

conftest pins the reporting timezone to Asia/Jerusalem and freezes the settings
cache, so these tests describe the window in that zone and never touch a DB.
"""

from __future__ import annotations

import time as time_mod
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import market_window
import settings

JER = ZoneInfo("Asia/Jerusalem")


@pytest.fixture
def configure(monkeypatch):
    """Put window settings in the (frozen) settings cache."""
    def apply(**values):
        monkeypatch.setattr(settings, "_cache", dict(values))
        monkeypatch.setattr(settings, "_db_ok", True)
        monkeypatch.setattr(settings, "_cache_at", time_mod.monotonic())
    return apply


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "PORTFOLIODB_MARKET_START", "PORTFOLIODB_MARKET_END", "PORTFOLIODB_MARKET_WEEK",
    ):
        monkeypatch.delenv(name, raising=False)


class TestParsing:
    def test_defaults_when_unset(self, configure):
        configure()
        start, end, week = market_window.window()
        assert start.strftime("%H:%M") == market_window.DEFAULT_START
        assert end.strftime("%H:%M") == market_window.DEFAULT_END
        assert week == {0, 1, 2, 3, 4}

    def test_env_and_db_precedence(self, configure, monkeypatch):
        monkeypatch.setenv("PORTFOLIODB_MARKET_START", "09:00")
        configure()
        assert market_window.window()[0].strftime("%H:%M") == "09:00"
        configure(market_window_start="10:30")          # DB wins over env
        assert market_window.window()[0].strftime("%H:%M") == "10:30"

    def test_garbage_time_falls_back_to_default(self, configure):
        configure(market_window_start="not-a-time")
        assert market_window.window()[0].strftime("%H:%M") == market_window.DEFAULT_START

    @pytest.mark.parametrize("spec,expected", [
        ("1-5", {0, 1, 2, 3, 4}),
        ("1,3,5", {0, 2, 4}),
        ("6,7", {5, 6}),
        ("0", {6}),          # cron 0 = Sunday
        ("7", {6}),          # cron 7 = Sunday too
        ("1-7", {0, 1, 2, 3, 4, 5, 6}),
        ("", {0, 1, 2, 3, 4}),
        ("garbage", {0, 1, 2, 3, 4}),
    ])
    def test_week_specs(self, configure, spec, expected):
        configure(market_week=spec)
        assert market_window.window()[2] == expected


class TestIsOpen:
    def test_matches_the_retired_powershell_guard(self, configure):
        """The old run_snapshot.ps1 rule: weekdays 15:15-23:15 Jerusalem."""
        configure(market_window_start="15:15", market_window_end="23:15", market_week="1-5")
        wednesday = lambda h, m: datetime(2026, 8, 12, h, m, tzinfo=JER)  # noqa: E731
        assert market_window.is_open(wednesday(15, 15)) is True      # inclusive start
        assert market_window.is_open(wednesday(23, 15)) is True      # inclusive end
        assert market_window.is_open(wednesday(19, 0)) is True
        assert market_window.is_open(wednesday(15, 14)) is False
        assert market_window.is_open(wednesday(23, 16)) is False
        assert market_window.is_open(wednesday(3, 0)) is False

    def test_weekend_closed(self, configure):
        configure(market_window_start="15:15", market_window_end="23:15", market_week="1-5")
        saturday = datetime(2026, 8, 15, 19, 0, tzinfo=JER)
        sunday = datetime(2026, 8, 16, 19, 0, tzinfo=JER)
        assert market_window.is_open(saturday) is False
        assert market_window.is_open(sunday) is False

    def test_window_wrapping_midnight(self, configure):
        configure(market_window_start="22:00", market_window_end="04:00", market_week="1-7")
        assert market_window.is_open(datetime(2026, 8, 12, 23, 30, tzinfo=JER)) is True
        assert market_window.is_open(datetime(2026, 8, 12, 2, 0, tzinfo=JER)) is True
        assert market_window.is_open(datetime(2026, 8, 12, 12, 0, tzinfo=JER)) is False

    def test_naive_input_read_as_reporting_local(self, configure):
        configure(market_window_start="15:15", market_window_end="23:15", market_week="1-5")
        # 16:00 naive is inside the window when read as Jerusalem; the same
        # wall time in UTC would be 19:00 Jerusalem — also inside, so pick an
        # hour where the two disagree: 14:00 Jerusalem is outside, but 14:00
        # UTC is 17:00 Jerusalem, inside.
        assert market_window.is_open(datetime(2026, 8, 12, 14, 0)) is False

    def test_utc_instant_converted_before_comparing(self, configure):
        configure(market_window_start="15:15", market_window_end="23:15", market_week="1-5")
        utc = ZoneInfo("UTC")
        # 13:00 UTC = 16:00 Jerusalem (IDT, +3) → inside the window.
        assert market_window.is_open(datetime(2026, 8, 12, 13, 0, tzinfo=utc)) is True
        # 05:00 UTC = 08:00 Jerusalem → outside.
        assert market_window.is_open(datetime(2026, 8, 12, 5, 0, tzinfo=utc)) is False

    def test_describe_mentions_zone_and_days(self, configure):
        configure(market_window_start="15:15", market_window_end="23:15", market_week="1-5")
        text = market_window.describe()
        assert "15:15" in text and "23:15" in text
        assert "Asia/Jerusalem" in text
        assert "Mon" in text and "Fri" in text and "Sat" not in text
