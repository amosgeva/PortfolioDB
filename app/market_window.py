"""When the price collector is allowed to run — one definition, three readers.

Until now this lived *only* in run_snapshot.ps1 (a weekday 15:15–23:15
Asia/Jerusalem check), so it was Windows-only and invisible to Python. The
containerized scheduler runs the collector directly, so the guard has to live
here; the dashboard's freshness panel and the Settings page read the same
values instead of restating them.

Configured through app/settings.py (Settings page → env var → default):

    market_window_start   PORTFOLIODB_MARKET_START   default "13:30"
    market_window_end     PORTFOLIODB_MARKET_END     default "21:15"
    market_week           PORTFOLIODB_MARKET_WEEK    default "1-5" (Mon-Fri)

Times are HH:MM in the *reporting* timezone (PORTFOLIODB_TZ, default UTC), so
the defaults describe US regular hours plus a post-close tail in UTC. An
operator in Israel reading Asia/Jerusalem sets 15:15–23:15 and gets exactly
the old behaviour.

The window is inclusive at both ends and may wrap past midnight (start > end),
which is what an Asia/Tokyo operator watching US markets needs.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import reporting_tz
import settings

DEFAULT_START = "13:30"
DEFAULT_END = "21:15"
DEFAULT_WEEK = "1-5"


def _parse_hhmm(raw: str | None, fallback: str) -> time:
    """'15:15' → time(15, 15). Falls back rather than raising: a typo in a
    setting must not stop the collector from ever running again."""
    for candidate in (raw, fallback):
        if not candidate:
            continue
        try:
            hh, _, mm = candidate.strip().partition(":")
            return time(int(hh), int(mm or 0))
        except (TypeError, ValueError):
            continue
    return time(0, 0)


def _parse_week(raw: str | None) -> set[int]:
    """'1-5' or '1,2,5' → {0,1,2,3,4}-style Python weekdays (Mon=0).

    Uses cron's convention on the way in (1=Monday … 7=Sunday, 0 also Sunday)
    because that is what the crontab next to it uses.
    """
    text = (raw or DEFAULT_WEEK).strip()
    days: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                lo, _, hi = part.partition("-")
                rng = range(int(lo), int(hi) + 1)
            else:
                rng = [int(part)]
            for n in rng:
                days.add((n - 1) % 7 if n else 6)  # cron 0/7 = Sunday = py 6
        except ValueError:
            continue
    return days or {0, 1, 2, 3, 4}


def window() -> tuple[time, time, set[int]]:
    """(start, end, weekdays) as configured."""
    start = _parse_hhmm(
        settings.get("market_window_start", env="PORTFOLIODB_MARKET_START"), DEFAULT_START
    )
    end = _parse_hhmm(
        settings.get("market_window_end", env="PORTFOLIODB_MARKET_END"), DEFAULT_END
    )
    week = _parse_week(settings.get("market_week", env="PORTFOLIODB_MARKET_WEEK"))
    return start, end, week


def is_open(now: datetime | None = None) -> bool:
    """Whether the collector should run at `now` (default: this instant).

    `now` may be naive — it is then read as reporting-local, matching how the
    rest of the app treats bare timestamps.
    """
    tz = reporting_tz.tzinfo()
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    start, end, week = window()
    if now.weekday() not in week:
        return False

    minutes = now.hour * 60 + now.minute
    lo = start.hour * 60 + start.minute
    hi = end.hour * 60 + end.minute
    if lo <= hi:
        return lo <= minutes <= hi
    # Wrapped window (e.g. 22:00–04:00): open at either end of midnight.
    return minutes >= lo or minutes <= hi


# A missed tick or two is jitter, not an outage: the collector ticks every five
# minutes and a run can slip. Half an hour of window time with nothing recorded
# is past anything scheduling explains.
GAP_MINUTES = 30


def open_minutes_between(start: datetime, end: datetime) -> int:
    """Minutes between two instants that fall INSIDE the collector window.

    This is the difference between "the market was shut" and "we were not
    looking". Wall-clock length cannot tell those apart -- the ordinary gap
    between a Friday close and a Monday open is about 64 hours -- so a gap is
    measured by how much of it the collector was supposed to be awake for.

    Both ends may be naive, and are then read as reporting-local like every
    other bare timestamp in the app.
    """
    tz = reporting_tz.tzinfo()

    def _local(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tz)
        return dt.astimezone(tz)

    a, b = _local(start), _local(end)
    if b <= a:
        return 0

    start_t, end_t, week = window()
    lo = start_t.hour * 60 + start_t.minute
    hi = end_t.hour * 60 + end_t.minute
    # A window that wraps past midnight is two spans on each calendar day.
    spans = [(lo, hi)] if lo <= hi else [(0, hi), (lo, 24 * 60)]

    total = 0.0
    day = a.date()
    last = b.date()
    while day <= last:
        if day.weekday() in week:
            midnight = datetime.combine(day, time(0, 0)).replace(tzinfo=tz)
            for lo_m, hi_m in spans:
                s_dt = midnight + timedelta(minutes=lo_m)
                e_dt = midnight + timedelta(minutes=hi_m)
                overlap_lo = max(s_dt, a)
                overlap_hi = min(e_dt, b)
                if overlap_hi > overlap_lo:
                    total += (overlap_hi - overlap_lo).total_seconds() / 60.0
        day += timedelta(days=1)
    return int(round(total))

def describe() -> str:
    """Human-readable summary for logs and the Settings page."""
    start, end, week = window()
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    days = ", ".join(names[d] for d in sorted(week))
    return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} {reporting_tz.tz_name()} on {days}"
