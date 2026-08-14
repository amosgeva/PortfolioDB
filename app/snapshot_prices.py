"""Append-only price snapshot collector (yfinance).

- Finds all symbols currently present in instruments that have lots.
- Fetches last/bid/ask from yfinance.
- Inserts into price_snapshots (PK: symbol+ts).

Usage:
  set PORTFOLIODB_PASSWORD=...
  python snapshot_prices.py

Notes:
- yfinance bid/ask is best-effort; sometimes None.
- Vendor-reported splits are recorded into `corporate_actions` as they appear,
  with prices adjusted but LOTS NOT — whether the broker credited the extra
  shares is not something a price vendor knows. Rows land `reviewed=FALSE`.
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
import os
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import yfinance as yf

import market_overview
import market_window
from db import connect, execute, fetch_all, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# While the regular session is live, a quote whose last trade is older than
# this is treated as stale upstream data rather than a real print. Generous by
# design: thin names can legitimately go a while between trades, and a false
# "partial" run is worse noise than a slightly late catch.
QUOTE_MAX_AGE_MIN = int(os.environ.get("PORTFOLIODB_QUOTE_MAX_AGE_MIN", "60"))


class StaleQuote(RuntimeError):
    """Yahoo reported a live session but returned an earlier session's price."""


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

    last_price = info.get("regularMarketPrice")
    if last_price is None:
        last_price = info.get("currentPrice")
    if last_price is None:
        raise RuntimeError(f"No price for {symbol}")

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

    return Quote(
        last_price=float(last_price),
        bid=float(bid) if bid is not None else None,
        ask=float(ask) if ask is not None else None,
        market_time=market_time,
        market_state=info.get("marketState"),
        volume=float(volume) if volume is not None else None,
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


def get_quote_with_retry(symbol: str, attempts: int = 3) -> Quote:
    """get_quote with exponential backoff — a transient yfinance blip should
    not drop the symbol from the whole run.

    Staleness is checked by the caller, not here: a stale upstream feed is not
    a transient blip, and retrying it would just burn the backoff on every
    symbol of every run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return get_quote(symbol)
        except Exception:
            if attempt == attempts:
                raise
            wait = 2 ** attempt  # 2s, 4s
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

    Returns a short description when a row is inserted, else None.

    **`adjust_lots` is deliberately FALSE.** Whether the broker credited the
    extra shares is not something the price vendor knows, and the recorded
    quantity may already be in post-split units. Defaulting it TRUE would
    silently rewrite cost basis on the strength of a guess. `adjust_prices` is
    TRUE because the quote series demonstrably *is* rebased at the ex-date —
    that is what makes TWR read a 2:1 split as -50% if left alone.

    `reviewed` stays FALSE so the row surfaces for confirmation rather than
    quietly taking effect unexamined.
    """
    ratio = parse_split_factor(quote.split_factor)
    if ratio is None or quote.split_date is None:
        return None
    if ratio == 1:
        return None
    if floor is not None and quote.split_date < floor:
        return None   # predates anything this database can be affected by

    kind = "SPLIT" if ratio > 1 else "REVERSE_SPLIT"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO corporate_actions
              (symbol, kind, ex_date, ratio, adjust_prices, adjust_lots,
               source, reviewed, notes)
            VALUES (%s, %s, %s, %s, TRUE, FALSE, 'yfinance', FALSE, %s)
            ON CONFLICT (symbol, kind, ex_date) DO NOTHING
            RETURNING id
            """,
            (
                symbol, kind, quote.split_date, ratio,
                f"Auto-recorded from yfinance lastSplitFactor={quote.split_factor!r}. "
                f"Prices are adjusted; LOTS ARE NOT — confirm whether the broker "
                f"credited the extra shares, then set adjust_lots and reviewed. "
                f"Set ex_ts to the ex-date's session open if raw-timestamp series "
                f"matter for this symbol.",
            ),
        )
        inserted = cur.fetchone()
    conn.commit()
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


def _reap_stale_runs(conn) -> None:
    """Close out 'running' rows left behind by a hard-killed process.

    A run normally finishes in seconds; anything still 'running' after 10
    minutes belongs to a process that died without reaching _finish_run
    (reboot, task kill), so mark it failed rather than leaving it dangling.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE snapshot_runs
            SET ts_end = now(),
                status = 'failed',
                error = 'stale: process died before finishing (reaped at startup)'
            WHERE status = 'running'
              AND ts_start < now() - interval '10 minutes'
            RETURNING id
            """,
        )
        reaped = [row[0] for row in cur.fetchall()]
    conn.commit()
    if reaped:
        log.warning(f"Reaped stale running snapshot_runs: {reaped}")


def main():
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
    args = ap.parse_args()

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

    with connect(cfg) as conn:
        _reap_stale_runs(conn)
        # A benchmark run deliberately records NO row in snapshot_runs. That table
        # means one thing — "an attempt to collect the portfolio's prices" — and
        # three readers depend on it meaning exactly that: cutoff.py steps the
        # cutoff back behind any in-flight run, data_quality.py judges freshness
        # against runs rather than clock age, and health.py reports the last one.
        # Benchmarks collect round the clock, so recording them would pin the
        # cutoff behind a futures fetch and tell Data Health your holdings are
        # fresh when they have not been collected since Friday. The strip carries
        # its own as-of timestamp, which is the staleness signal that belongs to it.
        run_id = None if args.benchmarks else _start_run(conn, ts)

        try:
            if args.benchmarks:
                # sync_flags makes instruments.benchmark match the setting, so the
                # setting stays the only place the symbol set is edited.
                symbols = market_overview.sync_flags(conn)
                if not symbols:
                    log.info("No market-overview symbols configured — nothing collected.")
                    _finish_run(conn, run_id, "ok", 0, 0, 0, None)
                    return
                log.info("Collecting %d benchmark symbol(s): %s", len(symbols), ", ".join(symbols))
            else:
                symbols = fetch_all(
                conn,
                """
                WITH pos AS (
                  SELECT symbol,
                         SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) AS qty
                  FROM lots
                  GROUP BY symbol
                )
                SELECT i.symbol
                FROM instruments i
                LEFT JOIN pos p ON p.symbol = i.symbol
                WHERE COALESCE(p.qty, 0) > 0
                   OR i.watchlist = TRUE
                ORDER BY i.symbol
                """,
                )
                symbols = [r["symbol"] for r in symbols]

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

            for sym in symbols:
                try:
                    quote = get_quote_with_retry(sym)
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

                execute(
                    conn,
                    """
                    INSERT INTO price_snapshots(ts, symbol, last_price, bid, ask, source, session, volume)
                    VALUES (%s, %s, %s, %s, %s, 'yfinance', %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (ts, sym, quote.last_price, quote.bid, quote.ask,
                     quote.market_state, quote.volume),
                )
                inserted += 1

                # Best-effort: a vendor hiccup here must not fail the price run,
                # which is the collector's actual job.
                try:
                    recorded = record_split(conn, sym, quote, floor)
                    if recorded:
                        splits_found.append(recorded)
                except Exception as e:
                    log.warning(f"{sym}: could not record split — {e}")

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
                    "NEW CORPORATE ACTION(S) recorded — prices adjusted, lots NOT. "
                    "Confirm whether shares were credited, then set adjust_lots:"
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
            err_text = traceback.format_exc()
            _finish_run(conn, run_id, "failed", 0, 0, 0, err_text)
            raise


if __name__ == "__main__":
    main()
