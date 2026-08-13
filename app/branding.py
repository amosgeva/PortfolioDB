"""Operator display identity for the dashboard shell.

The rail footer shows a name + avatar initials. Resolved through
app/settings.py (Settings page → PORTFOLIODB_DISPLAY_NAME env var →
"Operator") so no personal identity is hardcoded in the repo.
"""

from __future__ import annotations

import settings


def display_name() -> str:
    return settings.get(
        "display_name", env="PORTFOLIODB_DISPLAY_NAME", default="Operator"
    )


def display_initials(name: str | None = None) -> str:
    n = (name if name is not None else display_name()).strip()
    parts = n.split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return n[:2].upper() if n else "OP"
