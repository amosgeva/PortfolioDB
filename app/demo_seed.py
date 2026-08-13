"""Seed a fictional portfolio so a fresh install has something to look at.

Everything here is invented: the holdings, the quantities, the trade dates, the
prices (a seeded random walk — no network calls, no vendor data). Real tickers
are used only so the symbols look familiar and the benchmark comparison has a
SPY series to read; the positions are not anybody's portfolio.

Usage (from a host with the venv, or inside the container):

    python app/demo_seed.py --yes
    docker compose run --rm dashboard python app/demo_seed.py --yes

Idempotent: every insert is ON CONFLICT DO NOTHING, so re-running adds nothing.
It refuses to touch a database that already holds non-demo lots unless
--force is passed, so it cannot quietly contaminate a real ledger.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, datetime, timedelta, timezone

from db import fetch_all, load_config, run, transaction

DEMO_NOTE = "demo-seed"

# symbol -> (name, asset_type, sector, country, start_price, annual_drift, daily_vol)
UNIVERSE = {
    "AAPL": ("Apple Inc.", "stock", "Technology", "US", 185.0, 0.18, 0.014),
    "MSFT": ("Microsoft Corp.", "stock", "Technology", "US", 395.0, 0.16, 0.012),
    "NVDA": ("NVIDIA Corp.", "stock", "Technology", "US", 118.0, 0.35, 0.026),
    "JNJ": ("Johnson & Johnson", "stock", "Healthcare", "US", 152.0, 0.04, 0.009),
    "VOO": ("Vanguard S&P 500 ETF", "etf", "Broad Market", "US", 505.0, 0.10, 0.008),
    "SPY": ("SPDR S&P 500 ETF", "etf", "Broad Market", "US", 548.0, 0.10, 0.008),
    "AMD": ("Advanced Micro Devices", "stock", "Technology", "US", 142.0, 0.12, 0.024),
}

# Held on the watchlist only — no lots, but still snapshotted. Exercises the
# "watchlist symbol with no position" path in the dashboard and collector.
WATCHLIST_ONLY = {"AMD"}

# (symbol, account, side, days_ago, quantity, price_factor, fees)
# price_factor multiplies that day's simulated close, so a lot's price is
# plausible against the series without hardcoding numbers.
TRADES = [
    ("VOO", "Broker A", "BUY", 175, 12, 1.000, 0.00),
    ("AAPL", "Broker A", "BUY", 168, 40, 0.998, 1.25),
    ("MSFT", "Broker A", "BUY", 150, 15, 1.004, 1.25),
    ("NVDA", "Broker B", "BUY", 132, 60, 0.995, 2.50),
    ("JNJ", "Broker B", "BUY", 120, 55, 1.001, 1.25),
    ("VOO", "Broker A", "BUY", 84, 8, 1.002, 0.00),
    ("AAPL", "Broker A", "BUY", 63, 25, 1.006, 1.25),
    # A partial exit, so realized P&L and the FIFO matching have something to
    # chew on rather than every position being untouched.
    ("NVDA", "Broker B", "SELL", 35, 20, 1.010, 2.50),
    ("SPY", "Broker A", "BUY", 28, 6, 1.000, 0.00),
]

CASH = [("Broker A", 4820.55), ("Broker B", 1290.00)]

# A fictional brief, so the Advisor tab shows its real shape on a fresh install.
# Written by hand on purpose: seeding must not need an API key, spend money, or
# touch the network. The text is deliberately generic and is not advice.
DEMO_BRIEF = {
    "summary": (
        "Two technology names now carry 38% of the book between them, and the "
        "cash sleeve has drifted to 8% after last month's additions. Nothing "
        "here needs action today; the concentration is the thing worth watching."
    ),
    "insights": [
        {
            "title": "Concentration is creeping up",
            "body": "AAPL and NVDA together are 38% of market value, up from 31% "
                    "in April. That is the largest single-sector exposure this "
                    "portfolio has carried.",
            "tag": "concentration",
        },
        {
            "title": "Realized gains are small so far",
            "body": "Only one partial exit this year (NVDA, +$300). Most of the "
                    "return is unrealized, so the tax picture is still open.",
            "tag": "tax",
        },
        {
            "title": "Cash is doing its job",
            "body": "8% in cash across both accounts — enough to add on weakness "
                    "without selling anything.",
            "tag": "cash",
        },
    ],
    "suggestions": [
        {
            "action": "Decide a concentration ceiling before it is tested",
            "rationale": "Writing the limit down now is easier than choosing one "
                         "during a drawdown.",
            "rule_invoked": "Core Philosophy — position sizing",
        }
    ],
    "markdown": (
        "**Portfolio brief — demo data**\n\n"
        "Net asset value is roughly $56k across two accounts, up 15% since the "
        "first lot in February. The shape of the book matters more than the "
        "number today:\n\n"
        "- **AAPL and NVDA are 38% combined.** That is concentration by "
        "accumulation rather than by decision — three separate additions, none "
        "of them large.\n"
        "- **JNJ is the quiet anchor**, 19% and the least volatile holding.\n"
        "- **Cash at 8%** leaves room to act without forced selling.\n\n"
        "Nothing in the ledger asks for a trade today. The open question is what "
        "concentration you are willing to hold deliberately, written down before "
        "the market makes the decision for you.\n\n"
        "_Demo content generated for a fictional portfolio — not investment advice._"
    ),
}

TRADING_DAYS = 180          # calendar days of history to simulate
SNAPSHOT_HOUR_UTC = 20      # ~US close, in UTC


def simulate_prices(seed: int = 20260813) -> dict[str, list[tuple[datetime, float]]]:
    """{symbol: [(ts, close)]} — one point per weekday, deterministic."""
    # Seeded on purpose: the same seed must reproduce the same demo portfolio,
    # which a cryptographic generator cannot do. Nothing here is a secret —
    # they are invented prices — so the "insecure RNG" warnings don't apply.
    rng = random.Random(seed)  # nosec B311  # nosemgrep
    now = datetime.now(timezone.utc)
    today = now.replace(hour=SNAPSHOT_HOUR_UTC, minute=0, second=0, microsecond=0)
    out: dict[str, list[tuple[datetime, float]]] = {}
    for sym, (_n, _a, _s, _c, start, drift, vol) in UNIVERSE.items():
        price = start
        series: list[tuple[datetime, float]] = []
        for offset in range(TRADING_DAYS, -1, -1):
            ts = today - timedelta(days=offset)
            if ts.weekday() >= 5:          # weekends have no session
                continue
            if ts > now:
                # Today's "close" hasn't happened yet if we're seeding in the
                # morning; a future-dated snapshot is excluded by every cutoff
                # read anyway, and looks like a bug in the freshness panel.
                continue
            # Geometric-ish walk: a small daily drift plus symbol-specific noise.
            price *= 1 + drift / 252 + rng.gauss(0, vol)
            price = max(price, 1.0)
            series.append((ts, round(price, 2)))
        out[sym] = series
    return out


def close_on(series: list[tuple[datetime, float]], day: date) -> float:
    """Simulated close for a given day, or the nearest earlier one."""
    candidates = [p for ts, p in series if ts.date() <= day]
    return candidates[-1] if candidates else series[0][1]


def seed(conn, prices: dict[str, list[tuple[datetime, float]]]) -> dict[str, int]:
    counts = {"instruments": 0, "lots": 0, "snapshots": 0, "cash": 0}

    for sym, (name, asset_type, sector, country, *_rest) in UNIVERSE.items():
        run(
            conn,
            """
            INSERT INTO instruments(symbol, name, asset_type, sector, country, watchlist)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol) DO NOTHING
            """,
            (sym, name, asset_type, sector, country, sym in WATCHLIST_ONLY),
        )
        counts["instruments"] += 1

    for sym, series in prices.items():
        for ts, price in series:
            spread = round(price * 0.0004, 4)
            run(
                conn,
                """
                INSERT INTO price_snapshots(ts, symbol, last_price, bid, ask, source)
                VALUES (%s, %s, %s, %s, %s, 'demo')
                ON CONFLICT DO NOTHING
                """,
                (ts, sym, price, round(price - spread, 4), round(price + spread, 4)),
            )
            counts["snapshots"] += 1

    today = datetime.now(timezone.utc).date()
    for sym, account, side, days_ago, qty, factor, fees in TRADES:
        trade_date = today - timedelta(days=days_ago)
        price = round(close_on(prices[sym], trade_date) * factor, 4)
        run(
            conn,
            """
            INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (sym, account, side, trade_date, qty, price, fees, DEMO_NOTE),
        )
        counts["lots"] += 1

    for account, cash in CASH:
        run(
            conn,
            """
            INSERT INTO cash_snapshots(ts, account, cash, note)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (datetime.now(timezone.utc), account, cash, DEMO_NOTE),
        )
        counts["cash"] += 1

    # A completed collector run, so freshness checks have a reference point
    # instead of reporting every symbol as never-collected.
    last_ts = max(series[-1][0] for series in prices.values())
    run(
        conn,
        """
        INSERT INTO snapshot_runs(ts_start, ts_end, status, symbols_total, symbols_ok, symbols_failed)
        VALUES (%s, %s, 'ok', %s, %s, 0)
        """,
        (last_ts, last_ts + timedelta(seconds=9), len(UNIVERSE), len(UNIVERSE)),
    )

    # One advisor brief, so the Advisor tab shows what it looks like in use
    # rather than an empty state. Written here rather than generated: seeding
    # must not require an API key, cost money, or reach the network.
    run(
        conn,
        """
        INSERT INTO advisor_briefs(ts, kind, total_value, payload)
        VALUES (%s, 'morning', %s, %s::jsonb)
        """,
        (datetime.now(timezone.utc), 56000.00, json.dumps(DEMO_BRIEF)),
    )
    counts["briefs"] = 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true", help="Confirm writing demo data.")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Seed even if the database already holds lots that aren't demo lots.",
    )
    args = ap.parse_args()

    if not args.yes:
        print("Refusing to write without --yes. This inserts a fictional portfolio.")
        return 2

    cfg = load_config()
    with transaction(cfg) as conn:
        existing = fetch_all(
            conn,
            "SELECT COUNT(*) AS n FROM lots WHERE notes IS DISTINCT FROM %s",
            (DEMO_NOTE,),
        )
        real_lots = int(existing[0]["n"]) if existing else 0
        if real_lots and not args.force:
            print(
                f"Refusing to seed: {real_lots} non-demo lot(s) already present. "
                "This looks like a real ledger — pass --force only if you are sure."
            )
            return 1

        prices = simulate_prices()
        counts = seed(conn, prices)

    print(
        "Seeded fictional demo data: "
        f"{counts['instruments']} instruments, {counts['lots']} lots, "
        f"{counts['snapshots']} price snapshots, {counts['cash']} cash rows, "
        f"{counts.get('briefs', 0)} advisor brief."
    )
    print("Prices are a seeded random walk — not market data.")
    print(
        "Note: the scheduler collects *real* quotes for these tickers, which will "
        "sit on top of this invented history and show up as one enormous day "
        "move. Stop the scheduler (or set a collector window that is closed) if "
        "you want the demo to stay self-consistent — e.g. for screenshots."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
