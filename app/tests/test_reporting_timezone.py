"""Timezone handling for the text reports.

`reporting_utils.IL_TZ` used to be `pytz.timezone(reporting_tz.tz_name())` —
a second, pytz-flavoured copy of a zone the codebase already resolved correctly
in `reporting_tz.tzinfo()`. The reports then had to remember to reach it through
`.localize()`, because a bare pytz zone object carries LMT until that call.

That is not a hypothetical hazard here. The identical pattern in
`import_csv_history.py` — a pytz zone used without `.localize()` — recorded
1,129 price snapshots at the -4:56 LMT offset before 1.1.4 caught it. These
three reports used pytz *correctly*, but correctness that depends on every
future edit remembering which idiom is safe is worth removing rather than
documenting.

What these tests pin:

  * `IL_TZ` is the same zone `reporting_tz` resolves, not a parallel object.
  * The offsets are real IANA offsets, not LMT — the check that would have
    caught the import bug.
  * The boundary instants the reports build land on the right UTC time in both
    standard and daylight time.
  * The one place `.replace()` and pytz's `.localize()` genuinely disagree, so
    that difference is recorded rather than discovered.
"""

import os
import sys
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reporting_tz
import reporting_utils

JERUSALEM = ZoneInfo("Asia/Jerusalem")
NEW_YORK = ZoneInfo("America/New_York")


def test_il_tz_is_the_zone_reporting_tz_resolves():
    """One source of truth for the reporting zone, not two."""
    assert reporting_utils.IL_TZ == reporting_tz.tzinfo()


def test_il_tz_is_a_zoneinfo():
    assert isinstance(reporting_utils.IL_TZ, ZoneInfo)


@pytest.mark.parametrize(
    "zone, moment, expected_offset_hours",
    [
        # Israel: IST +2 in winter, IDT +3 in summer.
        (JERUSALEM, datetime(2026, 1, 15, 16, 15), 2),
        (JERUSALEM, datetime(2026, 7, 15, 16, 15), 3),
        # New York, for a second zone with different transition dates.
        (NEW_YORK, datetime(2026, 1, 15, 16, 0), -5),
        (NEW_YORK, datetime(2026, 7, 15, 16, 0), -4),
    ],
)
def test_offsets_are_real_not_lmt(zone, moment, expected_offset_hours):
    """The check that would have caught the CSV import bug.

    LMT offsets are the giveaway: -4:56 for New York, +2:21 for Jerusalem. A
    real offset is a whole or half hour, and attaching a ZoneInfo directly is
    enough to get one — which is the entire reason for preferring it.
    """
    attached = moment.replace(tzinfo=zone)
    assert attached.utcoffset() == timedelta(hours=expected_offset_hours)
    assert attached.utcoffset().total_seconds() % 900 == 0, "not a quarter-hour offset — LMT leak"


@pytest.mark.parametrize(
    "zone, day, clock, expected_utc",
    [
        # report_portfolio_db's "eod" cutoff: 23:05 local.
        (JERUSALEM, "2026-01-15", time(23, 5), datetime(2026, 1, 15, 21, 5, tzinfo=timezone.utc)),
        (JERUSALEM, "2026-07-15", time(23, 5), datetime(2026, 7, 15, 20, 5, tzinfo=timezone.utc)),
        # report_weekly_db's week start, and report_portfolio_db's day start.
        (JERUSALEM, "2026-01-15", time(16, 15), datetime(2026, 1, 15, 14, 15, tzinfo=timezone.utc)),
        (JERUSALEM, "2026-07-15", time(16, 15), datetime(2026, 7, 15, 13, 15, tzinfo=timezone.utc)),
    ],
)
def test_report_boundary_instants(zone, day, clock, expected_utc):
    """The exact expression the reports build, asserted against absolute UTC."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    local = datetime.combine(d, clock).replace(tzinfo=zone)
    assert local.astimezone(timezone.utc) == expected_utc


def test_the_times_the_reports_use_are_never_ambiguous():
    """Why swapping .localize() for .replace() was safe here.

    The two differ only inside the hour a clock repeats when DST ends: pytz's
    .localize() defaults to standard time, .replace() to the first (daylight)
    occurrence. Both report clock times sit far from that window, so no report
    boundary can land in it. This asserts that rather than trusting it — if
    someone changes a report to a small-hours boundary, this is where it
    surfaces.
    """
    ambiguous_hours = set()
    d = datetime(2025, 1, 1).date()
    while d < datetime(2028, 1, 1).date():
        for hour in range(24):
            probe = datetime.combine(d, time(hour, 30))
            first = probe.replace(tzinfo=JERUSALEM, fold=0).utcoffset()
            second = probe.replace(tzinfo=JERUSALEM, fold=1).utcoffset()
            if first != second:
                ambiguous_hours.add(hour)
        d += timedelta(days=1)

    assert ambiguous_hours, "expected to find the DST fall-back hour somewhere in 3 years"
    for report_clock in (time(23, 5), time(16, 15)):
        assert report_clock.hour not in ambiguous_hours, (
            f"{report_clock} now falls in the ambiguous hour {sorted(ambiguous_hours)}; "
            "replace() and localize() disagree there by an hour"
        )


def test_utc_conversion_round_trips():
    local = datetime(2026, 3, 30, 16, 15).replace(tzinfo=JERUSALEM)
    assert local.astimezone(timezone.utc).astimezone(JERUSALEM) == local
