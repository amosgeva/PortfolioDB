"""Price service — latest snapshots, history, change-over-window, top movers.

All queries hit `price_snapshots` directly; no aggregation tables exist by
design (everything is recomputed from append-only snapshots on read). The
"top movers" and "portfolio value history" tools reuse the dashboard's
window functions to stay aligned with what the user already sees.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.mcp.deps import get_conn
from app.mcp.services import common
from app.mcp.services.cutoff import REPORTING_TZ

# Import deps first (above): it puts app/ on sys.path so these top-level modules
# resolve however the tests are invoked.
import corporate_actions
import holdings as holdings_module

# Windows accepted by get_price_change / get_top_movers.
VALID_WINDOWS = ("1d", "1w", "1m", "3m", "6m", "ytd", "1y", "all")

# How historical series value the portfolio.
HOLDINGS_BASES = ("historical", "current_constant")


# ────────────────────────── latest snapshot ──────────────────────────


def latest_price(
    symbol: str, *, as_of_ts: datetime | None = None
) -> dict[str, Any] | None:
    """Most recent snapshot for one symbol at or before ``as_of_ts``.

    as_of_ts=None means "now", i.e. genuinely the latest row.
    """
    sym = symbol.upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, ts, last_price, bid, ask, source
                FROM price_snapshots
                WHERE symbol = %s
                  AND (%s::timestamptz IS NULL OR ts <= %s)
                ORDER BY ts DESC
                LIMIT 1
                """,
                (sym, as_of_ts, as_of_ts),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            r = dict(zip(cols, row))
    return _price_record(r)


