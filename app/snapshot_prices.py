"""Append-only price snapshot collector (yfinance).

- Finds all symbols currently present in instruments that have lots.
- Fetches last/bid/ask from yfinance.
- Inserts into price_snapshots (PK: symbol+ts).

Usage:
  set PORTFOLIODB_PASSWORD=...
  python snapshot_prices.py

Notes:
- yfinance bid/ask is best-effort; sometimes None, and outside regular hours
  often 0.0, which is stored as NULL (a zero bid is not a quote).
- A non-positive or non-finite last price is refused (the symbol counts as
  failed): one stored zero used to zero every value and return computed from
  it for good (audit F-15). The database refuses it too.
- Vendor-reported splits are recorded into `corporate_actions` as they appear,
  PENDING REVIEW (`reviewed=FALSE`). A pending row changes no figure — not
  prices, not lots — until the operator confirms it with
  `python check_splits.py --approve SYMBOL:YYYY-MM-DD` and says whether the
  broker credited the shares (audit F-3). A split already recorded within a
  week of the vendor's date is not recorded again (F-16).
- One run at a time: holdings runs take a PostgreSQL advisory lock (benchmark
  runs another), so a manual run and the scheduled one cannot overlap, and
  only a run holding the lock reaps 'running' rows — which, since every live
  run holds it, can only be dead ones (audit E-5). Each run also has a
  deadline (PORTFOLIODB_COLLECTOR_DEADLINE_S, default 240 s, inside the
  five-minute tick); symbols it does not reach are reported, not hung on.
- Each symbol is written under its own savepoint and committed on its own, so
  a database error on one symbol — its price or its split record — cannot
  abort the run or leave it marked 'running' (audit E-1, Q-7).
- `volume` is the cumulative session count at snapshot time, not a daily total.
  It is recorded only so liquidity history starts accumulating; nothing reads it
  yet. Any future average-daily-volume metric must use the LAST snapshot per
  day, never a mid-session row.
- Quotes are rejected when Yahoo reports a live regular session but returns an
  earlier session's trade timestamp (see check_fresh). Such a run lands as
  'partial'/'failed' rather than writing a stale price under a fresh ts.
  Override the tolerance with PORTFOLIODB_QUOTE_MAX_AGE_MIN (minutes).
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import yfinance as yf

import psycopg2

import corporate_actions
import ledger_inputs
import ledger_writes
import market_overview
import market_window
from db import connect, execute, fetch_all, load_config, run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# While the regular session is live, a quote whose last trade is older than
# this is treated as stale upstream data rather than a real print. Generous by
# design: thin names can legitimately go a while between trades, and a false
# "partial" run is worse noise than a slightly late catch.
QUOTE_MAX_AGE_MIN = int(os.environ.get("PORTFOLIODB_QUOTE_MAX_AGE_MIN", "60"))

# Whole-run budget. The holdings job ticks every five minutes; a run that is
# still fetching after four has stopped being useful, and stopping it keeps the
# next tick from finding the lock held. yfinance's own HTTP timeout (30 s) bounds
# a single request, not a run of forty symbols with retries.
RUN_DEADLINE_S = float(os.environ.get("PORTFOLIODB_COLLECTOR_DEADLINE_S", "240"))

# Session-level advisory locks: (namespace, kind). Released when the
# connection closes, including when the process dies, so a crash never leaves
# one behind.
LOCK_NAMESPACE = 7_210_402
LOCK_HOLDINGS = 1
LOCK_BENCHMARKS = 2


class StaleQuote(RuntimeError):
    """Yahoo reported a live session but returned an earlier session's price."""


class BadQuote(RuntimeError):
    """The vendor returned a price that cannot be one: zero, negative, NaN."""


