"""Ledger-core behaviour against a real PostgreSQL (schema + migrations applied).

Skips without a database, fails without one when PORTFOLIODB_TESTS_REQUIRE_DB
is set (db.connect_for_tests). Everything uses sentinel symbols (TSTW2*) and
cleans up after itself, so it can share the CI database with the demo ledger.

* R-1: lot_open_quantities() (SQL) and holdings.net_quantities over
  ledger_inputs.load() (Python) give the same answer on a seeded random ledger
  with same-day pairs, oversells, NULL and '' accounts, a hand-entered split,
  a pending vendor split and a doubly recorded split.
* D-2, D-4, F-15, F-16: the constraints and the near-duplicate trigger.
* D-3: apply_schema.py records migrations, skips them next time, and reports a
  changed one.
* E-3: fetch_actions leaves the caller's transaction intact.
* E-1, Q-7, F-15, E-5, F-17, F-3: the collector through main(), with
  collector-realistic fake quotes (zero bid pre-market, a zero price, an
  out-of-range volume, a vendor split, a failing split record).
* N-2/D-1, E-6: the add-lot CLI and the CSV importer through main().
"""

from __future__ import annotations

import io
import os
import random
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg2
import pytest
from psycopg2 import errors as pg_errors

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import add_lot  # noqa: E402
import apply_schema  # noqa: E402
import corporate_actions  # noqa: E402
import enrich_instruments  # noqa: E402
import holdings  # noqa: E402
import import_csv_history  # noqa: E402
import ledger_inputs  # noqa: E402
import portfolio  # noqa: E402
import snapshot_prices as sp  # noqa: E402
import symbol_paths  # noqa: E402
from db import connect_for_tests  # noqa: E402

PREFIX = "TSTW2"
SYMS = tuple(f"{PREFIX}{c}" for c in "ABCDEFGH")


def _wipe(conn) -> None:
    conn.rollback()
    with conn.cursor() as cur:
        like = f"{PREFIX}%"
        cur.execute("DELETE FROM lots WHERE symbol LIKE %s", (like,))
        cur.execute("DELETE FROM price_snapshots WHERE symbol LIKE %s", (like,))
        cur.execute("DELETE FROM corporate_actions WHERE symbol LIKE %s", (like,))
        cur.execute("DELETE FROM income WHERE symbol LIKE %s", (like,))
        cur.execute("DELETE FROM instruments WHERE symbol LIKE %s", (like,))
        cur.execute("DELETE FROM snapshot_runs WHERE error LIKE %s", (f"%{PREFIX}%",))
    conn.commit()


@pytest.fixture(scope="module")
def conn():
    c = connect_for_tests(skip=pytest.skip, fail=pytest.fail)
    try:
        _wipe(c)
        yield c
    finally:
        try:
            _wipe(c)
        finally:
            c.close()


@pytest.fixture(autouse=True)
def clean(conn):
    _wipe(conn)
    yield
    _wipe(conn)


def _instr(cur, *symbols, watch=False):
    for s in symbols:
        cur.execute("INSERT INTO instruments(symbol, watchlist) VALUES (%s, %s) ON CONFLICT DO NOTHING", (s, watch))


def _lot(cur, symbol, side, day, qty, account="IBKR", price=100):
    cur.execute(
        "INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees) "
        "VALUES (%s, %s, %s, %s, %s, %s, 0) RETURNING id",
        (symbol, account, side, day, Decimal(str(qty)), Decimal(str(price))),
    )
    return cur.fetchone()[0]


def _sql_net(conn, as_of=None):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, account, quantity FROM lot_open_quantities(%s) WHERE symbol LIKE %s",
                    (as_of, f"{PREFIX}%"))
        return {(s, a): q for s, a, q in cur.fetchall()}


def _py_net(conn, as_of=None):
    nets = holdings.net_quantities(ledger_inputs.load(conn, as_of=as_of).lots)
    return {k: q for k, q in nets.items() if k[0].startswith(PREFIX)}


# ── R-1: one net quantity, in Python and in SQL ───────────────────


