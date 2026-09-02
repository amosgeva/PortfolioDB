"""Tests for the news-item age label on the dashboard.

`_format_age` has exactly one caller: the publication time on a news item. The
reader wants an ordering ("is this today's?"), not a duration, so the label is
read at a glance and never overstates its own precision. It previously ran
hours all the way to 48, rendering a two-day-old headline as "35.2h ago" -- a
unit nobody holds in their head, at a resolution (0.1h = 6 minutes) that means
nothing on a story that old.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dashboard.payload import _format_age


def ago(**kw) -> datetime:
    """A timestamp the given interval in the past, in UTC."""
    return datetime.now(timezone.utc) - timedelta(**kw)


class TestNearTerm:
    def test_none_is_an_em_dash(self):
        assert _format_age(None) == "—"

    def test_under_a_minute_is_just_now(self):
        assert _format_age(ago(seconds=5)) == "just now"

    def test_minutes(self):
        assert _format_age(ago(minutes=7)) == "7m ago"

    def test_naive_timestamps_are_read_as_utc(self):
        naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=7)
        assert _format_age(naive) == "7m ago"

    def test_a_future_timestamp_does_not_produce_a_negative(self):
        """Clock skew must not render "-3m ago"."""
        assert _format_age(datetime.now(timezone.utc) + timedelta(minutes=3)) == "just now"


class TestHours:
    @pytest.mark.parametrize("h", [1, 5, 23])
    def test_whole_hours_only(self, h):
        assert _format_age(ago(hours=h, minutes=30)) == f"{h}h ago"

    def test_no_decimal_point_anywhere_in_the_first_day(self):
        for h in range(1, 24):
            assert "." not in _format_age(ago(hours=h, minutes=17))


class TestPastADay:
    def test_the_case_that_prompted_this(self):
        """35.2h was the live value; hours are the wrong unit at that age."""
        assert _format_age(ago(hours=35, minutes=12)) == "1d ago"

    def test_hours_never_exceed_twenty_three(self):
        """The old branch ran hours to 48, so "47.9h ago" was a legal reading."""
        assert _format_age(ago(hours=47)) == "1d ago"

    def test_days_round_down_so_the_count_never_overstates_age(self):
        assert _format_age(ago(hours=47, minutes=59)) == "1d ago"

    def test_multi_day(self):
        assert _format_age(ago(days=3)) == "3d ago"

    def test_beyond_a_week(self):
        assert _format_age(ago(days=9, hours=5)) == "9d ago"

    def test_exactly_one_day(self):
        assert _format_age(ago(hours=24)) == "1d ago"

    def test_no_label_is_wide_enough_to_break_the_320px_feed_row(self):
        """The label '1d 11h ago' measured 72px in .fitem__time, which overflows 320px.

        The row cannot shrink (nowrap, min-width:auto), so the label's own
        length is the constraint. Every reachable label stays inside the
        widest string the old format already fit.
        """
        longest = max(
            (_format_age(ago(**kw)) for kw in (
                {"seconds": 5}, {"minutes": 59}, {"hours": 23}, {"hours": 35},
                {"days": 9}, {"days": 366},
            )),
            key=len,
        )
        assert len(longest) <= len("35.2h ago")


class TestBoundaries:
    @pytest.mark.parametrize(
        "kw, expected",
        [
            ({"seconds": 59}, "just now"),
            ({"seconds": 60}, "1m ago"),
            ({"minutes": 59}, "59m ago"),
            ({"minutes": 60}, "1h ago"),
        ],
    )
    def test_unit_changes_land_on_the_right_side(self, kw, expected):
        assert _format_age(ago(**kw)) == expected

    def test_the_hour_to_day_handover_never_says_24h(self):
        """int(secs//3600) at 23h59m must stay in hours, not round up to a day."""
        assert _format_age(ago(hours=23, minutes=59)) == "23h ago"
