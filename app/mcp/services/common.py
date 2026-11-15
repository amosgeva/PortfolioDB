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

from psycopg2 import sql

import account_names


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


def window_start(window: str, *, now: datetime | None = None) -> datetime | None:
    """Window start as UTC datetime; None for 'all'.

    ``now`` anchors the window — pass the cutoff's instant so an as-of read
    looks back from the cutoff, not from the wall clock (audit X-10). None
    means the current instant.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
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


# ────────────────────────── accounts ──────────────────────────

# The label every listing uses for lots recorded without an account
# (list_accounts, allocation by account, per-account breakdowns). It is also
# accepted back as a filter and selects exactly those lots: it used to be
# compared with `account = '(none)'`, matched nothing and returned an empty
# portfolio with no error (audit X-8).
#
# The label means account IS NULL and nothing else, in every reader. It is
# reserved: the writers and the database refuse it as an account name, in any
# case or spacing (app/account_names.py, migration 007). Before that, an
# account literally named "(none)" could be stored, could not be selected,
# merged into this bucket in the listing, vanished from allocation by account,
# and lent its cash to this bucket (rescore NEW-1).
NONE_ACCOUNT = account_names.UNASSIGNED_LABEL


def is_none_account(account: str | None) -> bool:
    """True when ``account`` is the unassigned label (any case or spacing)."""
    return account is not None and account_names.is_reserved(account)


def add_account_filter(
    query: sql.Composable, params: list[Any], account: str | None
) -> tuple[sql.Composable, list[Any]]:
    """Append the account predicate for ``account`` to a lots/income query.

    None → no filter; ``NONE_ACCOUNT`` → ``account IS NULL``; anything else →
    ``account = %s`` with the value bound.
    """
    if account is None:
        return query, params
    if is_none_account(account):
        return query + sql.SQL(" AND account IS NULL"), params
    return query + sql.SQL(" AND account = %s"), [*params, account]


def account_matches(row_account: str | None, account: str | None) -> bool:
    """The same rule as add_account_filter, for rows already in memory."""
    if account is None:
        return True
    if is_none_account(account):
        return row_account is None
    return row_account == account


def account_label(account: str | None) -> str:
    return NONE_ACCOUNT if account is None else account


# ────────────────────────── methods ──────────────────────────

METHODS = ("fifo", "avg")


def normalize_method(method: str) -> str:
    """'fifo' or 'avg', from any casing of 'fifo' / 'avg' / 'avg_cost'.

    One rule for every tool: "FIFO" used to be rejected by one service and
    silently return no open lots from another (audit X-10).
    """
    m = (method or "").strip().lower()
    if m == "fifo":
        return "fifo"
    if m in ("avg", "avg_cost"):
        return "avg"
    raise ValueError(f"method must be 'fifo' or 'avg', got {method!r}")


# ────────────────────────── client-controlled sizes ──────────────────────────


def check_limit(name: str, value: int, maximum: int, *, minimum: int = 1) -> int:
    """Reject a size the caller controls when it is outside [minimum, maximum].

    Rejected, not clamped: a caller that asked for 0 rows or a million should
    learn that, rather than get a silently different answer. ``limit=0`` used
    to return every loser, since ``rows[-0:]`` is the whole list (audit M-5).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def check_symbols(
    symbols: list[str] | None, maximum: int, *, name: str = "symbols"
) -> list[str] | None:
    """Uppercase, de-duplicated symbol list, refused beyond ``maximum``."""
    if symbols is None:
        return None
    if len(symbols) > maximum:
        raise ValueError(f"{name} may list at most {maximum} symbols, got {len(symbols)}")
    out: list[str] = []
    for s in symbols:
        if not isinstance(s, str) or not s.strip() or len(s.strip()) > 32:
            raise ValueError(f"{name} entries must be ticker strings of 1-32 characters")
        sym = s.strip().upper()
        if sym not in out:
            out.append(sym)
    return out


# ────────────────────────── untrusted text ──────────────────────────

# Longest free-text value returned. Notes and vendor text have no reason to be
# longer, and a cap keeps one hostile row from dominating a response.
MAX_UNTRUSTED_CHARS = 2000

_CONTROL = {c: None for c in range(32) if c not in (9, 10)}
_CONTROL[127] = None


def untrusted(value: Any, *, max_chars: int = MAX_UNTRUSTED_CHARS) -> dict[str, Any] | None:
    """Wrap free text written by a person or a vendor as data, not prose.

    Lot and cash notes, collector error text and vendor fields go to the
    client model, and nothing marked them as foreign (audit M-6). Wrapped, a
    value reads ``{"untrusted_text": "...", "truncated": false}``, and the
    server instructions tell the model what that means. Control characters
    other than tab and newline are dropped; the text is capped.
    """
    if value is None:
        return None
    text = str(value).translate(_CONTROL)
    truncated = len(text) > max_chars
    return {"untrusted_text": text[:max_chars], "truncated": truncated}


def vendor_record(
    row: dict[str, Any] | None, *, text_fields: tuple[str, ...]
) -> dict[str, Any] | None:
    """A vendor (FD) row without its ``raw`` payload, free text wrapped.

    The full upstream JSON used to be returned wholesale under ``raw`` (audit
    M-6): unbounded, unreviewed vendor text. The extracted columns carry the
    data; ``text_fields`` are the ones that hold free text.
    """
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k != "raw"}
    for f in text_fields:
        if f in out and out[f] is not None:
            out[f] = untrusted(out[f])
    return out