def test_sql_and_python_net_quantity_agree_on_a_random_ledger(conn):
    rng = random.Random(1815)
    d0 = date(2025, 1, 6)
    with conn.cursor() as cur:
        _instr(cur, *SYMS[:4])
        for _ in range(400):
            _lot(cur, rng.choice(SYMS[:4]), rng.choice(["BUY", "BUY", "SELL"]),
                 d0 + timedelta(days=rng.randint(0, 120)),
                 Decimal(rng.randint(1, 30)) / Decimal(rng.choice([1, 2, 8])),
                 account=rng.choice(["IBKR", "SCHW", None, ""]),
                 price=rng.randint(10, 200) + rng.randint(0, 99) / 100)
        ins = ("INSERT INTO corporate_actions(symbol, kind, ex_date, ratio, adjust_prices, adjust_lots, "
               "source, reviewed) VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s)")
        cur.execute(ins, (SYMS[0], "SPLIT", d0 + timedelta(days=40), 2, True, "manual", False))
        cur.execute(ins, (SYMS[1], "SPLIT", d0 + timedelta(days=50), 3, True, "yfinance", False))  # pending
        cur.execute(ins, (SYMS[2], "REVERSE_SPLIT", d0 + timedelta(days=60), Decimal("0.5"), True, "yfinance", True))
        cur.execute(ins, (SYMS[3], "SPLIT", d0 + timedelta(days=70), 4, False, "manual", False))     # prices only
        # One split recorded twice, as an older database may hold: bypass the
        # trigger that now refuses it, to prove both sides apply it once.
        cur.execute("ALTER TABLE corporate_actions DISABLE TRIGGER corporate_actions_near_duplicate")
        try:
            cur.execute(ins, (SYMS[0], "SPLIT", d0 + timedelta(days=42), 2, True, "yfinance", True))
        finally:
            cur.execute("ALTER TABLE corporate_actions ENABLE TRIGGER corporate_actions_near_duplicate")
    conn.commit()

    for as_of in (None, d0 + timedelta(days=41), d0 + timedelta(days=55), d0 + timedelta(days=90)):
        sql_q, py_q = _sql_net(conn, as_of), _py_net(conn, as_of)
        assert sql_q == py_q, as_of
        assert sql_q, "the ledger must leave something open, or this proves little"

    # And both equal the engines' open quantity per symbol.
    engine = portfolio.compute_fifo_merged(ledger_inputs.load(conn).lots).set_index("symbol")["qty"]
    per_symbol: dict[str, Decimal] = {}
    for (s, _a), q in _sql_net(conn).items():
        per_symbol[s] = per_symbol.get(s, Decimal(0)) + q
    for s in SYMS[:4]:
        assert float(per_symbol.get(s, 0)) == pytest.approx(engine.get(s, 0.0)), s


def test_the_doubly_recorded_split_is_applied_once_in_sql(conn):
    with conn.cursor() as cur:
        _instr(cur, SYMS[0])
        _lot(cur, SYMS[0], "BUY", date(2025, 1, 2), 10)
        cur.execute("ALTER TABLE corporate_actions DISABLE TRIGGER corporate_actions_near_duplicate")
        try:
            for day, src in ((date(2025, 2, 3), "manual"), (date(2025, 2, 4), "yfinance")):
                cur.execute("INSERT INTO corporate_actions(symbol, kind, ex_date, ratio, source, reviewed) "
                            "VALUES (%s, 'SPLIT', %s, 2, %s, TRUE)", (SYMS[0], day, src))
        finally:
            cur.execute("ALTER TABLE corporate_actions ENABLE TRIGGER corporate_actions_near_duplicate")
    conn.commit()
    assert _sql_net(conn) == {(SYMS[0], "IBKR"): Decimal(20)} == _py_net(conn)


# ── D-2, D-4, F-15, F-16: constraints ─────────────────────────────