def _positive(value) -> float | None:
    """A finite, strictly positive float, or None."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


@dataclass(frozen=True)
class Quote:
    last_price: float
    bid: float | None
    ask: float | None
    market_time: datetime | None  # last regular-session trade, UTC
    market_state: str | None  # PRE / REGULAR / POST / CLOSED
    # Cumulative session volume at the moment of the quote — NOT a daily total.
    # Stored so liquidity history accumulates; nothing computes on it yet.
    volume: float | None = None
    # Vendor-reported most recent split, e.g. '10:1' with its ex-date. This is
    # the authoritative signal; the price-ratio heuristic in check_splits.py is
    # only a backstop, and cannot tell a split from a one-day crash.
    split_factor: str | None = None
    split_date: date | None = None


def get_quote(symbol: str) -> Quote:
    """Pull a quote for `symbol` from yfinance.

    Uses `Ticker.info` as the single source. `fast_info` is deliberately not
    used: as of yfinance 1.5.2 its keys are camelCase, so the previous
    `fi.get("last_price")` always returned None and every symbol silently fell
    through to `info` regardless — paying for an extra round-trip to learn
    nothing. It also exposes neither bid/ask nor any trade timestamp, and the
    timestamp is what the staleness guard needs.
    """
    info = yf.Ticker(symbol).info

    candidates = [info.get("regularMarketPrice"), info.get("currentPrice")]
    if all(c is None for c in candidates):
        raise RuntimeError(f"No price for {symbol}")
    # The first usable price wins; a 0 regularMarketPrice next to a real
    # currentPrice is the vendor's fault, not the market's.
    last_price = next((p for p in map(_positive, candidates) if p is not None), None)
    if last_price is None:
        raise BadQuote(
            f"vendor price for {symbol} is not a price: "
            f"regularMarketPrice={candidates[0]!r}, currentPrice={candidates[1]!r}"
        )

    epoch = info.get("regularMarketTime")
    market_time = (
        datetime.fromtimestamp(epoch, timezone.utc)
        if isinstance(epoch, (int, float))
        else None
    )

    bid = info.get("bid")
    ask = info.get("ask")

    # regularMarketVolume is the regular session's cumulative count; `volume`
    # is its alias. Either may be absent for a thin instrument, in which case
    # the column stays NULL rather than recording a zero that would read as
    # "no trading" instead of "not reported".
    volume = info.get("regularMarketVolume")
    if volume is None:
        volume = info.get("volume")

    split_epoch = info.get("lastSplitDate")
    split_date = (
        datetime.fromtimestamp(split_epoch, timezone.utc).date()
        if isinstance(split_epoch, (int, float))
        else None
    )

    vol = None
    if volume is not None:
        try:
            vol = float(volume)
        except (TypeError, ValueError):
            vol = None
        if vol is not None and (not math.isfinite(vol) or vol < 0):
            vol = None

    return Quote(
        last_price=last_price,
        # Outside regular hours Yahoo reports bid/ask as 0.0 for many symbols.
        # A zero bid is "no quote", and stored as one it read as a real price
        # and made any spread computed from it garbage.
        bid=_positive(bid),
        ask=_positive(ask),
        market_time=market_time,
        market_state=info.get("marketState"),
        volume=vol,
        split_factor=info.get("lastSplitFactor"),
        split_date=split_date,
    )


def check_fresh(quote: Quote, now: datetime) -> None:
    """Raise StaleQuote if Yahoo claims a live regular session but hands back a
    trade timestamp from an earlier one.

    Only enforced while marketState == 'REGULAR'. Outside the regular session
    the last regular print is legitimately hours old — the 15:15–16:30
    reporting-local pre-market slice of the snapshot window correctly still carries
    the previous close — so enforcing freshness there would fire every day.
    """
    if (quote.market_state or "").upper() != "REGULAR":
        return
    if quote.market_time is None:
        return

    age_min = (now - quote.market_time).total_seconds() / 60.0
    if age_min > QUOTE_MAX_AGE_MIN:
        raise StaleQuote(
            f"last trade {age_min:.0f} min old ({quote.market_time.isoformat()}) "
            f"while marketState=REGULAR"
        )



def benchmark_market_closed(market_state: str | None) -> bool:
    """Should a benchmark quote be skipped because its market is shut?

    Only for benchmarks, and only for this reason: when a futures market is
    closed the vendor keeps serving the last print, so a collector running every
    15 minutes would write that same price under a fresh timestamp ~96 times a
    day. The rows are not wrong exactly, but the strip's "as of" line would claim
    the price is current when it is Friday's, and the sparkline would grow a long
    flat tail that reads as a quiet market rather than a shut one.

    Holdings are deliberately NOT treated this way — see check_fresh: outside
    regular hours `regularMarketPrice` legitimately *is* the previous close, and
    refusing it would leave the portfolio unpriced every evening.
    """
    return (market_state or "").upper() != "REGULAR"


def get_quote_with_retry(symbol: str, attempts: int = 3, *, deadline: float | None = None) -> Quote:
    """get_quote with exponential backoff — a transient yfinance blip should
    not drop the symbol from the whole run.

    Staleness is checked by the caller, not here: a stale upstream feed is not
    a transient blip, and retrying it would just burn the backoff on every
    symbol of every run. A retry that would sleep past ``deadline`` (a
    ``time.monotonic()`` instant) is not attempted: the run's budget is spent.
    """
    for attempt in range(1, attempts + 1):
        try:
            return get_quote(symbol)
        except Exception:
            if attempt == attempts:
                raise
            wait = 2 ** attempt  # 2s, 4s
            if deadline is not None and time.monotonic() + wait >= deadline:
                raise
            log.info(f"{symbol}: quote attempt {attempt} failed; retrying in {wait}s")
            time.sleep(wait)


# ────────────────────────── split recording ──────────────────────────


def parse_split_factor(factor: str | None) -> Decimal | None:
    """'10:1' -> 10, '1:10' -> 0.1. New shares per old share.

    Returns None for anything unparseable rather than guessing — a corporate
    action recorded from a misread string would adjust real numbers.
    """
    if not factor or ":" not in str(factor):
        return None
    new, _, old = str(factor).partition(":")
    try:
        new_d, old_d = Decimal(new.strip()), Decimal(old.strip())
    except Exception:
        return None
    if old_d == 0 or new_d <= 0:
        return None
    return new_d / old_d


def ledger_floor_date(conn) -> date | None:
    """Earliest date any recorded split could still matter.

    A split before both the first trade and the first price observation cannot
    affect any number this database holds, so recording it is noise.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT LEAST(
              (SELECT MIN(trade_date) FROM lots),
              (SELECT MIN(ts)::date FROM price_snapshots)
            )
            """
        )
        row = cur.fetchone()
        return row[0] if row else None


def record_split(conn, symbol: str, quote: Quote, floor: date | None) -> str | None:
    """Record a vendor-reported split, if it is new and still relevant.

    Returns a short description when a row is inserted, else None. Does not
    commit: the caller's per-symbol transaction does.

    **The row is recorded PENDING REVIEW and changes nothing until confirmed**
    (``reviewed = FALSE``; see corporate_actions.effective_actions). It used to
    take effect at once with prices adjusted and lots not, so a 2:1 split
    halved every position's value until someone noticed (audit F-3). A price
    vendor cannot know whether the broker credited the shares, so a human
    decides, with ``check_splits.py --approve``. The proposal is the common
    case — prices and lots adjusted together (``adjust_lots = TRUE``) — so an
    approval that skips the question still applies both sides.

    Not recorded when an action of the same symbol, kind and ratio is already
    on file within ``corporate_actions.DUPLICATE_WINDOW_DAYS`` of the vendor's
    date: that is the same split under another date (manual vs vendor), and
    two records of one split were applied twice (audit F-16). The database
    trigger refuses such a pair as well.
    """
    ratio = parse_split_factor(quote.split_factor)
    if ratio is None or quote.split_date is None:
        return None
    if ratio == 1:
        return None
    if floor is not None and quote.split_date < floor:
        return None   # predates anything this database can be affected by

    kind = "SPLIT" if ratio > 1 else "REVERSE_SPLIT"
    window = corporate_actions.DUPLICATE_WINDOW_DAYS
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO corporate_actions
              (symbol, kind, ex_date, ratio, adjust_prices, adjust_lots,
               source, reviewed, notes)
            SELECT %s, %s, %s, %s, TRUE, TRUE, 'yfinance', FALSE, %s
            WHERE NOT EXISTS (
              SELECT 1 FROM corporate_actions
              WHERE symbol = %s AND kind = %s AND ratio = %s
                AND ex_date BETWEEN %s::date - %s AND %s::date + %s
            )
            ON CONFLICT (symbol, kind, ex_date) DO NOTHING
            RETURNING id
            """,
            (
                symbol, kind, quote.split_date, ratio,
                f"Auto-recorded from yfinance lastSplitFactor={quote.split_factor!r}. "
                f"PENDING REVIEW — this row changes no figure until confirmed. Run "
                f"check_splits.py --approve {symbol}:{quote.split_date} with "
                f"--shares-credited (the broker added the shares; the usual case) or "
                f"--quantities-already-post-split. Set ex_ts to the ex-date's session "
                f"open if raw-timestamp series matter for this symbol.",
                symbol, kind, ratio, quote.split_date, window, quote.split_date, window,
            ),
        )
        inserted = cur.fetchone()
    if inserted:
        return f"{symbol} {kind} {ratio} on {quote.split_date}"
    return None


