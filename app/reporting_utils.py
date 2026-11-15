"""Small helpers shared by the text reports (report_weekly_db,
report_portfolio_db) and anything else printing money/percent to a console.

Only the genuinely common core lives here — exec_report keeps its own
HTML-oriented formatters ('—' placeholders, signed variants, market-cap
compaction) because their None-handling is part of that report's contract.

Importing this module opens nothing (audit R-5). The reporting timezone is
looked up when it is asked for: `local_tz()` resolves it through
`reporting_tz` → `settings`, and that lookup may read the `settings` table.
It used to run at import, so a module documented as pure reached for a
database connection the moment a test or a tool imported it.
"""

from __future__ import annotations

import contextlib
import sys
from zoneinfo import ZoneInfo

import reporting_tz


def local_tz() -> ZoneInfo:
    """The reporting timezone, resolved now (Settings → PORTFOLIODB_TZ → UTC)."""
    return reporting_tz.tzinfo()


def __getattr__(name: str):
    # Historical name — the value follows PORTFOLIODB_TZ. Resolved on first
    # access rather than at import, for the reason in the module docstring.
    #
    # reporting_tz already resolves this correctly with zoneinfo; wrapping the
    # name in pytz built a second, more dangerous copy of the same zone. A bare
    # pytz zone carries LMT (+2:21 for Jerusalem) until .localize() replaces it,
    # so every caller had to remember that. The same pattern, un-remembered,
    # mis-stamped 1,129 price snapshots from the CSV importer before 1.1.4.
    if name == "IL_TZ":
        return local_tz()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
