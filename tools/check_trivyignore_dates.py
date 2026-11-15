"""Fail if any .trivyignore entry is undated or past its review-by date.

Every image scan that reads .trivyignore runs this first -- the weekly and
pull-request scan in security-scan.yml and both pre-publish scans in
publish.yml -- so an expired exception stops a publish as well as a scan, which
is what .trivyignore and SECURITY.md promise. Before, only security-scan.yml
checked, and a direct push or tag could publish on a waiver whose date had
passed (QA of PR #124, defect 2).

    python3 tools/check_trivyignore_dates.py [--today YYYY-MM-DD] < .trivyignore

The file comes in on stdin, so the script opens no path it is handed.
`--today` exists for the tests. Standard library only: publish.yml runs the
copy from the workflow's own commit, beside a checkout that may predate it.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timezone

_DATE = re.compile(r"^# (added|reviewed|review-by): (\d{4}-\d{2}-\d{2})\s*$")


def parse_entries(text: str) -> list[tuple[str, dict[str, date]]]:
    """(advisory id, {added, reviewed, review-by}) per entry, dates read from the
    comment lines directly above it. A blank line resets the pending dates."""
    out: list[tuple[str, dict[str, date]]] = []
    pending: dict[str, date] = {}
    for line in text.splitlines():
        m = _DATE.match(line)
        if m:
            pending[m.group(1)] = date.fromisoformat(m.group(2))
        elif line.strip() and not line.startswith("#"):
            out.append((line.strip(), pending))
            pending = {}
        elif not line.strip():
            pending = {}
    return out


def problems(entries: list[tuple[str, dict[str, date]]], today: date) -> list[str]:
    found = []
    for ident, dates in entries:
        due = dates.get("review-by")
        if due is None:
            found.append(f"{ident}: no `# review-by:` date")
        elif due < today:
            found.append(f"{ident}: review-by {due.isoformat()} has passed")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--today", type=date.fromisoformat, default=None)
    args = ap.parse_args(argv)
    today = args.today or datetime.now(timezone.utc).date()
    # No entries is a valid allowlist (the goal, even), so it passes.
    found = problems(parse_entries(sys.stdin.read()), today)
    for p in found:
        print(f"::error file=.trivyignore::{p}")
    if found:
        print("Re-check each reason; remove the entry, or move `reviewed` and `review-by` forward.")
        return 1
    print(f"every .trivyignore entry is within its review date ({today.isoformat()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
