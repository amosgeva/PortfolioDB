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
