"""Reporting timezone for the whole app — the calendar that turns snapshot
timestamps into "days" (daily series, EOD picks, snapshot dedupe).

Resolved through app/settings.py: Settings page → PORTFOLIODB_TZ env var →
default. The default is UTC — the only defensible default for a tool other
people install; every deployment that cares sets its own zone (this repo's
own instance pins Asia/Jerusalem in .env).

Note: several modules capture this in module-level constants at import, and
the MCP side reads it once at server start (cutoff.py::REPORTING_TZ — keep
the two defaults in sync), so a timezone change applies to those components
on their next restart. The Settings UI says so next to the field.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, tzinfo as _tzinfo_base
from zoneinfo import ZoneInfo

import settings

DEFAULT_TZ = "UTC"


def tz_name() -> str:
    """IANA name of the reporting timezone, e.g. 'Asia/Jerusalem'."""
    name = settings.get("reporting_tz", env="PORTFOLIODB_TZ", default=DEFAULT_TZ)
    try:
        ZoneInfo(name)  # reject a typo'd name rather than failing mid-query
    except Exception:
        return DEFAULT_TZ
    return name


def tzinfo() -> ZoneInfo:
    """The reporting timezone as a tzinfo (ZoneInfo instances are cached)."""
    return ZoneInfo(tz_name())


class _ReportingZone(_tzinfo_base):
    """A tzinfo that resolves the reporting timezone on first *use*.

    Several modules hold the zone in a module-level constant. Written as
    ``LOCAL_TZ = reporting_tz.tzinfo()`` that constant read the settings table
    at import, so importing a "pure" module opened a database connection
    (audit R-5) — and in a process whose settings source was registered after
    the import, it silently pinned the environment's fallback instead of the
    table's value (the MCP's half of X-4).

    ``lazy_tzinfo()`` returns this instead. It behaves as the real zone for
    every datetime operation; the zone is looked up the first time one runs.
    Once the lookup came from the settings table it is pinned, keeping the
    documented "a timezone change applies on restart" semantics. A lookup that
    could not read the table (database down) is not pinned, so a process that
    started before its database recovers on a later use.
    """

    def __init__(self) -> None:
        self._zone: ZoneInfo | None = None
        self._lock = threading.Lock()

    def resolve(self) -> ZoneInfo:
        zone = self._zone
        if zone is not None:
            return zone
        with self._lock:
            if self._zone is not None:
                return self._zone
            zone = tzinfo()
            if settings.db_available():
                self._zone = zone
            return zone

    @property
    def key(self) -> str:
        return self.resolve().key

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return self.resolve().utcoffset(dt)

    def dst(self, dt: datetime | None) -> timedelta | None:
        return self.resolve().dst(dt)

    def tzname(self, dt: datetime | None) -> str | None:
        return self.resolve().tzname(dt)

    def fromutc(self, dt: datetime) -> datetime:
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        zone = self.resolve()
        return zone.fromutc(dt.replace(tzinfo=zone)).replace(tzinfo=self)

    def __repr__(self) -> str:
        return f"<reporting timezone {self._zone.key if self._zone else '(unresolved)'}>"

    def __str__(self) -> str:
        return self.key


_LAZY = _ReportingZone()


def lazy_tzinfo() -> _ReportingZone:
    """The reporting timezone, resolved on first use rather than now.

    Use this for a module-level constant; use ``tzinfo()`` inside a function.
    """
    return _LAZY
