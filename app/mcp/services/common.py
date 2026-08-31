"""Shared helpers for MCP services.

Consolidates three things that had drifted into near-identical copies:
row serialization (positions._clean_record / fundamentals._scrub), the
window-name → start-datetime mapping (prices._window_start /
analytics._window_since), and the FD section → table registry
(fundamentals._FD_TABLES / the inline list in health.fd_freshness).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any


def is_nan(value: Any) -> bool:
    """True only for a NaN float (or Decimal NaN).

    NaN is the one value that compares unequal to itself, so this used to be
    spelled `v != v` inline at six call sites. That reads like a typo — and a
    static analyser agrees, flagging it as an accidental self-comparison — so
    the idiom lives here once, under a name that says what it means.

    Anything else, None included, is not NaN: callers rely on that to tell a
    missing price apart from an unquotable one.
    """
    return isinstance(value, (float, Decimal)) and math.isnan(float(value))

# FD enrichment tables, keyed by section name. The values double as the
# hardcoded identifier whitelist for freshness queries — never interpolate
# non-whitelisted names into SQL.
FD_TABLES = {
    "facts": "fd_company_facts",
    "metrics": "fd_financial_metrics",
    "statements": "fd_financial_statements",
    "earnings": "fd_earnings",
    "filings": "fd_filings",
    "insiders": "fd_insider_trades",
    "ownership": "fd_institutional_ownership",
    "news": "fd_news",
}


def window_start(window: str) -> datetime | None:
    """Window start as UTC datetime; None for 'all'."""
    now = datetime.now(timezone.utc)
    if window == "1d":
        return now - timedelta(days=1)
    if window == "1w":
        return now - timedelta(days=7)
    if window == "1m":
        return now - timedelta(days=30)
    if window == "3m":
        return now - timedelta(days=90)
    if window == "6m":
        return now - timedelta(days=180)
    if window == "1y":
        return now - timedelta(days=365)
    if window == "ytd":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc)
    if window == "all":
        return None
    raise ValueError(f"Unknown window {window!r}")


def clean_record(r: dict[str, Any] | None) -> dict[str, Any] | None:
    """Coerce a DB/DataFrame row to JSON-safe plain types.

    NaN → None, datetime/date (incl. pd.Timestamp) → ISO string,
    Decimal → float, JSONB dict/list pass through, numpy scalars unwrap
    via .item(). None rows pass through as None.
    """
    if r is None:
        return None
    out: dict[str, Any] = {}
    for k, v in r.items():
        if v is None:
            out[k] = None
        elif is_nan(v):
            out[k] = None
        elif isinstance(v, (datetime, date)):  # pd.Timestamp subclasses datetime
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (dict, list)):
            out[k] = v
        else:
            try:
                out[k] = v.item() if hasattr(v, "item") else v
            except Exception:
                out[k] = v
    return out
