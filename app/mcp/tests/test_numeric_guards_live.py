"""The database refuses NaN in every numeric ledger column.

The parsers in app/ledger_numbers.py stop NaN at the door; migration 003 is
the backstop for any writer that does not use them. PostgreSQL's numeric NaN
satisfies `quantity > 0`, so without the migration this INSERT succeeds — which
is exactly what these tests would report.

Inside a transaction that is rolled back: nothing persists. Skips without a
database, and skips with a pointer when the migration has not been applied
(an operator's database before `make schema`), so it never fails for a reason
that is not the constraint.
"""

from __future__ import annotations

import importlib

import pytest
from psycopg2 import errors as pg_errors

# Importing deps first puts app/ on sys.path so the bare modules below resolve.
importlib.import_module("app.mcp.deps")

from db import connect, fetch_all, load_config  # noqa: E402

pytestmark = pytest.mark.slow

CASES = [
    ("lots", "quantity",
     "INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees) "
     "VALUES ('ZZNANTEST', 'T', 'BUY', '2026-01-01', 'NaN', 1, 0)"),
    ("lots", "price",
     "INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees) "
     "VALUES ('ZZNANTEST', 'T', 'BUY', '2026-01-01', 1, 'NaN', 0)"),
    ("lots", "fees",
     "INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees) "
     "VALUES ('ZZNANTEST', 'T', 'BUY', '2026-01-01', 1, 1, 'NaN')"),
    ("price_snapshots", "last_price",
     "INSERT INTO price_snapshots(ts, symbol, last_price, source) VALUES (now(), 'ZZNANTEST', 'NaN', 'test')"),
    ("price_snapshots", "bid",
     "INSERT INTO price_snapshots(ts, symbol, last_price, bid, source) VALUES (now(), 'ZZNANTEST', 1, 'NaN', 'test')"),
    ("cash_snapshots", "cash",
     "INSERT INTO cash_snapshots(account, cash) VALUES ('ZZNANTEST', 'NaN')"),
    ("income", "amount",
     "INSERT INTO income(symbol, kind, pay_date, amount) VALUES ('ZZNANTEST', 'DIVIDEND', '2026-01-01', 'NaN')"),
]


@pytest.fixture
def conn():
    try:
        c = connect(load_config())
    except Exception as e:
        pytest.skip(f"DB unreachable: {e}")
    applied = fetch_all(c, "SELECT 1 FROM pg_constraint WHERE conname = 'lots_quantity_not_nan'")
    if not applied:
        c.close()
        pytest.skip("migration 003_finite_numeric_checks.sql not applied to this database (run `make schema`)")
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.mark.parametrize("table,column,insert", CASES, ids=[f"{t}.{c}" for t, c, _ in CASES])
def test_nan_is_rejected(conn, table, column, insert):
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT nan_case")
        cur.execute("INSERT INTO instruments(symbol) VALUES ('ZZNANTEST') ON CONFLICT DO NOTHING")
        with pytest.raises(pg_errors.CheckViolation) as exc:
            cur.execute(insert)
        assert f"{table}_{column}_not_nan" in str(exc.value)
        cur.execute("ROLLBACK TO SAVEPOINT nan_case")


def test_the_old_sign_check_alone_would_have_admitted_nan(conn):
    """Documents why the migration exists: NaN > 0 is true for numeric."""
    assert fetch_all(conn, "SELECT 'NaN'::numeric > 0 AS gt")[0]["gt"] is True
