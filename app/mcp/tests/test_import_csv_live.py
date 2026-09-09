"""The importer's transaction policy against real PostgreSQL constraints.

The fake-connection tests in app/tests/test_import_csv_policy.py prove the
control flow; this proves the thing the flow exists for — that after a real
CHECK violation the connection is still usable under --continue-on-error, and
that atomic mode really leaves nothing behind. Both need a real failed
statement, because only PostgreSQL puts a transaction into the aborted state.

Nothing persists: the connection handed to the importer has its commit()
replaced with a no-op, and the test rolls back at the end. Symbols are
deliberately unlike anything a real ledger holds.

Lives in the MCP suite (repo root, `-m slow`) because that is the suite with a
database in CI; it skips when none is reachable.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

# Importing deps first puts app/ on sys.path so the bare modules below resolve.
importlib.import_module("app.mcp.deps")

import import_csv_history as imp  # noqa: E402
from db import connect, fetch_all, load_config  # noqa: E402

pytestmark = pytest.mark.slow

HEADER = "Symbol,Trade Date,Side,Purchase Price,Quantity,Commission,Comment\n"
ROWS = (
    "ZZIMPTEST1,20260213,BUY,100.00,10,1.00,importer live test\n"
    "ZZIMPBAD,20260216,BUY,30.00,3,0.00,importer live test\n"
    "ZZIMPTEST2,20260214,BUY,50.00,4,0.50,importer live test\n"
)


class _NoCommit:
    """The real connection, except commit() does nothing."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def live_conn():
    try:
        conn = connect(load_config())
    except Exception as e:
        pytest.skip(f"DB unreachable: {e}")
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def violating_insert(monkeypatch):
    """Make ZZIMPBAD hit a real constraint: its price goes in as -1."""
    real = imp.insert_lot

    def bad(conn, symbol, account, trade_date, qty, price, fees, notes, side="BUY"):
        if symbol == "ZZIMPBAD":
            price = -1
        return real(conn, symbol, account, trade_date, qty, price, fees, notes, side=side)

    monkeypatch.setattr(imp, "insert_lot", bad)


def _count(conn) -> int:
    rows = fetch_all(conn, "SELECT count(*) AS n FROM lots WHERE symbol LIKE 'ZZIMP%%'")
    return int(rows[0]["n"])


def _file(tmp_path):
    p = tmp_path / "live.csv"
    p.write_text(HEADER + ROWS, encoding="utf-8")
    return str(p)


def test_continue_on_error_survives_a_real_check_violation(tmp_path, live_conn, violating_insert):
    conn = _NoCommit(live_conn)
    args = SimpleNamespace(default_account="IBKR", dry_run=False, continue_on_error=True)
    counts = imp._import_file(conn, _file(tmp_path), args, [])
    assert counts["rejected"] == 1
    assert counts["inserted"] == 2, "the row after the failure must still go in"
    # Visible inside the transaction; the fixture rolls it back afterwards.
    assert _count(live_conn) == 2


def test_atomic_mode_leaves_nothing_after_a_real_check_violation(tmp_path, live_conn, violating_insert):
    conn = _NoCommit(live_conn)
    args = SimpleNamespace(default_account="IBKR", dry_run=False, continue_on_error=False)
    counts = imp._import_file(conn, _file(tmp_path), args, [])
    assert counts["rejected"] == 1 and counts["inserted"] == 0
    assert _count(live_conn) == 0
    # And the connection is usable again: the importer rolled back itself.
    assert fetch_all(live_conn, "SELECT 1 AS one")[0]["one"] == 1