@pytest.mark.parametrize("sym", ["<IMG>", "tstw2a", "-X", "A B", "X" * 33, "&#60;"])
def test_the_database_refuses_a_symbol_that_is_not_one(conn, sym):
    with pytest.raises(pg_errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO instruments(symbol) VALUES (%s)", (sym,))
    conn.rollback()


def test_the_database_and_python_agree_on_what_a_symbol_is(conn):
    corpus = ["NVDA", "BRK.B", "BRK-B", "^GSPC", "ES=F", "0700.HK", "A_B", "^TA125.TA", "X" * 32,
              "X" * 33, "nvda", ".A", "-A", "=A", "A B", "A/B", "<A>", "É", "", "A\nB", "A^B"]
    with conn.cursor() as cur:
        for s in corpus:
            cur.execute("SELECT %s ~ '^[A-Z0-9^][A-Z0-9.=^_-]{0,31}$'", (s,))
            assert cur.fetchone()[0] == symbol_paths.is_safe_symbol(s), repr(s)


@pytest.mark.parametrize("stmt", [
    "INSERT INTO price_snapshots(ts, symbol, last_price) VALUES (now(), 'TSTW2A', 0)",
    "INSERT INTO price_snapshots(ts, symbol, last_price) VALUES (now(), 'TSTW2A', -1)",
    "INSERT INTO corporate_actions(symbol, kind, ex_date, ratio) VALUES ('TSTW2A', 'SPLIT', '2025-01-01', 'NaN')",
    "INSERT INTO corporate_actions(symbol, kind, ex_date, ratio) VALUES ('TSTW2A', 'SPLIT', '2025-01-01', 0.5)",
    "INSERT INTO corporate_actions(symbol, kind, ex_date, ratio) VALUES ('TSTW2A', 'REVERSE_SPLIT', '2025-01-01', 2)",
    "INSERT INTO corporate_actions(symbol, kind, ex_date, ratio) VALUES ('TSTW2A', 'NONE', '2025-01-01', 2)",
])
def test_value_constraints(conn, stmt):
    with conn.cursor() as cur:
        _instr(cur, SYMS[0])
    conn.commit()
    with pytest.raises(pg_errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(stmt)
    conn.rollback()


def test_one_split_cannot_be_recorded_twice(conn):
    with conn.cursor() as cur:
        _instr(cur, SYMS[0])
        cur.execute("INSERT INTO corporate_actions(symbol, kind, ex_date, ratio) VALUES (%s, 'SPLIT', '2025-03-03', 2)",
                    (SYMS[0],))
    conn.commit()
    with pytest.raises(pg_errors.UniqueViolation, match="recorded twice"):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO corporate_actions(symbol, kind, ex_date, ratio, source) "
                        "VALUES (%s, 'SPLIT', '2025-03-05', 2, 'yfinance')", (SYMS[0],))
    conn.rollback()
    with conn.cursor() as cur:      # a different event, and inert NONE rows, are fine
        cur.execute("INSERT INTO corporate_actions(symbol, kind, ex_date, ratio) VALUES (%s, 'SPLIT', '2025-03-05', 3)",
                    (SYMS[0],))
        cur.execute("INSERT INTO corporate_actions(symbol, kind, ex_date, ratio) VALUES (%s, 'NONE', '2025-03-04', 1)",
                    (SYMS[0],))
        cur.execute("INSERT INTO corporate_actions(symbol, kind, ex_date, ratio) VALUES (%s, 'NONE', '2025-03-06', 1)",
                    (SYMS[0],))
    conn.commit()


def test_record_split_skips_a_split_already_on_file_under_another_date(conn):
    with conn.cursor() as cur:
        _instr(cur, SYMS[0])
        cur.execute("INSERT INTO corporate_actions(symbol, kind, ex_date, ratio) VALUES (%s, 'SPLIT', '2025-06-09', 4)",
                    (SYMS[0],))
    conn.commit()
    q = sp.Quote(last_price=50.0, bid=None, ask=None, market_time=None, market_state="REGULAR",
                 split_factor="4:1", split_date=date(2025, 6, 10))
    assert sp.record_split(conn, SYMS[0], q, None) is None
    q2 = sp.Quote(last_price=50.0, bid=None, ask=None, market_time=None, market_state="REGULAR",
                  split_factor="2:1", split_date=date(2025, 12, 1))
    assert sp.record_split(conn, SYMS[0], q2, None) is not None
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT ex_date, reviewed, adjust_lots, source FROM corporate_actions WHERE symbol = %s "
                    "ORDER BY ex_date", (SYMS[0],))
        rows = cur.fetchall()
    assert rows == [(date(2025, 6, 9), False, True, "manual"), (date(2025, 12, 1), False, True, "yfinance")]


# ── E-3 ───────────────────────────────────────────────────────────