def _start_run(conn, ts_start: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO snapshot_runs(ts_start, status) VALUES (%s, 'running') RETURNING id",
            (ts_start,),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def _finish_run(conn, run_id: int | None, status: str, total: int, ok: int, failed: int, error: str | None) -> None:
    # run_id is None for a benchmark run, which records no row — see main().
    if run_id is None:
        return
    execute(
        conn,
        """
        UPDATE snapshot_runs
        SET ts_end = now(),
            status = %s,
            symbols_total = %s,
            symbols_ok = %s,
            symbols_failed = %s,
            error = %s
        WHERE id = %s
        """,
        (status, total, ok, failed, error, run_id),
    )


def _try_lock(conn, kind: int) -> bool:
    """Take this run's session-level advisory lock, without waiting."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_NAMESPACE, kind))
        got = bool(cur.fetchone()[0])
    conn.commit()  # the lock is session-level; do not hold a transaction open
    return got


def _session_limits(conn) -> None:
    """Bound every statement, so a stuck lock or query cannot outlive the run."""
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '60s'")
        cur.execute("SET lock_timeout = '10s'")
    conn.commit()


def _reap_dead_runs(conn) -> None:
    """Close out 'running' rows left behind by a process that died.

    Called only while holding the holdings lock. Every live holdings run holds
    that lock for its whole life, so any 'running' row seen now belongs to a
    process that is gone (reboot, kill, OOM) — whatever its age. The old rule,
    "older than ten minutes", reaped a legitimately slow run from under it
    whenever a manual or benchmark run started (audit E-5), and left a dead
    one alone for ten minutes.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE snapshot_runs
            SET ts_end = now(),
                status = 'failed',
                error = 'stale: process died before finishing (reaped by the next run)'
            WHERE status = 'running'
            RETURNING id
            """,
        )
        reaped = [row[0] for row in cur.fetchall()]
    conn.commit()
    if reaped:
        log.warning(f"Reaped dead running snapshot_runs: {reaped}")


def _fail_run(conn, cfg, run_id: int | None, error: str) -> None:
    """Mark the run failed even when its own connection is unusable.

    The old handler issued the UPDATE on a connection whose transaction a
    failed statement had already aborted, so the update failed too and the
    run stayed 'running' until reaped (audit E-1). Roll back first; if the
    connection itself is gone, use a fresh one.
    """
    if run_id is None:
        return
    try:
        conn.rollback()
        _finish_run(conn, run_id, "failed", 0, 0, 0, error)
        return
    except Exception:
        log.exception("could not mark run %s failed on its own connection", run_id)
    try:
        fresh = connect(cfg)
        try:
            _finish_run(fresh, run_id, "failed", 0, 0, 0, error)
        finally:
            fresh.close()
    except Exception:
        log.exception("could not mark run %s failed at all (the next run reaps it)", run_id)


def held_or_watched_symbols(conn) -> list[str]:
    """What a holdings run collects: every symbol with an open position, by
    the engines' rules (``ledger_inputs.open_symbols``), plus the watchlist.

    This was a SQL sum of signed quantities, which disagreed with the engines
    whenever a sale was truncated or recorded under the wrong account (audit
    R-1): a position the dashboard showed as open could go uncollected.
    """
    held = sorted(ledger_inputs.open_symbols(conn))
    rows = fetch_all(
        conn,
        "SELECT symbol FROM instruments WHERE watchlist = TRUE OR symbol = ANY(%s) ORDER BY symbol",
        (held,),
    )
    return [r["symbol"] for r in rows]


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Collect one price snapshot.")
    ap.add_argument(
        "--ignore-window",
        action="store_true",
        help="Collect even outside the configured market window.",
    )
    ap.add_argument(
        "--benchmarks",
        action="store_true",
        help=(
            "Collect the market-overview benchmarks (index futures, volatility) "
            "instead of your holdings, and ignore the market window — futures "
            "quote when the equity market is shut, which is the whole point of "
            "collecting them."
        ),
    )
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    # The window guard lives here rather than in the caller, so every entry
    # point obeys it: the container scheduler runs this on a plain cron tick,
    # the retired run_snapshot.ps1 checked the clock itself, and an operator
    # running it by hand gets the same rule.
    # Benchmarks are exempt: the window describes when *your market* trades, and
    # a futures quote at 03:00 ET is a live regular-session price. Gating them on
    # the equity window would leave the strip nine hours stale at exactly the hour
    # someone opens the dashboard to ask what happened overnight.
    if not args.benchmarks and not args.ignore_window and not market_window.is_open():
        log.info(
            "Outside the market window (%s) — nothing collected. "
            "Use --ignore-window to force.",
            market_window.describe(),
        )
        return

    cfg = load_config()
    ts = datetime.now(timezone.utc)
    # A plain connection, closed in `finally` — `with connect()` neither closes
    # it nor, therefore, releases the advisory lock until garbage collection.
    conn = connect(cfg)
    try:
        return _collect(conn, cfg, args, ts)
    finally:
        conn.close()


def _collect(conn, cfg, args, ts: datetime):
    _session_limits(conn)
    kind = LOCK_BENCHMARKS if args.benchmarks else LOCK_HOLDINGS
    if not _try_lock(conn, kind):
        log.warning(
            "Another %s collector run is in progress (it holds the lock) — not "
            "starting a second one.", "benchmark" if args.benchmarks else "holdings",
        )
        return
    deadline = time.monotonic() + RUN_DEADLINE_S

    # A benchmark run deliberately records NO row in snapshot_runs, and never
    # reaps. That table means one thing — "an attempt to collect the portfolio's
    # prices" — and three readers depend on it meaning exactly that: cutoff.py
    # steps the cutoff back behind any in-flight run, data_quality.py judges
    # freshness against runs rather than clock age, and health.py reports the
    # last one. Benchmarks collect round the clock, so recording them would pin
    # the cutoff behind a futures fetch and tell Data Health your holdings are
    # fresh when they have not been collected since Friday. The strip carries
    # its own as-of timestamp, which is the staleness signal that belongs to it.
    if not args.benchmarks:
        _reap_dead_runs(conn)
    run_id = None if args.benchmarks else _start_run(conn, ts)

    try:
        if args.benchmarks:
            # sync_flags makes instruments.benchmark match the setting, so the
            # setting stays the only place the symbol set is edited.
            try:
                symbols = market_overview.sync_flags(conn)
            except psycopg2.errors.UndefinedColumn:
                # Upgraded the image but skipped the schema step. A raw
                # psycopg2 traceback in a cron log is a poor way to learn that
                # — the fix is one documented command.
                conn.rollback()
                log.error(
                    "instruments.benchmark is missing — the market-overview "
                    "migration has not been applied. Run: docker compose run "
                    "--rm dashboard python app/apply_schema.py  (idempotent). "
                    "Nothing else is affected; the Markets strip stays empty "
                    "until then."
                )
                return 1
            if not symbols:
                log.info("No market-overview symbols configured — nothing collected.")
                return
            log.info("Collecting %d benchmark symbol(s): %s", len(symbols), ", ".join(symbols))
        else:
            symbols = held_or_watched_symbols(conn)

        if not symbols:
            log.warning("No symbols found. Add lots first.")
            _finish_run(conn, run_id, "ok", 0, 0, 0, None)
            return

        inserted = 0
        failed = []
        stale = []
        splits_found = []
        # Splits older than the ledger cannot affect anything here.
        floor = ledger_floor_date(conn)
        conn.commit()  # end the read transaction before the network calls

        for sym in symbols:
            if time.monotonic() >= deadline:
                failed.append((sym, f"not reached: the run's {RUN_DEADLINE_S:.0f}s deadline passed"))
                continue
            try:
                quote = get_quote_with_retry(sym, deadline=deadline)
                if args.benchmarks and benchmark_market_closed(quote.market_state):
                    log.info(
                        "%s: market %s — nothing new to record",
                        sym, (quote.market_state or "unknown").lower(),
                    )
                    continue
                check_fresh(quote, ts)
            except StaleQuote as e:
                # Don't persist it: writing an earlier session's close under
                # today's ts is what makes the dashboard look quietly fresh
                # while showing yesterday's numbers.
                stale.append((sym, str(e)))
                log.warning(f"{sym}: skipping stale quote — {e}")
                continue
            except Exception as e:
                failed.append((sym, str(e)))
                continue

            # One symbol, one transaction. The savepoint keeps a rejected row
            # from aborting the transaction, and the commit makes each stored
            # price durable on its own.
            try:
                with ledger_writes.savepoint(conn, "pdb_symbol"):
                    run(
                        conn,
                        """
                        INSERT INTO price_snapshots(ts, symbol, last_price, bid, ask, source, session, volume)
                        VALUES (%s, %s, %s, %s, %s, 'yfinance', %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (ts, sym, quote.last_price, quote.bid, quote.ask,
                         quote.market_state, quote.volume),
                    )
            except psycopg2.Error as e:
                failed.append((sym, f"not stored: {str(e).strip().splitlines()[0]}"))
                conn.commit()
                continue
            inserted += 1

            # Best-effort, under its own savepoint: a failed split record must
            # not take the price down with it, nor poison the transaction the
            # next symbol runs in (audit Q-7).
            try:
                with ledger_writes.savepoint(conn, "pdb_split"):
                    recorded = record_split(conn, sym, quote, floor)
                if recorded:
                    splits_found.append(recorded)
            except psycopg2.Error as e:
                log.warning(f"{sym}: could not record split — {e}")
            conn.commit()

        total = len(symbols)
        problems = stale + failed
        if not problems:
            status = "ok"
            error_text = None
        else:
            status = "failed" if inserted == 0 else "partial"
            error_text = "; ".join(
                [f"stale: {s}: {e}" for s, e in stale[:10]]
                + [f"{s}: {e}" for s, e in failed[:10]]
            )

        _finish_run(conn, run_id, status, total, inserted, len(problems), error_text)

        log.info(f"OK snapshots inserted: {inserted}/{total} at {ts.isoformat()}")
        if splits_found:
            log.warning(
                "NEW CORPORATE ACTION(S) recorded, PENDING REVIEW — they change no "
                "figure until approved. Confirm each against the broker, then run "
                "check_splits.py --approve SYMBOL:YYYY-MM-DD --shares-credited "
                "(or --quantities-already-post-split):"
            )
            for line in splits_found:
                log.warning(f"  {line}")
        if stale:
            log.warning(f"Stale upstream quotes skipped ({len(stale)}/{total}):")
            for sym, err in stale:
                log.warning(f"  {sym}: {err}")
        if failed:
            log.warning("Failed:")
            for sym, err in failed:
                log.warning(f"  {sym}: {err}")
    except Exception:
        _fail_run(conn, cfg, run_id, traceback.format_exc())
        raise


if __name__ == "__main__":
    # SystemExit(main()) rather than main(): the benchmark path returns 1 when the
    # migration is missing, and a cron job that exits 0 on failure is a cron job
    # nobody notices. main() returns None on every other path, which exits 0.
    raise SystemExit(main())