def latest_prices(
    symbols: list[str] | None = None, *, as_of_ts: datetime | None = None
) -> list[dict[str, Any]]:
    """Latest snapshot per symbol at or before ``as_of_ts``.

    symbols=None = every symbol that has a snapshot in range. as_of_ts=None
    keeps the previous behaviour of reading the genuinely-latest row, so
    callers that have not been given a cutoff are unaffected.
    """
    syms = [s.upper() for s in symbols] if symbols else None
    with get_conn() as conn:
        with conn.cursor() as cur:
            # DISTINCT ON rather than a join against MAX(ts): one scan, and the
            # optional bound drops in without duplicating the whole query.
            cur.execute(
                """
                SELECT DISTINCT ON (symbol)
                       symbol, ts, last_price, bid, ask, source
                FROM price_snapshots
                WHERE (%s::text[] IS NULL OR symbol = ANY(%s))
                  AND (%s::timestamptz IS NULL OR ts <= %s)
                ORDER BY symbol, ts DESC
                """,
                (syms, syms, as_of_ts, as_of_ts),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return [_price_record(r) for r in rows]


def latest_price_map(*, as_of_ts: datetime | None = None) -> dict[str, float]:
    """symbol -> last_price (float) for joining into positions."""
    return {
        r["symbol"]: float(r["last_price"])
        for r in latest_prices(as_of_ts=as_of_ts)
        if r["last_price"] is not None
    }


def latest_price_map_with_ts(
    *, as_of_ts: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """symbol -> {last_price, ts} — used by the positions service."""
    out: dict[str, dict[str, Any]] = {}
    for r in latest_prices(as_of_ts=as_of_ts):
        out[r["symbol"]] = {"last_price": r["last_price"], "ts": r["ts"]}
    return out


# ────────────────────────── second-latest + prev-EOD ──────────────────────────


def second_latest_price_map(
    *, as_of_ts: datetime | None = None
) -> dict[str, float]:
    """The price per symbol from the snapshot before the one at the cutoff.

    Mirrors streamlit_app.get_second_latest_snapshot_map. Split-adjusted: on a
    split's ex-date the comparison price is from before the rebase, which would
    otherwise register the whole split ratio as a price move.
    """
    with get_conn() as conn:
        actions = corporate_actions.fetch_actions(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH ranked AS (
                  SELECT symbol, ts, last_price,
                         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
                  FROM price_snapshots
                  WHERE (%s::timestamptz IS NULL OR ts <= %s)
                )
                SELECT symbol, ts, last_price
                FROM ranked
                WHERE rn = 2
                """,
                (as_of_ts, as_of_ts),
            )
            rows = cur.fetchall()
    return _adjusted_price_map(rows, actions)


def prev_day_eod_price_map(
    *, as_of_ts: datetime | None = None
) -> dict[str, float]:
    """EOD price per symbol from the day before the cutoff's day (reporting timezone).

    Mirrors streamlit_app.get_prev_snapshot_map. Split-adjusted for the same
    reason as second_latest_price_map. With a cutoff, "today" is the cutoff's
    local day rather than the wall clock's — so an as-of report compares
    against the right prior day instead of against yesterday-from-now.
    """
    with get_conn() as conn:
        actions = corporate_actions.fetch_actions(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH ref AS (
                  SELECT (date_trunc(
                            'day',
                            COALESCE(%s::timestamptz, now()) AT TIME ZONE %s
                          )) AT TIME ZONE %s AS day_start
                ),
                prev AS (
                  SELECT symbol, price_snapshots.ts, last_price,
                         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_snapshots.ts DESC) AS rn
                  FROM price_snapshots, ref
                  WHERE price_snapshots.ts < ref.day_start
                )
                SELECT symbol, ts, last_price
                FROM prev
                WHERE rn = 1
                """,
                (as_of_ts, REPORTING_TZ, REPORTING_TZ),
            )
            rows = cur.fetchall()
    return _adjusted_price_map(rows, actions)


def _adjusted_price_map(
    rows: list[tuple[str, Any, Any]],
    actions: list[corporate_actions.CorporateAction],
) -> dict[str, float]:
    """(symbol, ts, price) rows → {symbol: split-adjusted price}."""
    points = corporate_actions.adjust_price_points(
        ((ts, sym, float(price)) for sym, ts, price in rows if price is not None),
        actions,
    )
    return {sym: price for _ts, sym, price in points}


# ────────────────────────── history ──────────────────────────


def price_history(
    symbol: str,
    since: date,
    until: date | None = None,
    *,
    resample: str = "raw",
) -> list[dict[str, Any]]:
    """Split-adjusted snapshot history for one symbol over a date window.

    Prices before a recorded split are restated into today's units, so the
    series is continuous and directly comparable across the ex-date. Bid and
    ask are adjusted with the same factor.

    Args:
        resample: 'raw' = every snapshot row; 'daily' = last snapshot per day.
    """
    if resample not in ("raw", "daily"):
        raise ValueError(f"Unknown resample '{resample}'. Use 'raw' or 'daily'.")
    sym = symbol.upper()
    until = until or date.today()
    with get_conn() as conn:
        actions = [
            a for a in corporate_actions.fetch_actions(conn) if a.symbol.upper() == sym
        ]
        with conn.cursor() as cur:
            if resample == "daily":
                cur.execute(
                    """
                    SELECT DISTINCT ON (date_trunc('day', ts AT TIME ZONE %s))
                        date_trunc('day', ts AT TIME ZONE %s) AS day_local,
                        ts, last_price, bid, ask
                    FROM price_snapshots
                    WHERE symbol = %s
                      AND ts >= %s AND ts <= %s
                    ORDER BY date_trunc('day', ts AT TIME ZONE %s),
                             ts DESC
                    """,
                    (REPORTING_TZ, REPORTING_TZ, sym, since, until + timedelta(days=1), REPORTING_TZ),
                )
            else:  # resample == "raw"
                cur.execute(
                    """
                    SELECT ts, last_price, bid, ask
                    FROM price_snapshots
                    WHERE symbol = %s
                      AND ts >= %s AND ts <= %s
                    ORDER BY ts
                    """,
                    (sym, since, until + timedelta(days=1)),
                )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    out: list[dict[str, Any]] = []
    for r in rows:
        ts = r.get("ts")
        factor = float(corporate_actions.price_factor(actions, ts)) if ts else 1.0

        def scaled(key: str) -> float | None:
            v = r.get(key)
            return float(v) / factor if v is not None else None

        out.append({
            "ts": ts.isoformat() if ts else None,
            "last_price": scaled("last_price"),
            "bid": scaled("bid"),
            "ask": scaled("ask"),
        })
    return out


# ────────────────────────── change over window ──────────────────────────


def price_change(symbol: str, window: str = "1d") -> dict[str, Any] | None:
    """Split-adjusted price change for a symbol over a named window.

    Returns the first snapshot at-or-after the window start and the latest
    snapshot, with absolute and percent delta. None if either endpoint is
    missing (e.g. window predates the symbol's snapshot history).

    Both endpoints are restated into today's units, so a window spanning a
    split reports the real move rather than the split ratio.
    """
    if window not in VALID_WINDOWS:
        raise ValueError(f"window must be one of {VALID_WINDOWS}, got {window!r}")
    sym = symbol.upper()
    since = _window_start(window)

    with get_conn() as conn:
        actions = [
            a for a in corporate_actions.fetch_actions(conn) if a.symbol.upper() == sym
        ]
        with conn.cursor() as cur:
            # Latest snapshot
            cur.execute(
                "SELECT ts, last_price FROM price_snapshots "
                "WHERE symbol = %s ORDER BY ts DESC LIMIT 1",
                (sym,),
            )
            latest = cur.fetchone()
            if not latest:
                return None

            if since is None:
                # 'all' — use the very first snapshot
                cur.execute(
                    "SELECT ts, last_price FROM price_snapshots "
                    "WHERE symbol = %s ORDER BY ts ASC LIMIT 1",
                    (sym,),
                )
            else:
                cur.execute(
                    "SELECT ts, last_price FROM price_snapshots "
                    "WHERE symbol = %s AND ts >= %s ORDER BY ts ASC LIMIT 1",
                    (sym, since),
                )
            base = cur.fetchone()
            if not base:
                return None

    from_ts = base[0]
    to_ts = latest[0]
    from_price = float(base[1]) / float(corporate_actions.price_factor(actions, from_ts))
    to_price = float(latest[1]) / float(corporate_actions.price_factor(actions, to_ts))
    if from_price == 0:
        return None
    return {
        "symbol": sym,
        "window": window,
        "from_ts": from_ts.isoformat(),
        "to_ts": to_ts.isoformat(),
        "from_price": from_price,
        "to_price": to_price,
        "change_abs": to_price - from_price,
        "change_pct": (to_price - from_price) / from_price * 100.0,
    }


# ────────────────────────── top movers ──────────────────────────


def top_movers(
    window: str = "snapshot",
    limit: int = 5,
    direction: str = "both",
) -> dict[str, list[dict[str, Any]]]:
    """Largest portfolio-value moves over a window.

    Args:
        window: 'snapshot' = vs the second-latest snapshot (matches dashboard
                "Top Movers Since Last Snapshot"); '1d' = vs yesterday's
                reporting-local EOD snapshot (matches dashboard "Daily Δ").
        limit: number of gainers and losers to return.
        direction: 'both' | 'up' | 'down'.
    """
    if window not in ("snapshot", "1d"):
        raise ValueError("window must be 'snapshot' or '1d'")
    if direction not in ("both", "up", "down"):
        raise ValueError("direction must be 'both', 'up', or 'down'")

    # Import lazily — positions imports prices, this would cycle.
    from app.mcp.services import positions as positions_service

    positions = positions_service.current_positions("fifo", held_only=True)
    base_map = (
        prev_day_eod_price_map() if window == "1d" else second_latest_price_map()
    )

    movers: list[dict[str, Any]] = []
    for p in positions:
        sym = p["symbol"]
        qty = float(p["qty"])
        last = p.get("last_price")
        base = base_map.get(sym)
        if last is None or base is None:
            continue
        delta_usd = qty * (last - base)
        delta_pct = ((last - base) / base * 100.0) if base else 0.0
        movers.append({
            "symbol": sym,
            "qty": qty,
            "last_price": last,
            "base_price": base,
            "delta_usd": delta_usd,
            "delta_pct": delta_pct,
        })

    movers.sort(key=lambda r: r["delta_usd"], reverse=True)
    out: dict[str, list[dict[str, Any]]] = {"window": window, "gainers": [], "losers": []}
    if direction in ("both", "up"):
        out["gainers"] = movers[:limit]
    if direction in ("both", "down"):
        out["losers"] = list(reversed(movers[-limit:])) if movers else []
    return out


# ────────────────────────── portfolio value history ──────────────────────────


def portfolio_value_history(
    since: date,
    until: date | None = None,
    *,
    freq: str = "snapshot",
    holdings_basis: str = "historical",
) -> list[dict[str, Any]]:
    """Reconstruct portfolio market value at each snapshot ts in the range.

    Cash is NOT included here — KPI history adds cash separately.

    holdings_basis:
      'historical'        (default) value each point using the holdings
          actually held then, reconstructed from the lot ledger.
      'current_constant'  the previous behaviour: today's quantities held
          constant across all of history. Retained for comparison only — it
          back-projects current positions onto a past that did not hold them,
          so a position opened last month appears owned all year and one since
          sold disappears from the record entirely.

    Prices are split-adjusted from `corporate_actions`, so a 2:1 split no
    longer shows up as a 50% drop in portfolio value.
    """
    if freq not in ("snapshot", "daily"):
        raise ValueError("freq must be 'snapshot' or 'daily'")
    if holdings_basis not in HOLDINGS_BASES:
        raise ValueError(f"holdings_basis must be one of {HOLDINGS_BASES}")
    until = until or date.today()

    with get_conn() as conn:
        actions = corporate_actions.fetch_actions(conn)
        lot_rows = _value_lots(conn, actions)
        if not lot_rows:
            return []

        # Every symbol ever traded, not just those held today: a position sold
        # inside the window still contributed value while it was open.
        symbols = sorted({r["symbol"] for r in lot_rows})
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, symbol, last_price
                FROM price_snapshots
                WHERE ts >= %s AND ts <= %s AND symbol = ANY(%s)
                ORDER BY ts
                """,
                (since, until + timedelta(days=1), symbols),
            )
            rows = cur.fetchall()

    points = corporate_actions.adjust_price_points(
        ((ts, sym, float(price)) for ts, sym, price in rows), actions
    )

    from collections import defaultdict
    bucket: dict[Any, dict[str, float]] = defaultdict(dict)
    for ts, sym, price in points:
        bucket[ts][sym] = price

    ordered = [(ts, bucket[ts]) for ts in sorted(bucket.keys())]

    if holdings_basis == "current_constant":
        qty_map = _current_quantities()
        valued = [
            (ts, sum(qty_map.get(s, 0.0) * p for s, p in prices.items()))
            for ts, prices in ordered
        ]
    else:
        valued = holdings_module.value_series(lot_rows, ordered, carry_forward=True)

    series: list[dict[str, Any]] = [
        {"ts": ts.isoformat(), "market_value": float(value)} for ts, value in valued
    ]

    if freq == "daily":
        # Keep the latest entry per reporting-timezone day.
        from zoneinfo import ZoneInfo
        jer = ZoneInfo(REPORTING_TZ)
        by_day: dict[date, dict[str, Any]] = {}
        for item in series:
            ts = datetime.fromisoformat(item["ts"])
            day = ts.astimezone(jer).date()
            by_day[day] = item  # later overwrites — series already sorted ascending
        series = [by_day[d] for d in sorted(by_day)]
    return series


# ────────────────────────── helpers ──────────────────────────


def _value_lots(
    conn, actions: list[corporate_actions.CorporateAction]
) -> list[dict[str, Any]]:
    """Lot rows for holdings reconstruction, restated into post-split units.

    Only the fields `holdings` needs — this is a valuation input, not a P&L
    one, so price and fees are irrelevant here.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, side, trade_date, quantity, price FROM lots ORDER BY trade_date, id"
        )
        rows = [
            {
                "symbol": r[0],
                "side": r[1],
                "trade_date": r[2],
                "quantity": r[3],
                "price": r[4],
            }
            for r in cur.fetchall()
        ]
    adjusted = corporate_actions.adjust_lot_rows(rows, actions)
    for r in adjusted:
        r["quantity"] = float(r["quantity"])
    return adjusted


def _current_quantities() -> dict[str, float]:
    """Held quantity per symbol today — only for holdings_basis='current_constant'."""
    from app.mcp.services import positions as positions_service

    return {
        p["symbol"]: float(p["qty"])
        for p in positions_service.current_positions("fifo", held_only=True)
    }


# Shared with analytics — see common.window_start. Kept under the module-local
# name so callers and tests keep working.
_window_start = common.window_start


def _price_record(r: dict[str, Any]) -> dict[str, Any]:
    ts = r.get("ts")
    return {
        "symbol": r["symbol"],
        "ts": ts.isoformat() if ts else None,
        "age_seconds": int((datetime.now(timezone.utc) - ts).total_seconds()) if ts else None,
        "last_price": float(r["last_price"]) if r.get("last_price") is not None else None,
        "bid": float(r["bid"]) if r.get("bid") is not None else None,
        "ask": float(r["ask"]) if r.get("ask") is not None else None,
        "source": r.get("source"),
    }