def test_fetch_actions_keeps_the_callers_transaction(conn):
    """A schema without corporate_actions, a row written in the open
    transaction, then fetch_actions: [] — and the row is still there."""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA tstw2a_e3")
        cur.execute("SET LOCAL search_path TO tstw2a_e3")
        cur.execute("CREATE TABLE marker(x int)")
        cur.execute("INSERT INTO marker VALUES (1)")
    try:
        assert corporate_actions.fetch_actions(conn) == []
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM marker")
            assert cur.fetchone()[0] == 1
    finally:
        conn.rollback()


# ── D-3 ───────────────────────────────────────────────────────────


def _apply(capsys=None):
    c = connect_for_tests(skip=pytest.skip, fail=pytest.fail)
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = apply_schema.run(c)
    finally:
        c.close()
    return rc, out.getvalue(), err.getvalue()


def test_apply_schema_records_and_then_skips_migrations(conn):
    rc, out, _ = _apply()
    assert rc == 0
    rc, out, err = _apply()
    assert rc == 0 and "0 applied" in out and "CHANGED" not in out + err
    names = {f.name for f in apply_schema.migration_files()}
    with conn.cursor() as cur:
        cur.execute("SELECT filename, checksum FROM schema_migrations")
        recorded = dict(cur.fetchall())
    conn.commit()
    assert names <= set(recorded)
    for f in apply_schema.migration_files():
        assert recorded[f.name] == apply_schema.checksum(f)


def test_a_changed_migration_is_reported_loudly_and_reapplied(conn):
    _apply()
    target = apply_schema.migration_files()[-1].name
    with conn.cursor() as cur:
        cur.execute("UPDATE schema_migrations SET checksum = 'stale' WHERE filename = %s", (target,))
    conn.commit()
    rc, out, err = _apply()
    assert rc == 0
    assert f"{target} HAS CHANGED" in err and "recorded sha256 stale" in err
    assert "1 CHANGED" in out
    with conn.cursor() as cur:
        cur.execute("SELECT checksum FROM schema_migrations WHERE filename = %s", (target,))
        assert cur.fetchone()[0] != "stale"
    conn.commit()


def test_the_checksum_ignores_line_endings(tmp_path):
    a, b = tmp_path / "a.sql", tmp_path / "b.sql"
    a.write_bytes(b"SELECT 1;\nSELECT 2;\n")
    b.write_bytes(b"SELECT 1;\r\nSELECT 2;\r\n")
    assert apply_schema.checksum(a) == apply_schema.checksum(b)


# ── the collector, end to end ─────────────────────────────────────


def _seed_collector_ledger(conn):
    A, B, C, D, E, F, G = SYMS[:7]
    with conn.cursor() as cur:
        _instr(cur, A, B, C, E, F, G)
        _instr(cur, D, watch=True)
        d = date(2026, 1, 5)
        for s in (A, B, C, F):
            _lot(cur, s, "BUY", d, 10)
        # E: a same-day round trip whose SELL was stored first — closed (F-1).
        _lot(cur, E, "SELL", d, 10)
        _lot(cur, E, "BUY", d, 10)
        # G: oversold then bought again — 5 open by the engines' rules; the old
        # SQL sum said 0 and stopped collecting it (R-1).
        _lot(cur, G, "BUY", d, 10)
        _lot(cur, G, "SELL", d + timedelta(days=1), 15)
        _lot(cur, G, "BUY", d + timedelta(days=2), 5)
    conn.commit()


def _fake_quotes(monkeypatch):
    A, B, C, D, _E, F, G = SYMS[:7]
    now_epoch = int(time.time())
    split_epoch = int(datetime(2026, 1, 20, tzinfo=timezone.utc).timestamp())
    infos = {
        A: {"regularMarketPrice": 101.5, "bid": 0.0, "ask": 0.0, "marketState": "PRE",
            "regularMarketTime": now_epoch - 3600},
        B: {"regularMarketPrice": 0.0, "marketState": "REGULAR", "regularMarketTime": now_epoch},
        C: {"regularMarketPrice": 55.0, "marketState": "REGULAR", "regularMarketTime": now_epoch,
            "regularMarketVolume": 1e19},                    # overflows NUMERIC(20,2)
        D: {"regularMarketPrice": 40.0, "marketState": "REGULAR", "regularMarketTime": now_epoch,
            "lastSplitFactor": "2:1", "lastSplitDate": split_epoch},
        F: {"regularMarketPrice": 12.25, "bid": 12.2, "ask": 12.3, "marketState": "REGULAR",
            "regularMarketTime": now_epoch},
        G: {"regularMarketPrice": 7.5, "marketState": "REGULAR", "regularMarketTime": now_epoch},
    }

    class Ticker:
        def __init__(self, symbol):
            self.info = infos[symbol]

    monkeypatch.setattr(sp.yf, "Ticker", Ticker)
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    real = sp.held_or_watched_symbols
    monkeypatch.setattr(sp, "held_or_watched_symbols",
                        lambda c: [s for s in real(c) if s.startswith(PREFIX)])

    # Q-7: the split record for A fails with a real database error. Without its
    # own savepoint this aborted the transaction the next symbol ran in.
    real_record = sp.record_split

    def record(c, symbol, quote, floor):
        if symbol == A:
            with c.cursor() as cur:
                cur.execute("SELECT 1/0")
        return real_record(c, symbol, quote, floor)

    monkeypatch.setattr(sp, "record_split", record)


