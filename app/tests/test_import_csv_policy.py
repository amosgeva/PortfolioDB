"""The importer's transaction policy: a file goes in whole, or not at all.

What used to happen (audit F08): every insert committed on its own, a row that
PostgreSQL rejected left the transaction aborted, every later statement then
failed with "current transaction is aborted" and was reported as its own error,
and the earlier rows were already committed. A parse failure, meanwhile, ended
the run with exit 0.

These tests drive `_import_file` and `main` with a fake `run` and a fake
connection that records commits, rollbacks and savepoints, so the policy is
asserted without a database. `test_import_csv_live.py` (MCP suite, slow) runs
the same scenarios against real PostgreSQL constraints.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from psycopg2 import errors as pg_errors

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import import_csv_history as imp  # noqa: E402

HEADER = "Symbol,Trade Date,Side,Purchase Price,Quantity,Commission,Comment,Date,Time,Current Price\n"
GOOD_A = "AAA,20260213,BUY,100.00,10,1.00,,2026/05/20,16:00 EDT,110.00\n"
GOOD_B = "BBB,20260214,SELL,50.00,4,0.50,,2026/05/20,16:00 EDT,55.00\n"
BAD_PARSE = "CCC,20260215,BUY,20.00,-5,0.00,negative qty,2026/05/20,16:00 EDT,21.00\n"
BAD_DB = "BADDB,20260216,BUY,30.00,3,0.00,,2026/05/20,16:00 EDT,31.00\n"
DUP = "DUPL,20260217,BUY,40.00,2,0.00,,2026/05/20,16:00 EDT,41.00\n"


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    """A fake db.run that inserts, skips duplicates, and rejects one symbol."""
    log: list[tuple[str, tuple]] = []

    def fake_run(conn, sql, params=None):
        sql_one = " ".join(sql.split())
        log.append((sql_one, tuple(params or ())))
        if sql_one.startswith("INSERT INTO lots"):
            symbol = params[0]
            if symbol == "BADDB":
                # What a CHECK violation looks like from psycopg2. Only the lot
                # fails; its snapshot is fine, as it would be for a bad price.
                raise pg_errors.CheckViolation('new row violates check constraint "lots_price_check"')
            if symbol == "DUPL":
                return 0
        return 1

    monkeypatch.setattr(imp, "run", fake_run)
    return log


def _args(**kw):
    base = dict(default_account="IBKR", dry_run=False, continue_on_error=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _csv(tmp_path, *rows, name="trades.csv"):
    p = tmp_path / name
    p.write_text(HEADER + "".join(rows), encoding="utf-8")
    return str(p)


def _statements(log, prefix):
    return [s for s, _ in log if s.startswith(prefix)]


# ── atomic by default ─────────────────────────────────────────────


def test_clean_file_commits_once_and_reports_counts(tmp_path, db):
    conn = FakeConn()
    counts = imp._import_file(conn, _csv(tmp_path, GOOD_A, GOOD_B, DUP), _args(), [])
    assert counts["attempted"] == 3
    assert counts["inserted"] == 2 and counts["duplicate"] == 1
    assert counts["sells"] == 1
    assert counts["rejected"] == 0
    assert counts["snaps_inserted"] == 3
    assert (conn.commits, conn.rollbacks) == (1, 0)
    assert not _statements(db, "SAVEPOINT"), "atomic mode needs no savepoints"


def test_parse_error_writes_nothing_and_never_touches_the_database(tmp_path, db):
    conn = FakeConn()
    counts = imp._import_file(conn, _csv(tmp_path, GOOD_A, BAD_PARSE, GOOD_B), _args(), [])
    assert counts["rejected"] == 1
    assert counts["inserted"] == 0 and counts["attempted"] == 0
    assert db == [], "a file with a bad row must not reach the database at all"
    assert (conn.commits, conn.rollbacks) == (0, 0)


def test_database_error_rolls_the_whole_file_back(tmp_path, db):
    conn = FakeConn()
    counts = imp._import_file(conn, _csv(tmp_path, GOOD_A, BAD_DB, GOOD_B), _args(), [])
    assert counts["rejected"] == 1
    assert counts["inserted"] == 0
    assert (conn.commits, conn.rollbacks) == (0, 1)
    # GOOD_A was attempted before the failure; GOOD_B must not have been.
    lot_inserts = [p[0] for s, p in db if s.startswith("INSERT INTO lots")]
    assert lot_inserts == ["AAA", "BADDB"]


# ── explicit partial mode ─────────────────────────────────────────


def test_continue_on_error_isolates_the_bad_row_with_a_savepoint(tmp_path, db):
    conn = FakeConn()
    counts = imp._import_file(conn, _csv(tmp_path, GOOD_A, BAD_DB, GOOD_B), _args(continue_on_error=True), [])
    assert counts["inserted"] == 2 and counts["rejected"] == 1
    assert (conn.commits, conn.rollbacks) == (1, 0)
    assert _statements(db, "ROLLBACK TO SAVEPOINT") == ["ROLLBACK TO SAVEPOINT row_1"]
    # Every other row's savepoint was released, and the row after the failure
    # was still attempted — the transaction was not left aborted.
    assert len(_statements(db, "RELEASE SAVEPOINT")) == 2 + 3   # two lots + three snapshots
    assert [p[0] for s, p in db if s.startswith("INSERT INTO lots")] == ["AAA", "BADDB", "BBB"]


def test_continue_on_error_still_skips_parse_failures_and_counts_them(tmp_path, db):
    conn = FakeConn()
    counts = imp._import_file(conn, _csv(tmp_path, GOOD_A, BAD_PARSE, GOOD_B), _args(continue_on_error=True), [])
    assert counts["inserted"] == 2 and counts["rejected"] == 1
    assert conn.commits == 1


# ── dry run ───────────────────────────────────────────────────────


def test_dry_run_validates_and_writes_nothing(tmp_path, db):
    conn = FakeConn()
    counts = imp._import_file(conn, _csv(tmp_path, GOOD_A, BAD_PARSE, GOOD_B), _args(dry_run=True, continue_on_error=True), [])
    assert db == []
    assert counts["rejected"] == 1 and counts["attempted"] == 2 and counts["sells"] == 1
    assert (conn.commits, conn.rollbacks) == (0, 0)


# ── exit status ───────────────────────────────────────────────────


def _run_main(monkeypatch, tmp_path, *extra):
    monkeypatch.setattr(imp, "connect", lambda cfg: FakeConn())
    monkeypatch.setattr(imp, "load_config", lambda: {})
    monkeypatch.setattr(sys, "argv", ["import_csv_history.py", "--dir", str(tmp_path), "--default-account", "IBKR", *extra])
    return imp.main()


def test_exit_0_when_everything_is_in(tmp_path, db, monkeypatch):
    _csv(tmp_path, GOOD_A, DUP)
    assert _run_main(monkeypatch, tmp_path) == imp.EXIT_OK


def test_exit_1_when_a_file_was_rejected(tmp_path, db, monkeypatch):
    _csv(tmp_path, GOOD_A, BAD_PARSE)
    assert _run_main(monkeypatch, tmp_path) == imp.EXIT_FAILED


def test_exit_2_for_partial_success(tmp_path, db, monkeypatch):
    _csv(tmp_path, GOOD_A, BAD_PARSE)
    assert _run_main(monkeypatch, tmp_path, "--continue-on-error") == imp.EXIT_PARTIAL


def test_exit_1_when_nothing_matched(tmp_path, db, monkeypatch):
    assert _run_main(monkeypatch, tmp_path) == imp.EXIT_FAILED


def test_exit_1_for_a_refused_pattern(tmp_path, db, monkeypatch):
    _csv(tmp_path, GOOD_A)
    assert _run_main(monkeypatch, tmp_path, "--pattern", "../*.csv") == imp.EXIT_FAILED
