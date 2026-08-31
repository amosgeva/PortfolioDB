"""Snapshot timestamp conversion for the CSV importer.

These exist because of a live data bug, not a style rule.

`parse_snapshot_ts` built its dateutil `tzinfos` map out of a bare
`pytz.timezone("America/New_York")`. A pytz zone object carries **LMT** — the
earliest offset in the database, -4:56 for New York — until `.localize()`
replaces it with a real one, and a value handed to dateutil never gets that
call. So every row carrying an explicit EST/EDT token was converted at -4:56:

    16:00 EST  ->  20:56 UTC   (4 minutes early;  21:00 is correct)
    15:59 EDT  ->  20:55 UTC   (56 minutes late;  19:59 is correct)

Rows with no timezone token went through `.localize()` and were correct, which
is why this survived: the importer looked right whenever it was spot-checked
against a file that omitted the token.

Anything already imported from a CSV whose Time column carried EST or EDT has a
`price_snapshots.ts` off by one of those two amounts.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from import_csv_history import parse_snapshot_ts

UTC = timezone.utc


@pytest.mark.parametrize(
    "date_str, time_str, expected",
    [
        # Explicit tokens: the offset is stated in the file, so it is not a
        # matter of interpretation. These are the two the LMT bug got wrong.
        ("2026/02/20", "16:00 EST", datetime(2026, 2, 20, 21, 0, tzinfo=UTC)),
        ("2026/07/15", "15:59 EDT", datetime(2026, 7, 15, 19, 59, tzinfo=UTC)),
        ("2026/12/31", "09:30 EST", datetime(2026, 12, 31, 14, 30, tzinfo=UTC)),
        ("2026/06/01", "09:30 EDT", datetime(2026, 6, 1, 13, 30, tzinfo=UTC)),
        # No token: fall back to New York and let the date decide EST vs EDT.
        ("2026/02/20", "16:00", datetime(2026, 2, 20, 21, 0, tzinfo=UTC)),
        ("2026/07/15", "16:00", datetime(2026, 7, 15, 20, 0, tzinfo=UTC)),
        ("2026/01/01", "00:00", datetime(2026, 1, 1, 5, 0, tzinfo=UTC)),
    ],
)
def test_snapshot_timestamps_convert_to_the_right_utc(date_str, time_str, expected):
    assert parse_snapshot_ts(date_str, time_str) == expected


@pytest.mark.parametrize(
    "date_str, time_str, wrong",
    [
        ("2026/02/20", "16:00 EST", datetime(2026, 2, 20, 20, 56, tzinfo=UTC)),
        ("2026/07/15", "15:59 EDT", datetime(2026, 7, 15, 20, 55, tzinfo=UTC)),
    ],
)
def test_the_lmt_offset_does_not_come_back(date_str, time_str, wrong):
    """Named for the failure, so a regression says what broke rather than
    just which numbers stopped matching."""
    assert parse_snapshot_ts(date_str, time_str) != wrong


def test_offsets_are_whole_hours():
    """-4:56 is not a real US market offset; any LMT leak shows up here.

    A stricter net than the fixed cases above: it catches an LMT regression at
    any date, not only the two that were reported.
    """
    for month, day in [(1, 15), (3, 20), (6, 1), (9, 10), (12, 31)]:
        for token in ["EST", "EDT", ""]:
            ts = parse_snapshot_ts(f"2026/{month:02d}/{day:02d}", f"12:00 {token}".strip())
            assert ts.minute == 0, f"{month}-{day} {token!r} produced {ts.isoformat()}"


def test_explicit_token_wins_over_the_date():
    """The file said EST; honour it even in July.

    A broker exporting a standard-time token in summer is stating an offset,
    not asking to have one re-derived. Deriving it from the date instead would
    silently shift the row by an hour.
    """
    assert parse_snapshot_ts("2026/07/15", "12:00 EST") == datetime(
        2026, 7, 15, 17, 0, tzinfo=UTC
    )


def test_result_is_always_utc():
    ts = parse_snapshot_ts("2026/05/05", "10:00 EDT")
    assert ts.tzinfo is not None
    assert ts.utcoffset().total_seconds() == 0
