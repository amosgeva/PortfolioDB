"""Snapshot selection and the unrealized-percent guard in report_portfolio_db.

The report failed with `ZeroDivisionError: float division by zero` in both
--mode daily and --mode eod. Two independent defects lined up:

1. `get_snapshot` took `MAX(ts)` over the whole of price_snapshots. Since 1.1.0
   that table also holds Markets-strip benchmarks (index futures), collected
   around the clock while the held symbols stop after the US close — so for
   most of the day the newest row is a benchmark-only instant at which nothing
   in the portfolio has a price.

2. With *no* symbol priced, `last_price` is all-None, which makes the derived
   columns object dtype. Pandas then divides element-wise in Python, where
   x / 0.0 raises instead of returning inf — and open_cost is 0 for every
   fully-closed position.

Either alone is survivable: with one real price the column stays float64 and
the zero divisor merely yields `inf`. That is why this looked intermittent, and
why the report printed "inf%" for closed positions on the runs that worked.

The sqlite fake below runs the real SQL rather than asserting on its text, so
these test the behaviour and not the wording.
"""

import os
import sqlite3
import sys
from datetime import datetime

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import report_portfolio_db as rp


# ───────────────────────── the percent guard ─────────────────────────

def test_pct_is_the_plain_ratio_when_there_is_a_cost():
    out = rp.unrealized_pct(pd.Series([25.0, -10.0]), pd.Series([100.0, 200.0]))
    assert list(out) == [25.0, -5.0]


def test_zero_cost_yields_nan_not_inf():
    """A closed position has no cost to compare against; inf% is not a return."""
    out = rp.unrealized_pct(pd.Series([5.0, 0.0]), pd.Series([100.0, 0.0]))
    assert out.iloc[0] == 5.0
    assert pd.isna(out.iloc[1])
    assert not any(out.abs() == float("inf")), "inf leaked into a percentage"


def test_all_none_prices_with_a_zero_cost_does_not_raise():
    """The exact crash: object dtype + a zero divisor.

    When no symbol in the frame has a price, every derived column is object
    dtype, and object-dtype division runs in Python where x/0 raises.
    """
    pnl = pd.Series([None, None], dtype=object)
    cost = pd.Series([100.0, 0.0])
    out = rp.unrealized_pct(pnl, cost)
    assert pd.isna(out).all()


def test_mixed_priced_and_unpriced_still_works():
    pnl = pd.Series([12.0, None], dtype=object)
    cost = pd.Series([100.0, 0.0])
    out = rp.unrealized_pct(pnl, cost)
    assert out.iloc[0] == 12.0
    assert pd.isna(out.iloc[1])


# ───────────────────────── snapshot selection ─────────────────────────

def _sqlite_conn():
    """A tiny stand-in for the two tables get_snapshot touches."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE instruments (symbol TEXT PRIMARY KEY, benchmark INT NOT NULL DEFAULT 0)")
    conn.execute("CREATE TABLE price_snapshots (symbol TEXT, ts TEXT, last_price REAL, bid REAL, ask REAL, source TEXT)")
    return conn


def _seed(conn, rows, benchmarks=("ES=F", "NQ=F", "YM=F")):
    symbols = {s for s, _ts in rows}
    for s in symbols:
        conn.execute("INSERT INTO instruments (symbol, benchmark) VALUES (?, ?)",
                     (s, 1 if s in benchmarks else 0))
    for s, ts in rows:
        conn.execute(
            "INSERT INTO price_snapshots (symbol, ts, last_price, bid, ask, source)"
            " VALUES (?, ?, 1.0, NULL, NULL, 'test')", (s, ts))
    conn.commit()


@pytest.fixture
def fake_fetch_all(monkeypatch):
    """Route report_portfolio_db.fetch_all at sqlite, running the real SQL."""
    def _install(conn):
        def fetch_all(_conn, sql, params=None):
            args = tuple(
                p.isoformat() if isinstance(p, datetime) else p
                for p in (params or ())
            )
            cur = conn.execute(sql.replace("%s", "?"), args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        monkeypatch.setattr(rp, "fetch_all", fetch_all)
    return _install


def test_daily_skips_a_benchmark_only_latest_snapshot(fake_fetch_all):
    """The defect, stated directly.

    Futures tick at 09:30 today; the holdings were last priced at 23:13 last
    night. MAX(ts) over the whole table names the futures instant, at which no
    holding has a price.
    """
    conn = _sqlite_conn()
    _seed(conn, [
        ("NVDA", "2026-08-31T23:13:00+00:00"),
        ("SPY",  "2026-08-31T23:13:00+00:00"),
        ("ES=F", "2026-09-01T09:30:00+00:00"),   # newer, but benchmark-only
        ("NQ=F", "2026-09-01T09:30:00+00:00"),
    ])
    fake_fetch_all(conn)

    snap = rp.get_snapshot(conn, mode="daily")
    assert snap.ts == "2026-08-31T23:13:00+00:00"
    assert set(snap.prices) == {"NVDA", "SPY"}


def test_daily_uses_a_mixed_snapshot_when_that_is_newest(fake_fetch_all):
    """A benchmark sharing a timestamp with holdings must not disqualify it."""
    conn = _sqlite_conn()
    _seed(conn, [
        ("NVDA", "2026-08-31T20:00:00+00:00"),
        ("NVDA", "2026-08-31T21:00:00+00:00"),
        ("ES=F", "2026-08-31T21:00:00+00:00"),
    ])
    fake_fetch_all(conn)

    snap = rp.get_snapshot(conn, mode="daily")
    assert snap.ts == "2026-08-31T21:00:00+00:00"
    assert set(snap.prices) == {"NVDA", "ES=F"}


def test_eod_still_respects_its_upper_bound(fake_fetch_all, monkeypatch):
    """The benchmark exclusion must not swallow the `ts <= target` clause."""
    conn = _sqlite_conn()
    _seed(conn, [
        ("NVDA", "2020-01-01T10:00:00+00:00"),
        ("NVDA", "2099-01-01T10:00:00+00:00"),   # far future, must be excluded
    ])
    fake_fetch_all(conn)

    snap = rp.get_snapshot(conn, mode="eod")
    assert snap.ts == "2020-01-01T10:00:00+00:00"


def test_no_portfolio_snapshot_at_all_is_an_error_not_a_crash(fake_fetch_all):
    """Benchmarks only, ever: say so rather than failing later on None."""
    conn = _sqlite_conn()
    _seed(conn, [("ES=F", "2026-09-01T09:30:00+00:00")])
    fake_fetch_all(conn)

    with pytest.raises(RuntimeError, match="No price snapshots found"):
        rp.get_snapshot(conn, mode="daily")
