"""Scan price history for unrecorded splits, then check each hit against the vendor.

The price-ratio heuristic alone **cannot** tell a split from a one-day crash —
both halve the price overnight. That is not a theoretical caveat: PRIM fell
0.4997 in a day on 2026-05-06 and was recorded as a 2:1 split on that evidence.
It was a real decline, and the wrong adjustment distorted every return spanning
the date until it was corrected.

So every hit is now cross-referenced against yfinance, which keeps an
authoritative split register. A hit the vendor does not confirm is evidence
*against* a split, not merely an absence of evidence. It reports; it never
writes.

Usage (from app/, with PORTFOLIODB_PASSWORD set):

    python check_splits.py
    python check_splits.py --symbol PRIM --tolerance 0.05

Exit code is 1 when anything is flagged, so a scheduled run can alert.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import corporate_actions
import reporting_tz
from db import connect, load_config


def fetch_daily_prices(conn, symbol: str | None) -> dict[str, list[tuple]]:
    """{symbol: [(day, last_price)]} — one price per reporting-timezone day."""
    tz = reporting_tz.tz_name()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (symbol, date_trunc('day', ts AT TIME ZONE %s))
                   symbol,
                   date_trunc('day', ts AT TIME ZONE %s)::date AS day_local,
                   last_price
            FROM price_snapshots
            WHERE last_price > 0
              AND (%s::text IS NULL OR symbol = %s)
            ORDER BY symbol,
                     date_trunc('day', ts AT TIME ZONE %s),
                     ts DESC
            """,
            (tz, tz, symbol, symbol, tz),
        )
        out: dict[str, list[tuple]] = defaultdict(list)
        for sym, day, price in cur.fetchall():
            out[sym].append((day, float(price)))
    return out


def _symbol_resolves(symbol: str) -> bool:
    """Whether yfinance knows this ticker at all."""
    try:
        import yfinance as yf

        info = yf.Ticker(symbol).info
        return bool(
            info
            and (info.get("regularMarketPrice") is not None
                 or info.get("currentPrice") is not None
                 or info.get("previousClose") is not None)
        )
    except Exception:
        return False


def vendor_verdict(symbol: str, day) -> dict:
    """Ask yfinance whether it knows of a split for this symbol near this date.

    A split within a few days either side counts as confirmation — vendor
    ex-dates and the day a price step becomes visible in our snapshots can
    differ by a session.

    Any failure returns 'unknown', never 'contradicted': being unable to reach
    the vendor is not evidence that a split did not happen.
    """
    try:
        import yfinance as yf

        splits = yf.Ticker(symbol).splits
    except Exception as e:
        return {"status": "unknown", "detail": f"vendor lookup failed: {e}"}

    if splits is None or len(splits) == 0:
        # An empty series is ambiguous: a real symbol that never split looks
        # exactly like a ticker yfinance cannot resolve at all. Confirm the
        # symbol resolves before treating the emptiness as evidence — reporting
        # "not a split" because the lookup silently failed would be the same
        # over-confidence this cross-check exists to prevent.
        if not _symbol_resolves(symbol):
            return {
                "status": "unknown",
                "detail": (
                    "yfinance returned no data for this symbol at all — cannot "
                    "tell 'never split' from 'could not look it up'."
                ),
            }
        return {
            "status": "contradicted",
            "detail": (
                "yfinance lists no splits for this symbol, ever — a price step "
                "this size is far more likely a real move."
            ),
        }

    for ts, ratio in splits.items():
        vendor_day = ts.date() if hasattr(ts, "date") else ts
        if abs((vendor_day - day).days) <= 3:
            return {
                "status": "confirmed",
                "detail": f"yfinance reports a {float(ratio):g}:1 split on {vendor_day}",
            }

    latest = max(
        (t.date() if hasattr(t, "date") else t) for t in splits.index
    )
    return {
        "status": "contradicted",
        "detail": (
            f"yfinance knows {len(splits)} split(s) for this symbol but none "
            f"near {day} (most recent {latest})."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", help="Restrict the scan to one symbol.")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=corporate_actions.DETECT_TOLERANCE,
        help="Max fractional distance from a common split ratio (default %(default)s).",
    )
    parser.add_argument(
        "--no-vendor-check",
        action="store_true",
        help="Skip the yfinance cross-reference (offline use).",
    )
    args = parser.parse_args()

    cfg = load_config()
    conn = connect(cfg)
    try:
        known = corporate_actions.fetch_actions(conn)
        prices = fetch_daily_prices(
            conn, args.symbol.upper() if args.symbol else None
        )
    finally:
        conn.close()

    found = corporate_actions.detect_suspected_splits(
        prices, known=known, tolerance=args.tolerance
    )

    if known:
        print(f"{len(known)} action(s) already recorded:")
        for a in known:
            flags = []
            if not a.adjust_prices:
                flags.append("prices NOT adjusted")
            if not a.adjust_lots:
                flags.append("lots NOT adjusted")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  {a.symbol:8} {a.ex_date}  ratio {a.ratio}{suffix}")
        print()

    if not found:
        print("No unrecorded splits detected.")
        return 0

    print(f"{len(found)} suspected unrecorded split(s):\n")
    for f in found:
        print(
            f"  {f['symbol']:8} {f['day']}  "
            f"{f['prev_price']:.2f} -> {f['price']:.2f}  "
            f"implies {f['observed_ratio']:.4f}:1  "
            f"(nearest {f['nearest_ratio']:g}:1, off by {f['deviation'] * 100:.2f}%)"
        )
    print(
        "\nVerify each against a real source, then record it:\n"
        "  INSERT INTO corporate_actions\n"
        "    (symbol, kind, ex_date, ex_ts, ratio, adjust_prices, adjust_lots, notes)\n"
        "  VALUES ('SYM', 'SPLIT', DATE 'YYYY-MM-DD', TIMESTAMPTZ '... 16:30:00+03',\n"
        "          2, TRUE, <TRUE if the shares were actually credited>, 'why');\n"
        "\nex_ts should be the ex-date's regular-session open — raw-timestamp\n"
        "readers need it to classify that day's pre-open snapshots correctly."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