def _last_run(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, status, symbols_total, symbols_ok, symbols_failed, error "
                    "FROM snapshot_runs ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    conn.commit()
    return row


def test_the_collector_survives_bad_symbols_and_finishes_the_run(conn, monkeypatch):
    _seed_collector_ledger(conn)
    A, B, C, D, _E, F, G = SYMS[:7]
    selected = [s for s in sp.held_or_watched_symbols(conn) if s.startswith(PREFIX)]
    assert selected == [A, B, C, D, F, G], "E is closed; G is open by the engines' rules"
    conn.commit()

    _fake_quotes(monkeypatch)
    sp.main(["--ignore-window"])

    run_id, status, total, ok, failed, error = _last_run(conn)
    assert (status, total, ok, failed) == ("partial", 6, 4, 2)
    assert B in error and "not a price" in error
    assert C in error and "not stored" in error
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, last_price, bid, ask FROM price_snapshots WHERE symbol LIKE %s "
                    "ORDER BY symbol", (f"{PREFIX}%",))
        stored = cur.fetchall()
        cur.execute("SELECT count(*) FROM snapshot_runs WHERE status = 'running'")
        running = cur.fetchone()[0]
        cur.execute("SELECT symbol, ex_date, reviewed, adjust_lots, source FROM corporate_actions "
                    "WHERE symbol LIKE %s", (f"{PREFIX}%",))
        actions = cur.fetchall()
    conn.commit()
    assert [s for s, *_ in stored] == [A, D, F, G]
    assert stored[0][2] is None and stored[0][3] is None, "a 0.0 bid/ask is stored as NULL"
    assert running == 0
    assert actions == [(D, date(2026, 1, 20), False, True, "yfinance")], "A's failed split record left nothing"

    # F-3: the pending split changes nothing until approved.
    assert ledger_inputs.load(conn, symbol=D).lots == []
    with conn.cursor() as cur:
        _lot(cur, D, "BUY", date(2026, 1, 5), 10)
    conn.commit()
    assert ledger_inputs.load(conn, symbol=D).lots[0]["quantity"] == Decimal(10)
    with conn.cursor() as cur:
        cur.execute("UPDATE corporate_actions SET reviewed = TRUE WHERE symbol = %s", (D,))
    conn.commit()
    assert ledger_inputs.load(conn, symbol=D).lots[0]["quantity"] == Decimal(20)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM snapshot_runs WHERE id = %s", (run_id,))
    conn.commit()


def test_an_error_outside_the_symbol_loop_still_finishes_the_run(conn, monkeypatch):
    """E-1: the failure handler used to UPDATE on an aborted transaction, fail,
    and leave the run 'running'."""
    _seed_collector_ledger(conn)
    _fake_quotes(monkeypatch)

    def broken_floor(c):
        with c.cursor() as cur:
            cur.execute("SELECT 1/0")

    monkeypatch.setattr(sp, "ledger_floor_date", broken_floor)
    with pytest.raises(psycopg2.errors.DivisionByZero):
        sp.main(["--ignore-window"])
    run_id, status, *_rest, error = _last_run(conn)
    assert status == "failed" and "DivisionByZero" in error
    with conn.cursor() as cur:
        cur.execute("DELETE FROM snapshot_runs WHERE id = %s", (run_id,))
    conn.commit()


