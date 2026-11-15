"""KPI parity and price alerts against a real Postgres.

The fakes in test_kpi_definitions.py prove the definitions; these run the real
SQL. Every write happens inside one transaction that is rolled back, so the
database is left as it was found (the demo ledger in CI and in the clean
room). Skips without a database unless PORTFOLIODB_TESTS_REQUIRE_DB is set, in
which case a missing database fails (db.connect_for_tests).

`price_alerts` arrives with migration 006 (another 1.8.0 workstream). These
tests create a TEMP table of the same shape, which shadows a real one inside
the session, so they hold before and after that migration.
"""

from __future__ import annotations

from datetime import date

import pytest

from db import connect_for_tests

# The contract's shape, minus the foreign key: Postgres does not let a
# temporary table reference a permanent one.
ALERTS_DDL = """
    CREATE TEMP TABLE price_alerts (
      id bigserial PRIMARY KEY,
      symbol text NOT NULL,
      direction text CHECK (direction IN ('above','below')),
      level numeric(20,8) CHECK (level > 0 AND level <> 'NaN'),
      note text,
      active boolean NOT NULL DEFAULT true,
      created_at timestamptz NOT NULL DEFAULT now()
    ) ON COMMIT DROP
"""

UNPRICED = "TST_UNPRICED"


@pytest.fixture
def conn():
    c = connect_for_tests(skip=pytest.skip, fail=pytest.fail)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def test_active_alerts_are_read_in_order(conn):
    from dashboard import queries

    with conn.cursor() as cur:
        cur.execute(ALERTS_DDL)
        cur.execute("INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT DO NOTHING", (UNPRICED,))
        cur.execute(
            "INSERT INTO price_alerts(symbol, direction, level, note, active, created_at) VALUES "
            "(%s, 'above', 10, 'first', true, now() - interval '2 days'),"
            "(%s, 'below', 5, 'off', false, now() - interval '1 day'),"
            "(%s, 'below', 8, 'second', true, now())",
            (UNPRICED, UNPRICED, UNPRICED),
        )
    rows = queries.price_alerts(conn)
    assert [r["note"] for r in rows] == ["first", "second"]
    assert {"id", "symbol", "direction", "level", "note", "created_at"} <= set(rows[0])


def test_a_missing_table_is_no_alerts_and_the_connection_stays_usable(conn):
    from dashboard import queries

    with conn.cursor() as cur:
        # Nothing but the (empty) temp schema on the path: price_alerts cannot
        # resolve, exactly as on a database without migration 006.
        cur.execute("SET LOCAL search_path TO pg_temp")
    assert queries.price_alerts(conn) == []
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1


def test_dashboard_advisor_and_report_agree_with_an_unpriced_holding(conn):
    """The demo ledger plus one holding that has never been quoted."""
    import advisor
    import exec_report
    from dashboard import payload

    with conn.cursor() as cur:
        cur.execute(ALERTS_DDL)      # so the payload's alerts read never rolls back our rows
        cur.execute("INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT DO NOTHING", (UNPRICED,))
        cur.execute(
            "INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees, notes) "
            "VALUES (%s, 'TSTACC', 'BUY', %s, 10, 10, 0, 'test') ON CONFLICT DO NOTHING",
            (UNPRICED, date(2026, 1, 5)),
        )

    k = payload.build_payload_data(conn, lambda syms: {})["kpi"]
    s = advisor.snapshot_portfolio(conn)
    t = exec_report.gather(conn).totals

    assert UNPRICED in k["unpricedSymbols"]
    assert k["unpricedSymbols"] == s["unpriced_symbols"] == t["unpriced_symbols"]
    pairs = {
        "market value": (k["marketValue"], s["total_market_value"], t["value"]),
        "priced cost": (k["pricedCostBasis"], s["priced_cost_basis"], t["priced_cost"]),
        "unpriced cost": (k["unpricedCostBasis"], s["unpriced_cost_basis"], t["unpriced_cost"]),
        "unrealized": (k["unrealized"], s["total_unrealized"], t["unrl"]),
        "realized": (k["realized"], s["total_realized"], t["realized"]),
        "invested": (k["investedCapital"], s["invested_capital"], t["invested"]),
        "total return %": (k["totalReturnPct"], s["total_return_pct"], t["total_return_pct"]),
        "cash": (k["cash"], s["cash_total"], t["cash"]),
    }
    for name, (dash, adv, rep) in pairs.items():
        if dash is None:
            assert adv is None and rep is None, name
            continue
        assert adv == pytest.approx(dash, abs=0.01), name
        assert rep == pytest.approx(dash, abs=0.01), name
    assert k["unpricedCostBasis"] >= 100.0
