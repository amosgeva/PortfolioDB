"""All dashboard SQL, one function per query surface.

Every function takes an open psycopg2 connection and returns plain rows
(list[dict] via db.fetch_all). No Streamlit, no caching, no presentation —
that lives in payload.py / streamlit_app.py.
"""

from __future__ import annotations

from psycopg2 import errors as psycopg2_errors

import reporting_tz
from db import fetch_all

TZ_NAME = reporting_tz.tz_name()


# Every price read below skips last_price <= 0: Yahoo answers 0.0 outside
# regular hours and the collector has stored it, and a zero is not a price —
# it reads as a −100% day and zeroes a TWR curve for good (audit F-15). The
# collector-side guard is W2a's; the readers refuse it too.


def latest_prices(conn) -> list[dict]:
    """Latest positive snapshot row per symbol."""
    return fetch_all(
        conn,
        """
        SELECT ps.symbol, ps.ts, ps.last_price, ps.bid, ps.ask, ps.source
        FROM price_snapshots ps
        JOIN (
          SELECT symbol, MAX(ts) AS ts FROM price_snapshots
          WHERE last_price > 0 GROUP BY symbol
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
          SELECT symbol, MAX(ts) AS ts FROM price_snapshots
          WHERE last_price > 0 GROUP BY symbol
        ),
        prev AS (
          SELECT ps.symbol, ps.ts, ps.last_price,
                 ROW_NUMBER() OVER (PARTITION BY ps.symbol ORDER BY ps.ts DESC) AS rn
          FROM price_snapshots ps
          JOIN latest l ON l.symbol = ps.symbol
          WHERE ps.last_price > 0
            AND (ps.ts AT TIME ZONE %s)::date < (l.ts AT TIME ZONE %s)::date
        )
        SELECT symbol, ts, last_price FROM prev WHERE rn = 1;
        """,
        (TZ_NAME, TZ_NAME),
    )


# Lots are not queried here on purpose: every valuation reads them restated
# through app/ledger_inputs.py (see CLAUDE.md, "Prepared ledger"). The one raw
# lot reader left in this module is recent_lots, the Manage page's
# trade-history table, which shows what the operator entered.


def price_history(conn, days: int = 370) -> list[dict]:
    """Snapshot series for sparklines / PV series / per-symbol charts."""
    return fetch_all(
        conn,
        """
        SELECT ts, symbol, last_price
        FROM price_snapshots
        WHERE ts >= now() - make_interval(days => %s) AND last_price > 0
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
        "SELECT DISTINCT ON (account) account, cash, ts, currency "
        "FROM cash_snapshots ORDER BY account, ts DESC",
    )


def income_rows(conn) -> list[dict]:
    # Currency travels with the amount: a dividend recorded in another
    # currency must not be summed as dollars (audit Q-8). payload.py splits
    # the rows by it.
    return fetch_all(conn, "SELECT symbol, pay_date, amount, currency FROM income")


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
          SELECT symbol, ts, last_price,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
          FROM price_snapshots
          WHERE last_price > 0
        )
        SELECT symbol, ts, last_price FROM ranked WHERE rn = 2;
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


def first_snapshot_run_ts(conn):
    """When run tracking began, or None if nothing has ever been recorded.

    Before this instant the collector demonstrably was not running, so no
    collection was owed and a hole in the price series is just sparse history
    rather than a miss. snapshot_runs was added after collection had already
    been going, which is why this is not simply the start of the data.
    """
    rows = fetch_all(conn, "SELECT MIN(ts_start) AS ts FROM snapshot_runs")
    return rows[0]["ts"] if rows else None


def price_alerts(conn) -> list[dict]:
    """Active price alerts, oldest first; [] when the table does not exist.

    `price_alerts` arrives with migration 006. A database that has not had it
    applied reads as "no alerts" so the dashboard still renders. That is the
    only failure absorbed — the same rule as corporate_actions.fetch_actions,
    and for the same reason: a transient error or a permissions slip that
    silently read as "no alerts" would hide armed alerts from the operator,
    which is worse than an error.
    """
    try:
        return fetch_all(
            conn,
            """
            SELECT id, symbol, direction, level, note, created_at
            FROM price_alerts
            WHERE active
            ORDER BY created_at, id
            """,
        )
    except psycopg2_errors.UndefinedTable:
        conn.rollback()
        return []
    except Exception:
        conn.rollback()
        raise


def news_max_fetched_at(conn):
    """Newest fd_news.fetched_at (None if the table is empty)."""
    rows = fetch_all(conn, "SELECT MAX(fetched_at) AS m FROM fd_news")
    return rows[0]["m"] if rows else None