def test_overlapping_runs_neither_start_nor_reap(conn, monkeypatch):
    """E-5: a run in progress holds the lock. A second run does not start, and
    does not reap the first; once the lock is free, a 'running' row can only
    be a dead process's, and the next run reaps it whatever its age."""
    _seed_collector_ledger(conn)
    _fake_quotes(monkeypatch)
    monkeypatch.setattr(sp, "RUN_DEADLINE_S", 0)         # keep the runs short
    with conn.cursor() as cur:
        cur.execute("INSERT INTO snapshot_runs(ts_start, status, error) "
                    "VALUES (now() - interval '2 minutes', 'running', %s) RETURNING id", (f"live {PREFIX}",))
        live_id = cur.fetchone()[0]
    conn.commit()
    holder = connect_for_tests(skip=pytest.skip, fail=pytest.fail)
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s, %s)", (sp.LOCK_NAMESPACE, sp.LOCK_HOLDINGS))
        holder.commit()
        before = _last_run(conn)[0]
        sp.main(["--ignore-window"])
        assert _last_run(conn)[0] == before, "no second run was started"
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM snapshot_runs WHERE id = %s", (live_id,))
            assert cur.fetchone()[0] == "running", "and the live run was not reaped"
        conn.commit()
    finally:
        holder.close()                                    # the 'live' process dies
    sp.main(["--ignore-window"])
    with conn.cursor() as cur:
        cur.execute("SELECT status, error FROM snapshot_runs WHERE id = %s", (live_id,))
        status, error = cur.fetchone()
        cur.execute("DELETE FROM snapshot_runs WHERE id > %s", (before,))
        cur.execute("DELETE FROM snapshot_runs WHERE id = %s", (live_id,))
    conn.commit()
    assert status == "failed" and "reaped" in error


def test_the_deadline_ends_the_run_and_reports_what_was_skipped(conn, monkeypatch):
    """F-17."""
    _seed_collector_ledger(conn)
    _fake_quotes(monkeypatch)
    monkeypatch.setattr(sp, "RUN_DEADLINE_S", 0)
    sp.main(["--ignore-window"])
    run_id, status, total, ok, failed, error = _last_run(conn)
    assert (status, ok, failed) == ("failed", 0, total) and "deadline passed" in error
    with conn.cursor() as cur:
        cur.execute("DELETE FROM snapshot_runs WHERE id = %s", (run_id,))
    conn.commit()


def test_a_quote_still_in_flight_at_the_deadline_is_not_stored(conn, monkeypatch):
    """QA of PR #125 (qa_deadline_probe): one quote taking 0.2 s against a
    0.05 s budget was stored and the run finished 'ok'. Now the late quote is
    abandoned, nothing is stored for it, and the run does not claim success."""
    import threading

    with conn.cursor() as cur:
        _instr(cur, SYMS[0], SYMS[1])
        _lot(cur, SYMS[0], "BUY", date(2026, 1, 5), 1)
        _lot(cur, SYMS[1], "BUY", date(2026, 1, 5), 1)
    conn.commit()
    real = sp.held_or_watched_symbols
    monkeypatch.setattr(sp, "held_or_watched_symbols", lambda c: [s for s in real(c) if s.startswith(PREFIX)])
    release = threading.Event()

    def quote(symbol):
        if symbol == SYMS[1]:
            release.wait(5)                     # slower than the whole budget
        return sp.Quote(last_price=10.0, bid=None, ask=None, market_time=None, market_state="REGULAR")

    monkeypatch.setattr(sp, "get_quote", quote)
    monkeypatch.setattr(sp, "RUN_DEADLINE_S", 0.5)
    started = time.monotonic()
    try:
        sp.main(["--ignore-window"])
    finally:
        release.set()
    assert time.monotonic() - started < 3.0
    run_id, status, total, ok, failed, error = _last_run(conn)
    assert (status, total, ok, failed) == ("partial", 2, 1, 1)
    assert SYMS[1] in error and "in flight" in error
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM price_snapshots WHERE symbol LIKE %s", (f"{PREFIX}%",))
        assert [r[0] for r in cur.fetchall()] == [SYMS[0]]
        cur.execute("DELETE FROM snapshot_runs WHERE id = %s", (run_id,))
    conn.commit()


# ── writers through main() (N-2/D-1, E-6) ─────────────────────────


def _count(conn, symbol):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM lots WHERE symbol = %s", (symbol,))
        n = cur.fetchone()[0]
    conn.commit()
    return n


