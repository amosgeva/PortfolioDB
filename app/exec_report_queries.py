"""SQL for the executive report — every read it makes, and nothing else.

Each function takes an open connection and returns plain Python values. Lots
are not read here: the report takes them from `ledger_inputs.load()` like every
other valuation (see CLAUDE.md, "Prepared ledger"), and its quotes are restated
through the same prepared ledger in `exec_report.gather()`.

Split out of `exec_report.py` (audit R-4); `exec_report` re-exports these under
their old underscore names.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta

import reporting_tz
from db import fetch_all


def fetch_latest_prices(conn) -> dict[str, dict]:
    """Newest quote per symbol, with its timestamp."""
    rows = fetch_all(
        conn,
        """
        SELECT ps.symbol, ps.ts, ps.last_price
        FROM price_snapshots ps
        JOIN (
          SELECT symbol, MAX(ts) AS ts
          FROM price_snapshots
          GROUP BY symbol
        ) m ON m.symbol = ps.symbol AND m.ts = ps.ts
        """,
    )
    return {
        r["symbol"]: {
            "ts": r["ts"],
            "last_price": float(r["last_price"]) if r["last_price"] is not None else None,
        }
        for r in rows
    }


def fetch_eod_by_day(conn) -> dict[date, dict[str, float]]:
    """Last quote per symbol per reporting-local day."""
    tz_name = reporting_tz.tz_name()
    rows = fetch_all(
        conn,
        """
        WITH daily AS (
          SELECT symbol,
                 (ts AT TIME ZONE %s)::date AS d,
                 last_price,
                 ROW_NUMBER() OVER (
                   PARTITION BY symbol, (ts AT TIME ZONE %s)::date
                   ORDER BY ts DESC
                 ) AS rn
          FROM price_snapshots
        )
        SELECT symbol, d, last_price
        FROM daily
        WHERE rn = 1
        ORDER BY d, symbol
        """,
        (tz_name, tz_name),
    )
    by_day: dict[date, dict[str, float]] = defaultdict(dict)
    for r in rows:
        if r["last_price"] is not None:
            by_day[r["d"]][r["symbol"]] = float(r["last_price"])
    return dict(by_day)


def fetch_latest_brief(conn) -> dict | None:
    rows = fetch_all(
        conn,
        """
        SELECT id, ts, kind, total_value, payload
        FROM advisor_briefs
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not rows:
        return None
    r = rows[0]
    payload = r["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    r["payload"] = payload or {}
    return r


def fetch_fd_metrics(conn) -> dict[str, dict]:
    rows = fetch_all(
        conn,
        """
        SELECT symbol, pe_ratio, revenue_growth, market_cap, net_margin,
               earnings_growth, fetched_at
        FROM fd_financial_metrics
        """,
    )
    return {r["symbol"]: r for r in rows}


def fetch_earnings_window(conn, symbols: list[str], past_days: int = 60, upcoming_days: int = 45) -> tuple[list[dict], list[dict]]:
    """Past actual earnings + computed next-expected (filing cadence ~90d)."""
    if not symbols:
        return [], []

    today = date.today()
    past_cutoff = today - timedelta(days=past_days)

    past = fetch_all(
        conn,
        """
        SELECT DISTINCT ON (symbol, report_period)
               symbol, report_period, filing_date, fiscal_period,
               eps_actual, eps_estimate, eps_surprise, eps_surprise_pct,
               revenue_actual, revenue_estimate, revenue_surprise, revenue_surprise_pct
        FROM fd_earnings
        WHERE symbol = ANY(%s)
          AND filing_date IS NOT NULL
          AND filing_date >= %s
        ORDER BY symbol, report_period DESC, filing_date DESC
        """,
        (symbols, past_cutoff),
    )
    past.sort(key=lambda r: r["filing_date"] or date.min, reverse=True)

    # For upcoming: take latest filing per symbol and project +90d.
    latest_per = fetch_all(
        conn,
        """
        SELECT DISTINCT ON (symbol) symbol, report_period, filing_date, fiscal_period
        FROM fd_earnings
        WHERE symbol = ANY(%s) AND filing_date IS NOT NULL
        ORDER BY symbol, filing_date DESC, report_period DESC
        """,
        (symbols,),
    )

    upcoming_cutoff = today + timedelta(days=upcoming_days)
    upcoming = []
    for r in latest_per:
        cadence_days = 90 if (r["fiscal_period"] or "").upper().startswith("Q") else 365
        expected = r["filing_date"] + timedelta(days=cadence_days)
        if today - timedelta(days=14) <= expected <= upcoming_cutoff:
            upcoming.append({
                "symbol": r["symbol"],
                "expected": expected,
                "last_filing": r["filing_date"],
                "last_period": r["report_period"],
                "cadence_days": cadence_days,
                "overdue": expected < today,
            })
    upcoming.sort(key=lambda r: r["expected"])
    return past, upcoming


def fetch_cash(conn) -> list[dict]:
    """Latest cash entry per account, with when it was entered and its currency."""
    return fetch_all(
        conn,
        """
        SELECT DISTINCT ON (account) account, cash, ts, currency
        FROM cash_snapshots
        ORDER BY account, ts DESC
        """,
    )


def fetch_income(conn) -> list[dict]:
    """Every income record's amount, date and currency (for the USD totals)."""
    return fetch_all(conn, "SELECT symbol, pay_date, amount, currency FROM income")


def fetch_instruments(conn) -> list[dict]:
    """Registry flags the report needs: currency, and whether a symbol is a
    market-overview benchmark (context, never a holding: its quotes must not
    decide the report's "as of" or its previous session, audit F-8)."""
    return fetch_all(conn, "SELECT symbol, currency, benchmark FROM instruments")
