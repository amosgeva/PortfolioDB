"""Market overview — index futures and volatility, for the hours your book is asleep.

Answers one question: *what is happening before the open?* Index futures answer it
and extended-hours equity prices mostly do not — at 03:00 ET a futures quote is a
live regular-session price, while `SPY` has no print at all. Measured 2026-08-14
during pre-market: `SPY` reported `marketState=PRE`, while `ES=F`, `NQ=F`, `YM=F`
and `^VIX` all reported `REGULAR`.

Two rules hold this together:

**The setting is the single source of truth.** `market_overview_symbols` is a
comma-separated `SYMBOL:Label` list, editable from the Settings page. Collection
and display both read it, and `sync_flags` makes `instruments.benchmark` match it
on every run — so removing a symbol stops collecting it and stops showing it,
with no second place to edit.

**Benchmarks never touch the portfolio.** They have no lots, so the P&L engines
cannot see them; the flag keeps them out of the watchlist rail and out of Data
Health's per-symbol scope. Nothing here is read by any position, cost-basis or
return calculation.
"""

from __future__ import annotations

import logging

import reporting_tz
import settings
from db import execute, fetch_all

log = logging.getLogger(__name__)

SETTING_KEY = "market_overview_symbols"
ENV_VAR = "PORTFOLIODB_MARKET_OVERVIEW"

# US index futures plus volatility. Futures trade nearly 23 hours, which is the
# entire point: they are quoting when the equity market is closed.
DEFAULT_SYMBOLS = "ES=F:S&P Futures,NQ=F:Nasdaq Futures,YM=F:Dow Futures,^VIX:VIX"

SPARK_POINTS = 40


def _fallback_label(symbol: str) -> str:
    """A readable label for a symbol given without one."""
    return symbol.removesuffix("=F").lstrip("^") or symbol


def configured() -> list[tuple[str, str]]:
    """[(symbol, label)] in display order, from the setting.

    Tolerant on purpose — this is a hand-edited free-text field on a settings
    page. A bare `GC=F` gets a derived label, whitespace and empty entries are
    dropped, and a repeated symbol keeps its first position rather than rendering
    twice.
    """
    raw = settings.get(SETTING_KEY, env=ENV_VAR, default=DEFAULT_SYMBOLS) or ""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        symbol, _, label = entry.partition(":")
        symbol = symbol.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append((symbol, label.strip() or _fallback_label(symbol)))
    return out


def sync_flags(conn) -> list[str]:
    """Make `instruments.benchmark` match the setting. Returns the symbols to collect.

    Called by the collector rather than by a separate management command, so the
    setting cannot drift from the flag. Clearing the flag on delisted entries is
    the half that matters: without it, a symbol removed from the setting would
    disappear from the strip and keep being collected forever.
    """
    wanted = [sym for sym, _ in configured()]

    for symbol in wanted:
        execute(
            conn,
            """
            INSERT INTO instruments (symbol, benchmark)
            VALUES (%s, TRUE)
            ON CONFLICT (symbol) DO UPDATE
               SET benchmark = TRUE, updated_at = now()
            """,
            (symbol,),
        )

    # Anything still flagged but no longer wanted stops being collected. The row
    # stays: its price history is real and the append-only rule applies to it too.
    if wanted:
        execute(
            conn,
            "UPDATE instruments SET benchmark = FALSE, updated_at = now() "
            "WHERE benchmark AND NOT (symbol = ANY(%s))",
            (wanted,),
        )
    else:
        execute(conn, "UPDATE instruments SET benchmark = FALSE, updated_at = now() WHERE benchmark")

    return wanted


def overview(conn) -> list[dict]:
    """One row per configured symbol: last, change vs the previous session, spark.

    The day-change baseline is the same one the ticker tape uses — the last price
    of the session *before* the latest snapshot's session, not "yesterday" by the
    clock. That is what keeps the number honest on a Monday morning or a holiday,
    when "today" has no snapshots and a naive query silently reports 0.00%.

    A symbol with no snapshots yet returns `price: None`. The strip renders that
    as "no data yet" rather than as zero — a fresh install has no history until
    the collector has run, and a zero would read as a flat market.
    """
    pairs = configured()
    if not pairs:
        return []

    symbols = [s for s, _ in pairs]
    rows = fetch_all(
        conn,
        """
        WITH latest AS (
          SELECT symbol, MAX(ts) AS ts
          FROM price_snapshots
          WHERE symbol = ANY(%(syms)s)
          GROUP BY symbol
        ),
        prev AS (
          SELECT ps.symbol, ps.last_price,
                 ROW_NUMBER() OVER (PARTITION BY ps.symbol ORDER BY ps.ts DESC) AS rn
          FROM price_snapshots ps
          JOIN latest l ON l.symbol = ps.symbol
          WHERE (ps.ts AT TIME ZONE %(tz)s)::date < (l.ts AT TIME ZONE %(tz)s)::date
        )
        SELECT l.symbol,
               cur.last_price AS price,
               cur.ts         AS ts,
               p.last_price   AS prev_close
        FROM latest l
        JOIN price_snapshots cur ON cur.symbol = l.symbol AND cur.ts = l.ts
        LEFT JOIN prev p ON p.symbol = l.symbol AND p.rn = 1
        """,
        {"syms": symbols, "tz": reporting_tz.tz_name()},
    )
    by_symbol = {r["symbol"]: r for r in rows}

    spark_rows = fetch_all(
        conn,
        """
        SELECT symbol, last_price, ts
        FROM (
          SELECT symbol, last_price, ts,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
          FROM price_snapshots
          WHERE symbol = ANY(%s)
        ) ranked
        WHERE rn <= %s
        ORDER BY symbol, ts
        """,
        (symbols, SPARK_POINTS),
    )
    sparks: dict[str, list[float]] = {}
    for r in spark_rows:
        sparks.setdefault(r["symbol"], []).append(float(r["last_price"]))

    out = []
    for symbol, label in pairs:
        row = by_symbol.get(symbol)
        price = float(row["price"]) if row and row["price"] is not None else None
        prev = float(row["prev_close"]) if row and row.get("prev_close") is not None else None
        change = pct = None
        if price is not None and prev:
            change = price - prev
            pct = change / prev * 100
        out.append({
            "sym": symbol,
            "label": label,
            "price": price,
            "change": change,
            "pct": pct,
            "hist": sparks.get(symbol, []),
            "ts": row["ts"].isoformat() if row and row.get("ts") else None,
        })
    return out
