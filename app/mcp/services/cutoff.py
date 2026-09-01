"""One cutoff instant, resolved once and threaded through every read.

The problem this solves: assembling a portfolio review takes ~10 MCP calls, and
each one independently calls ``datetime.now()`` and independently re-reads "the
latest price". The snapshot collector writes every five minutes, so a review
that straddles a run silently mixes two valuations — market value from the new
snapshot, daily change from the old one — and the totals stop reconciling. The
inconsistency is invisible: every individual response looks fine.

A ``Cutoff`` pins the instant *and* records which observation was actually used
for each symbol and account, so a response can state its own provenance rather
than implying "now" and hoping.

Resolution rules:

* ``None`` → this instant.
* a ``date`` → the **end** of that day in the reporting timezone, so "as of
  2026-08-01" includes everything that happened on the 1st rather than nothing.
* a ``datetime`` → itself; naive input is read as reporting-timezone local.

Everything downstream filters on the resolved ``ts``: lots by ``trade_date <=
cutoff.trade_date``, prices and cash by ``ts <= cutoff.ts``. Passing a Cutoff is
what makes a set of calls agree; omitting it preserves the old
independently-latest behaviour, so existing callers are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from app.mcp.deps import get_conn

# Importing deps (above) puts app/ on sys.path, so the bare settings module
# resolves here just like corporate_actions does in the sibling services.
import settings as _settings
import version

# Reporting timezone for the whole application — the window the snapshot
# collector runs in and the day boundary every daily series uses. Resolved
# once at import through app/settings.py (Settings page → PORTFOLIODB_TZ env
# → default), so a change from the Settings UI applies on server restart.
# The default is UTC; app/reporting_tz.py::DEFAULT_TZ holds the same default
# for the bare-module side — keep the two in sync.
REPORTING_TZ = _settings.get("reporting_tz", env="PORTFOLIODB_TZ", default="UTC")
try:
    LOCAL_TZ = ZoneInfo(REPORTING_TZ)
except Exception:  # typo'd setting — fall back rather than break every import
    REPORTING_TZ = "UTC"
    LOCAL_TZ = ZoneInfo(REPORTING_TZ)

# Every instrument is USD (verified 2026-08-12: 35/35). Stated explicitly so a
# response declares its currency rather than leaving it to be assumed, and so
# the day a non-USD instrument appears there is something to check against.
REPORTING_CURRENCY = "USD"

# Tracks the `-- PortfolioDB schema vX.Y` header in sql/schema.sql.
SCHEMA_VERSION = "0.3"

# How long a snapshot run may be in flight before we stop deferring to it.
#
# The collector stamps every row of a run with the run's *start* time, then
# commits them one symbol at a time as each yfinance call returns — measured at
# 7-12 seconds for 12 symbols. So for several seconds there are rows visible
# with ts inside the cutoff and rows not yet committed carrying the same ts. A
# cutoff landing in that window reads a partially-written run, and two services
# resolving microseconds apart legitimately disagree.
#
# Deferring to `snapshot_runs` fixes it: while a run is in flight the cutoff
# steps back behind it. The grace period matches the collector's own reaper
# (snapshot_prices._reap_stale_runs) so a run whose process died cannot pin the
# cutoff in the past indefinitely.
INFLIGHT_GRACE_MINUTES = 10


@dataclass(frozen=True)
class Cutoff:
    """A pinned observation point.

    Attributes:
        ts: the cutoff instant, tz-aware UTC. Nothing observed after it counts.
        tz: reporting timezone name, for rendering and day boundaries.
        trade_date: lots with ``trade_date <= this`` are in scope. Derived from
            ``ts`` in the reporting timezone, not UTC — a trade late on the 5th
            reporting-local time belongs to the 5th.
        price_ts_by_symbol: the snapshot actually used per symbol. Sparse: a
            symbol with no snapshot at or before the cutoff is absent, which is
            a fact worth reporting rather than a zero to substitute.
        cash_ts_by_account: likewise for cash balances.
        coverage_start: first price observation in the database, ever.
        coverage_end: last price observation at or before the cutoff. None when
            there are no observations at all.
    """

    ts: datetime
    tz: str = REPORTING_TZ
    trade_date: date = field(default=None)  # type: ignore[assignment]
    price_ts_by_symbol: dict[str, datetime] = field(default_factory=dict)
    cash_ts_by_account: dict[str, datetime] = field(default_factory=dict)
    coverage_start: date | None = None
    coverage_end: date | None = None
    requested_ts: datetime | None = None
    inflight_run_id: int | None = None

    @property
    def was_pulled_back(self) -> bool:
        """True when a snapshot run in flight forced the cutoff earlier than
        asked. The data is consistent either way; this says it is also slightly
        older than the caller requested."""
        return self.requested_ts is not None and self.ts < self.requested_ts

    @property
    def local(self) -> datetime:
        """The cutoff rendered in the reporting timezone."""
        return self.ts.astimezone(ZoneInfo(self.tz))

    def price_ts(self, symbol: str) -> datetime | None:
        return self.price_ts_by_symbol.get(symbol.upper())

    def is_stale_for(self, symbol: str, *, max_age_hours: float) -> bool | None:
        """Whether this symbol's observation is older than max_age_hours at the
        cutoff. None when there is no observation to judge."""
        seen = self.price_ts(symbol)
        if seen is None:
            return None
        return (self.ts - seen).total_seconds() / 3600.0 > max_age_hours


def to_instant(as_of: datetime | date | None) -> datetime:
    """Normalize a caller's as_of into a tz-aware UTC instant.

    Pure — no database access — so callers can pin a cutoff without a round
    trip when they only need the instant.
    """
    if as_of is None:
        return datetime.now(timezone.utc)
    if isinstance(as_of, datetime):
        aware = as_of if as_of.tzinfo else as_of.replace(tzinfo=LOCAL_TZ)
        return aware.astimezone(timezone.utc)
    # A bare date means the whole of that day, so pin to its final microsecond
    # in local time. Pinning to midnight instead would silently exclude every
    # observation on the requested day.
    return datetime.combine(as_of, time.max, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def resolve(as_of: datetime | date | None = None) -> Cutoff:
    """Resolve a cutoff, recording which observations it actually lands on.

    Four small queries. Call once per request and pass the result down; calling
    it per service would reintroduce exactly the drift it exists to prevent.
    """
    requested = to_instant(as_of)
    ts = requested
    inflight_run_id: int | None = None

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Step behind any snapshot run still writing. Its rows all carry
            # the run's start time, so without this the cutoff can include a
            # run that is only half committed — see INFLIGHT_GRACE_MINUTES.
            cur.execute(
                """
                SELECT id, ts_start
                FROM snapshot_runs
                WHERE status = 'running'
                  AND ts_start <= %s
                  AND ts_start > %s - make_interval(mins => %s)
                ORDER BY ts_start
                LIMIT 1
                """,
                (requested, requested, INFLIGHT_GRACE_MINUTES),
            )
            row = cur.fetchone()
            if row:
                inflight_run_id = int(row[0])
                ts = row[1] - timedelta(microseconds=1)

            cur.execute(
                """
                SELECT DISTINCT ON (symbol) symbol, ts
                FROM price_snapshots
                WHERE ts <= %s
                ORDER BY symbol, ts DESC
                """,
                (ts,),
            )
            price_ts = {sym: t for sym, t in cur.fetchall()}

            cur.execute(
                """
                SELECT DISTINCT ON (account) account, ts
                FROM cash_snapshots
                WHERE ts <= %s
                ORDER BY account, ts DESC
                """,
                (ts,),
            )
            cash_ts = {acct: t for acct, t in cur.fetchall()}

            cur.execute("SELECT MIN(ts) FROM price_snapshots")
            row = cur.fetchone()
            first_ts = row[0] if row else None

    coverage_end = max(price_ts.values()) if price_ts else None

    return Cutoff(
        ts=ts,
        tz=REPORTING_TZ,
        trade_date=ts.astimezone(LOCAL_TZ).date(),
        price_ts_by_symbol=price_ts,
        cash_ts_by_account=cash_ts,
        coverage_start=first_ts.astimezone(LOCAL_TZ).date() if first_ts else None,
        coverage_end=coverage_end.astimezone(LOCAL_TZ).date() if coverage_end else None,
        requested_ts=requested,
        inflight_run_id=inflight_run_id,
    )


def meta(cutoff: Cutoff, *, method: str | None = None, **extra: Any) -> dict[str, Any]:
    """Response-level provenance block.

    Deliberately response-level rather than per-field: attaching eleven
    provenance attributes to each of ~30 fields would inflate the payload
    10-20x, against the same requirement that responses stay compact. Fields
    whose provenance genuinely differs from the response's — a derived ratio, a
    value that is null for a specific reason — carry their own markers instead.
    """
    out: dict[str, Any] = {
        "as_of": cutoff.ts.isoformat(),
        "as_of_local": cutoff.local.isoformat(),
        "timezone": cutoff.tz,
        "reporting_currency": REPORTING_CURRENCY,
        "coverage_start": cutoff.coverage_start.isoformat() if cutoff.coverage_start else None,
        "coverage_end": cutoff.coverage_end.isoformat() if cutoff.coverage_end else None,
        "schema_version": SCHEMA_VERSION,
        "app_version": app_version(),
    }
    if cutoff.was_pulled_back:
        # Report it rather than quietly serving slightly older data: the caller
        # asked for one instant and got another.
        out["as_of_requested"] = (
            cutoff.requested_ts.isoformat() if cutoff.requested_ts else None
        )
        out["as_of_adjusted_reason"] = "snapshot_run_in_flight"
        out["inflight_run_id"] = cutoff.inflight_run_id
    if method is not None:
        out["cost_basis_method"] = method
    out.update(extra)
    return out


@lru_cache(maxsize=1)
def app_version() -> str:
    """Build identifier for the provenance block.

    The logic moved to app/version.py so the dashboard can read it without
    importing this module, which pulls in app.mcp.deps. Kept here as a cached
    wrapper: the answer cannot change within a server process, and callers
    (and tests, via .cache_clear()) already depend on that.
    """
    return version.build_stamp()
