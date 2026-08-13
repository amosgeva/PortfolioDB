"""All dashboard SQL, one function per query surface.

Every function takes an open psycopg2 connection and returns plain rows
(list[dict] via db.fetch_all). No Streamlit, no caching, no presentation —
that lives in payload.py / streamlit_app.py.
"""

from __future__ import annotations

import reporting_tz
from db import fetch_all

TZ_NAME = reporting_tz.tz_name()


def latest_prices(conn) -> list[dict]:
    """Latest snapshot row per symbol."""
    return fetch_all(
        conn,
        """
        SELECT ps.symbol, ps.ts, ps.last_price, ps.bid, ps.ask, ps.source
        FROM price_snapshots ps
        JOIN (
          SELECT symbol, MAX(ts) AS ts FROM price_snapshots GROUP BY symbol
        ) m ON m.symbol = ps.symbol AND m.ts = ps.ts
        ORDER BY ps.symbol;
        """,
    )


def prev_close(conn) -> list[dict]:
    """Per symbol: the last price of the trading session BEFORE the symbol's
    latest snapshot — the 'previous close' baseline for day-change math.

    Anchoring to the latest snapshot's session day (not to "today") means the
    day change always reflects the latest session's move. The old
    "newest price strictly before today" version collapsed to the latest
    snapshot itself whenever today had no snapshots yet (pre-market, weekends,
    holidays), silently showing a 0.00% day change."""
    return fetch_all(
        conn,
        """
        WITH latest AS (
          SELECT symbol, MAX(ts) AS ts FROM price_snapshots GROUP BY symbol
        ),
        prev AS (
          SELECT ps.symbol, ps.last_price,
                 ROW_NUMBER() OVER (PARTITION BY ps.symbol ORDER BY ps.ts DESC) AS rn
          FROM price_snapshots ps
          JOIN latest l ON l.symbol = ps.symbol
          WHERE (ps.ts AT TIME ZONE %s)::date < (l.ts AT TIME ZONE %s)::date
        )
        SELECT symbol, last_price FROM prev WHERE rn = 1;
        """,
        (TZ_NAME, TZ_NAME),
    )


def all_lots(conn) -> list[dict]:
    """Every lot, in FIFO processing order."""
    return fetch_all(
        conn,
        """
        SELECT id, symbol, account, side, trade_date, quantity, price, fees
        FROM lots
        ORDER BY symbol, COALESCE(account,''), trade_date, id
        """,
    )


def price_history(conn, days: int = 370) -> list[dict]:
    """Snapshot series for sparklines / PV series / per-symbol charts."""
    return fetch_all(
        conn,
        """
        SELECT ts, symbol, last_price
        FROM price_snapshots
        WHERE ts >= now() - make_interval(days => %s)
        ORDER BY ts
        """,
        (days,),
    )


def company_facts(conn) -> list[dict]:
    return fetch_all(conn, "SELECT symbol, name, sector FROM fd_company_facts")


def watchlist_symbols(conn) -> list[dict]:
    return fetch_all(
        conn, "SELECT symbol FROM instruments WHERE watchlist = TRUE ORDER BY symbol"
    )


def latest_cash_per_account(conn) -> list[dict]:
    # Latest row per account — a global ORDER BY ts LIMIT N would drop
    # accounts whose newest snapshot falls outside the first N rows.
    return fetch_all(
        conn,
        "SELECT DISTINCT ON (account) account, cash, ts FROM cash_snapshots ORDER BY account, ts DESC",
    )


def income_rows(conn) -> list[dict]:
    return fetch_all(conn, "SELECT pay_date, amount FROM income")


def instrument_attrs(conn) -> list[dict]:
    return fetch_all(
        conn, "SELECT symbol, asset_type, currency, country, sector FROM instruments"
    )


def second_latest_prices(conn) -> list[dict]:
    """Per symbol: the snapshot right before the latest one (Δ vs last tick)."""
    return fetch_all(
        conn,
        """
        WITH ranked AS (
          SELECT symbol, last_price,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
          FROM price_snapshots
        )
        SELECT symbol, last_price FROM ranked WHERE rn = 2;
        """,
    )


def recent_lots(conn, limit: int = 200) -> list[dict]:
    return fetch_all(
        conn,
        """
        SELECT id, symbol, account, side, trade_date, quantity, price, fees, notes
        FROM lots ORDER BY trade_date DESC, id DESC LIMIT %s
        """,
        (limit,),
    )


def snapshot_log(conn, limit: int = 30) -> list[dict]:
    return fetch_all(
        conn,
        """
        SELECT ts, COUNT(*) AS symbols, source
        FROM price_snapshots GROUP BY ts, source ORDER BY ts DESC LIMIT %s
        """,
        (limit,),
    )


def last_snapshot_run(conn) -> list[dict]:
    return fetch_all(
        conn,
        """
        SELECT ts_start, ts_end, status, symbols_total, symbols_ok, symbols_failed
        FROM snapshot_runs ORDER BY id DESC LIMIT 1
        """,
    )


def news_max_fetched_at(conn):
    """Newest fd_news.fetched_at (None if the table is empty)."""
    rows = fetch_all(conn, "SELECT MAX(fetched_at) AS m FROM fd_news")
    return rows[0]["m"] if rows else None