def test_add_lot_reports_a_duplicate_against_the_real_index(conn, capsys):
    args = ["--symbol", SYMS[0].lower(), "--account", "IBKR", "--trade-date", "2026-03-02",
            "--qty", "10", "--price", "100"]
    assert add_lot.main(args) == 0
    assert add_lot.main(args) == add_lot.EXIT_DUPLICATE
    assert _count(conn, SYMS[0]) == 1
    out, err = capsys.readouterr()
    assert out.count("OK recorded") == 1 and "DUPLICATE — NOT RECORDED" in err


def test_the_importer_end_to_end(conn, tmp_path, monkeypatch, capsys):
    """A real import with a same-day SELL listed before its BUY, then the same
    file again: everything listed as already recorded, exit 0."""
    A = SYMS[0]
    (tmp_path / "trades.csv").write_text(
        "Symbol,Trade Date,Side,Purchase Price,Quantity,Commission,Comment,Date,Time,Current Price\n"
        f"{A},20260302,SELL,110,10,0,IBKR,2026/03/02,16:00 EDT,111\n"
        f"{A},20260302,BUY,100,10,0,IBKR,2026/03/02,16:00 EDT,111\n",
        encoding="utf-8")
    argv = ["x", "--dir", str(tmp_path), "--default-account", "Other", "--tagged-accounts", "IBKR"]
    monkeypatch.setattr(sys, "argv", argv)
    assert import_csv_history.main() == import_csv_history.EXIT_OK
    assert _count(conn, A) == 2
    df = portfolio.compute_fifo_merged(ledger_inputs.load(conn, symbol=A).lots)
    assert df.iloc[0]["qty"] == 0 and df.iloc[0]["realized_pnl"] == pytest.approx(100.0)
    assert import_csv_history.main() == import_csv_history.EXIT_OK
    out = capsys.readouterr().out
    assert "trades.csv:2 |" in out and "trades.csv:3 |" in out and "already recorded" in out
    assert _count(conn, A) == 2


def test_the_importer_refuses_an_instrument_registered_in_another_currency(conn, tmp_path, monkeypatch, capsys):
    """QA of PR #125, F-11: QAEUR registered with currency 'EUR' imported with
    exit 0. Now the file is refused (atomic) unless --allow-non-usd."""
    eur = SYMS[1]
    with conn.cursor() as cur:
        cur.execute("INSERT INTO instruments(symbol, currency) VALUES (%s, 'EUR')", (eur,))
    conn.commit()
    (tmp_path / "eur.csv").write_text(
        "Symbol,Trade Date,Purchase Price,Quantity\n"
        f"{SYMS[0]},20261110,50,2\n"
        f"{eur},20261110,100,1\n", encoding="utf-8")
    argv = ["x", "--dir", str(tmp_path), "--default-account", "A"]
    monkeypatch.setattr(sys, "argv", argv)
    assert import_csv_history.main() == import_csv_history.EXIT_FAILED
    assert _count(conn, eur) == 0 and _count(conn, SYMS[0]) == 0, "the whole file rolled back"
    assert "registered as trading in EUR" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", argv + ["--allow-non-usd"])
    assert import_csv_history.main() == import_csv_history.EXIT_OK
    assert _count(conn, eur) == 1 and _count(conn, SYMS[0]) == 1


def test_enrichment_never_replaces_a_real_value_with_a_placeholder(conn, monkeypatch):
    with conn.cursor() as cur:
        _instr(cur, SYMS[0])
        cur.execute("UPDATE instruments SET sector = 'Technology', country = NULL WHERE symbol = %s", (SYMS[0],))
    conn.commit()

    class T:
        def __init__(self, s):
            self.info = {"quoteType": "EQUITY", "longName": "Test Co"}   # no sector, no country

    monkeypatch.setattr(enrich_instruments.yf, "Ticker", T)
    import types
    assert enrich_instruments._enrich(conn, types.SimpleNamespace(symbol=[SYMS[0]], held_only=False,
                                                                   missing_only=False)) == 0
    with conn.cursor() as cur:
        cur.execute("SELECT sector, country, name FROM instruments WHERE symbol = %s", (SYMS[0],))
        assert cur.fetchone() == ("Technology", "Unknown", "Test Co")
    conn.commit()
