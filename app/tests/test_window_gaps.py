"""Tests for `market_window.open_minutes_between`.

The dashboard needs to tell "the market was shut" apart from "we were not
looking". Wall-clock length cannot do it: the ordinary gap between a Friday
close and a Monday open is about 64 hours, and a collector that died for two
hours on a Tuesday morning is a much shorter gap that matters much more. The
measure is therefore how much of a gap the collector was supposed to be awake
for, which is what this function returns.

conftest pins the reporting timezone to Asia/Jerusalem for this suite, so the
window below is expressed in that zone.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import market_window
import reporting_tz


@pytest.fixture()
def window_9_to_17(monkeypatch):
    """A plain Mon-Fri 09:00-17:00 window, so the arithmetic is checkable by eye."""
    from datetime import time

    monkeypatch.setattr(
        market_window, "window", lambda: (time(9, 0), time(17, 0), {0, 1, 2, 3, 4})
    )
    return None


def local(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=reporting_tz.tzinfo())


class TestInsideOneDay:
    def test_wholly_inside_the_window(self, window_9_to_17):
        # Wednesday 10:00 -> 12:30
        assert market_window.open_minutes_between(
            local(2026, 9, 2, 10, 0), local(2026, 9, 2, 12, 30)
        ) == 150

    def test_wholly_outside_the_window(self, window_9_to_17):
        # Wednesday 02:00 -> 05:00, long before the window opens
        assert market_window.open_minutes_between(
            local(2026, 9, 2, 2, 0), local(2026, 9, 2, 5, 0)
        ) == 0

    def test_clipped_at_both_ends(self, window_9_to_17):
        # 07:00 -> 19:00 counts only the eight hours the window covers
        assert market_window.open_minutes_between(
            local(2026, 9, 2, 7, 0), local(2026, 9, 2, 19, 0)
        ) == 480

    def test_zero_length(self, window_9_to_17):
        t = local(2026, 9, 2, 10, 0)
        assert market_window.open_minutes_between(t, t) == 0

    def test_reversed_arguments_are_not_negative(self, window_9_to_17):
        assert market_window.open_minutes_between(
            local(2026, 9, 2, 12, 0), local(2026, 9, 2, 10, 0)
        ) == 0


class TestTheWeekendCase:
    """The case the whole function exists for."""

    def test_friday_close_to_monday_open_counts_nothing(self, window_9_to_17):
        # Friday 17:00 -> Monday 09:00 is 64 hours of wall clock and no window
        friday_close = local(2026, 8, 28, 17, 0)
        monday_open = local(2026, 8, 31, 9, 0)
        assert (monday_open - friday_close) == timedelta(hours=64)
        assert market_window.open_minutes_between(friday_close, monday_open) == 0

    def test_a_weekend_gap_is_below_the_threshold(self, window_9_to_17):
        gap = market_window.open_minutes_between(
            local(2026, 8, 28, 17, 0), local(2026, 8, 31, 9, 0)
        )
        assert gap < market_window.GAP_MINUTES

    def test_two_hours_on_a_tuesday_morning_is_above_it(self, window_9_to_17):
        gap = market_window.open_minutes_between(
            local(2026, 9, 1, 10, 0), local(2026, 9, 1, 12, 0)
        )
        assert gap == 120
        assert gap > market_window.GAP_MINUTES

    def test_a_weekend_that_swallows_a_real_outage_still_counts_the_outage(self, window_9_to_17):
        # Friday 15:00 -> Monday 11:00: two window hours on Friday, two on Monday
        assert market_window.open_minutes_between(
            local(2026, 8, 28, 15, 0), local(2026, 8, 31, 11, 0)
        ) == 240


class TestSpanningDays:
    def test_overnight_gap_counts_both_days(self, window_9_to_17):
        # Tuesday 16:00 -> Wednesday 10:00: one hour Tuesday, one hour Wednesday
        assert market_window.open_minutes_between(
            local(2026, 9, 1, 16, 0), local(2026, 9, 2, 10, 0)
        ) == 120

    def test_a_full_working_week(self, window_9_to_17):
        # Monday 00:00 -> Saturday 00:00 is five whole windows
        assert market_window.open_minutes_between(
            local(2026, 8, 31, 0, 0), local(2026, 9, 5, 0, 0)
        ) == 5 * 480


class TestWrappedWindow:
    """A window running past midnight, which an Asia/Tokyo operator needs."""

    @pytest.fixture()
    def window_22_to_04(self, monkeypatch):
        from datetime import time

        monkeypatch.setattr(
            market_window, "window", lambda: (time(22, 0), time(4, 0), {0, 1, 2, 3, 4})
        )

    def test_counts_the_evening_half(self, window_22_to_04):
        assert market_window.open_minutes_between(
            local(2026, 9, 2, 21, 0), local(2026, 9, 2, 23, 0)
        ) == 60

    def test_counts_the_after_midnight_half(self, window_22_to_04):
        # Thursday 03:00 -> 05:00 is one hour inside, one outside
        assert market_window.open_minutes_between(
            local(2026, 9, 3, 3, 0), local(2026, 9, 3, 5, 0)
        ) == 60

    def test_midday_counts_nothing(self, window_22_to_04):
        assert market_window.open_minutes_between(
            local(2026, 9, 2, 11, 0), local(2026, 9, 2, 15, 0)
        ) == 0


class TestNaiveTimestamps:
    def test_naive_is_read_as_reporting_local(self, window_9_to_17):
        naive = market_window.open_minutes_between(
            datetime(2026, 9, 2, 10, 0), datetime(2026, 9, 2, 12, 0)
        )
        aware = market_window.open_minutes_between(
            local(2026, 9, 2, 10, 0), local(2026, 9, 2, 12, 0)
        )
        assert naive == aware == 120
