"""Active positions and the day change, dashboard vs MCP convention, on Postgres.

Found by QA of the MCP workstream (#126): with one priced and one unpriced
holding the dashboard payload said `activeCount` 1 where the MCP's
`active_symbols` said 2, and with only the unpriced one left it said 0 vs 1
and `dayChangePct` 0.0 where the MCP's `daily_change_pct` is null. The agreed
convention: an active position is any open quantity > 0, priced or not; a day
change with no priced holding is null ("n/a"), not 0.0.

Runs build_payload_data against a real database with the ledger tables
shadowed by session TEMP tables (nothing is written; rolled back and closed).
Skips without a database unless PORTFOLIODB_TESTS_REQUIRE_DB is set.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from db import connect_for_tests

TABLES = ("instruments", "lots", "price_snapshots", "income", "cash_snapshots",
          "corporate_actions", "snapshot_runs")
# price_alerts may not exist yet (migration 006); a TEMP table of its shape
# keeps the payload's alerts read from rolling the fixture back.
ALERTS_DDL = """
    CREATE TEMP TABLE price_alerts (
      id bigserial PRIMARY KEY, symbol text NOT NULL, direction text,
      level numeric(20,8), note text, active boolean NOT NULL DEFAULT true,
      created_at timestamptz NOT NULL DEFAULT now()
    )
"""


@pytest.fixture
def conn():
    c = connect_for_tests(skip=pytest.skip, fail=pytest.fail)
    try:
        today = datetime.now(timezone.utc).date()
        prev_day = datetime.combine(today - timedelta(days=2), time(12), tzinfo=timezone.utc)
        last_day = datetime.combine(today - timedelta(days=1), time(12), tzinfo=timezone.utc)
        with c.cursor() as cur:
            for t in TABLES:
                cur.execute(f"CREATE TEMP TABLE {t} (LIKE public.{t} INCLUDING DEFAULTS)")
            cur.execute(ALERTS_DDL)
            cur.execute("INSERT INTO instruments(symbol) VALUES ('AAA'), ('BBB')")
            cur.execute(
                "INSERT INTO lots(id, symbol, account, side, trade_date, quantity, price, fees) VALUES "
                "(1, 'AAA', 'QA', 'BUY', %s, 10, 100, 0), (2, 'BBB', 'QA', 'BUY', %s, 5, 20, 0)",
                (today - timedelta(days=30), today - timedelta(days=30)),
            )
            # AAA moves 104 → 110 between two sessions; BBB has never been quoted.
            cur.execute(
                "INSERT INTO price_snapshots(symbol, ts, last_price) VALUES ('AAA', %s, 104), ('AAA', %s, 110)",
                (prev_day, last_day),
            )
        yield c
    finally:
        c.rollback()
        c.close()


def _kpi(conn):
    from dashboard import payload

    return payload.build_payload_data(conn, lambda syms: {})["kpi"]


def test_an_unpriced_holding_is_active(conn):
    k = _kpi(conn)
    assert k["activeCount"] == 2                       # AAA and BBB; was 1
    assert k["unpricedSymbols"] == ["BBB"]
    assert k["dayChange"] == pytest.approx(60.0)       # 10 × (110 − 104)
    assert k["dayChangePct"] == pytest.approx(60.0 / 1040.0 * 100, abs=0.01)


def test_with_only_an_unpriced_holding_the_day_change_is_null(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lots(id, symbol, account, side, trade_date, quantity, price, fees) "
            "VALUES (3, 'AAA', 'QA', 'SELL', %s, 10, 110, 0)",
            (datetime.now(timezone.utc).date() - timedelta(days=1),),
        )
    k = _kpi(conn)
    assert k["activeCount"] == 1                       # BBB; was 0
    assert k["dayChangePct"] is None                   # n/a; was 0.0
    assert k["dayChange"] == 0.0                       # the MCP's daily_change_usd is 0.0 too
