"""Regression tests for the dedupe unique indexes and latest-cash query.

Guards two bugs found in the 2026-07 review:

1. ``lots_dedupe_idx`` / ``income_dedupe_idx`` must include the discriminating
   column (``side`` / ``kind``). Without it, a BUY and a SELL (or a DIVIDEND
   and an INTEREST) sharing symbol/account/date/qty/price collide and the
   ``ON CONFLICT DO NOTHING`` inserts used by every CLI silently drop the
   second row.

2. "Latest cash per account" must use ``DISTINCT ON (account)``, not a global
   ``ORDER BY ts DESC LIMIT N`` — the latter starves accounts whose newest
   snapshot falls outside the first N rows.

Integration tests against a real Postgres (the project's docker-compose),
following the test_fd_store.py pattern: sentinel symbol/accounts, cleanup
between tests, module-level skip when the DB is unreachable.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import connect, load_config  # noqa: E402

TEST_SYMBOL = "TST_DEDUP"
TEST_ACCOUNTS = ("TSTACC_A", "TSTACC_B")

LOT_INSERT = """
    INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees)
    VALUES (%s, %s, %s, %s, %s, %s, 0)
    ON CONFLICT DO NOTHING
"""

INCOME_INSERT = """
    INSERT INTO income(symbol, account, kind, pay_date, amount)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT DO NOTHING
"""

# Same query production uses (streamlit_app.py, mcp/services/kpis.py,
# mcp/services/activity.py) — asserted here because the MCP unit suite's
# FakeCursor never executes real SQL.
LATEST_CASH_QUERY = """
    SELECT DISTINCT ON (account) account, cash, ts
    FROM cash_snapshots
    ORDER BY account, ts DESC
"""


def _wipe(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM lots WHERE symbol = %s", (TEST_SYMBOL,))
        cur.execute("DELETE FROM income WHERE symbol = %s", (TEST_SYMBOL,))
        cur.execute(
            "DELETE FROM cash_snapshots WHERE account IN %s", (TEST_ACCOUNTS,)
        )
        cur.execute("DELETE FROM instruments WHERE symbol = %s", (TEST_SYMBOL,))
    conn.commit()


@pytest.fixture(scope="module")
def conn():
    try:
        cfg = load_config()
    except Exception as e:
        pytest.skip(f"DB config unavailable: {e}")
    try:
        c = connect(cfg)
    except Exception as e:
        pytest.skip(f"DB unreachable: {e}")
    try:
        yield c
    finally:
        try:
            _wipe(c)
        finally:
            c.close()


@pytest.fixture(autouse=True)
def _clean_between_tests(conn):
    _wipe(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT DO NOTHING",
            (TEST_SYMBOL,),
        )
    conn.commit()
    yield


def _lot_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM lots WHERE symbol = %s", (TEST_SYMBOL,))
        return cur.fetchone()[0]


# ───────────────────────── lots dedupe ─────────────────────────

def test_buy_and_sell_same_params_both_persist(conn):
    """A same-day round-trip (BUY then SELL, identical qty/price) is two rows."""
    args = (TEST_SYMBOL, "IBKR", "2026-07-01", 5, 100)
    with conn.cursor() as cur:
        cur.execute(LOT_INSERT, (args[0], args[1], "BUY", *args[2:]))
        cur.execute(LOT_INSERT, (args[0], args[1], "SELL", *args[2:]))
    conn.commit()
    assert _lot_count(conn) == 2


def test_identical_duplicate_lot_rejected(conn):
    with conn.cursor() as cur:
        for _ in range(2):
            cur.execute(LOT_INSERT, (TEST_SYMBOL, "IBKR", "BUY", "2026-07-01", 5, 100))
    conn.commit()
    assert _lot_count(conn) == 1


def test_null_account_dedupe_still_applies(conn):
    """COALESCE(account,'') in the index must dedupe NULL-account rows too."""
    with conn.cursor() as cur:
        for _ in range(2):
            cur.execute(LOT_INSERT, (TEST_SYMBOL, None, "BUY", "2026-07-01", 5, 100))
    conn.commit()
    assert _lot_count(conn) == 1


# ───────────────────────── income dedupe ─────────────────────────

def test_income_different_kinds_both_persist(conn):
    with conn.cursor() as cur:
        cur.execute(INCOME_INSERT, (TEST_SYMBOL, "IBKR", "DIVIDEND", "2026-07-01", 12.34))
        cur.execute(INCOME_INSERT, (TEST_SYMBOL, "IBKR", "INTEREST", "2026-07-01", 12.34))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM income WHERE symbol = %s", (TEST_SYMBOL,))
        assert cur.fetchone()[0] == 2


def test_identical_income_rejected(conn):
    with conn.cursor() as cur:
        for _ in range(2):
            cur.execute(INCOME_INSERT, (TEST_SYMBOL, "IBKR", "DIVIDEND", "2026-07-01", 12.34))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM income WHERE symbol = %s", (TEST_SYMBOL,))
        assert cur.fetchone()[0] == 1


# ───────────────────────── latest cash per account ─────────────────────────

def test_latest_cash_survives_burst_of_other_account_rows(conn):
    """Account A's latest cash must be returned even when another account has
    written many newer snapshots (the old ORDER BY ts DESC LIMIT N dropped it)."""
    base = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    acct_a, acct_b = TEST_ACCOUNTS
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cash_snapshots(account, cash, ts) VALUES (%s, %s, %s)",
            (acct_a, 111, base - timedelta(days=30)),
        )
        cur.execute(
            "INSERT INTO cash_snapshots(account, cash, ts) VALUES (%s, %s, %s)",
            (acct_a, 222, base - timedelta(days=29)),  # A's true latest
        )
        for i in range(300):  # newer burst from B, > the old LIMIT 200
            cur.execute(
                "INSERT INTO cash_snapshots(account, cash, ts) VALUES (%s, %s, %s)",
                (acct_b, 1, base + timedelta(minutes=i)),
            )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(LATEST_CASH_QUERY)
        latest = {acct: (cash, ts) for acct, cash, ts in cur.fetchall()}

    assert acct_a in latest, "starved account must still be reported"
    assert float(latest[acct_a][0]) == 222.0, "must be the account's newest row"
    assert float(latest[acct_b][0]) == 1.0
