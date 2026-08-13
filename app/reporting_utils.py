"""Small helpers shared by the text reports (report_weekly_db,
report_portfolio_db) and anything else printing money/percent to a console.

Only the genuinely common core lives here — exec_report keeps its own
HTML-oriented formatters ('—' placeholders, signed variants, market-cap
compaction) because their None-handling is part of that report's contract.
"""

from __future__ import annotations

import contextlib
import sys

import pytz

import reporting_tz

# Historical name — the value now follows PORTFOLIODB_TZ.
IL_TZ = pytz.timezone(reporting_tz.tz_name())


def utf8_stdout() -> None:
    """Make emoji/unicode survive PowerShell / Task Scheduler on Windows.

    Best-effort: a redirected/exotic stdout without reconfigure() is fine.
    """
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")


def money(x) -> str:
    return f"${float(x):,.2f}"


def pct(x) -> str:
    return f"{float(x):+.2f}%"
