"""`--replace-estimates` deletes exactly what it rebuilds.

1.7.2 re-audit, N04: the deletion covered every backfilled row for the symbol
while the insert loop honoured `--since`, so a bounded correction removed the
older estimates and never put them back. A successful transaction cannot undo
that, so the scope has to be right before anything is deleted.

No database: `run` is recorded, yfinance is a dict, and the entitlement is a
fixed share count — the unit arithmetic has its own tests in
test_ledger_inputs.py.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import add_income  # noqa: E402
import db  # noqa: E402

OLD, NEW, NEWER = date(2025, 6, 1), date(2026, 2, 1), date(2026, 5, 1)
SINCE = date(2026, 1, 1)


class _Ts:
    def __init__(self, d):
        self._d = d

    def date(self):
        return self._d


@pytest.fixture
def harness(monkeypatch):
    """Returns (calls, set_dividends). Every `run` call is recorded as
    (first word, normalised SQL, params); DELETE reports 2 rows, INSERT 1."""
    calls: list[tuple[str, str, tuple]] = []
    state = {"dividends": {_Ts(OLD): 1.0, _Ts(NEW): 1.0, _Ts(NEWER): 1.0}}

    def set_dividends(d):
        state["dividends"] = d

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(
        Ticker=lambda sym: types.SimpleNamespace(dividends=state["dividends"])))
    monkeypatch.setattr(add_income, "_shares_held_on", lambda conn, sym, d, per_account=False: {None: 10.0})

    def fake_run(conn, sql_text, params=None):
        sql_one = " ".join(sql_text.split())
        calls.append((sql_one.split()[0], sql_one, tuple(params or ())))
        return 2 if sql_one.startswith("DELETE") else 1

    monkeypatch.setattr(add_income, "run", fake_run)
    return calls, set_dividends


def _deletes(calls):
    return [c for c in calls if c[0] == "DELETE"]


def _inserted_ex_dates(calls):
    return [c[2][3] for c in calls if c[0] == "INSERT"]


def test_since_bounds_the_deletion_to_the_dates_that_are_rebuilt(harness, capsys):
    calls, _ = harness
    inserted, dupes = add_income._backfill(object(), "AAA", since=SINCE, replace_estimates=True)
    (delete,) = _deletes(calls)
    assert "symbol = %s AND source = 'yfinance'" in delete[1], "manual rows and other symbols stay out of scope"
    assert "COALESCE(ex_date, pay_date) >= %s" in delete[1]
    assert delete[2] == ("AAA", SINCE)
    assert _inserted_ex_dates(calls) == [NEW, NEWER], "the 2025 estimate is neither deleted nor rebuilt"
    assert (inserted, dupes) == (2, 0)
    assert "dated on or after 2026-01-01" in capsys.readouterr().out


def test_without_since_every_estimate_is_replaced(harness, capsys):
    calls, _ = harness
    add_income._backfill(object(), "AAA", since=None, replace_estimates=True)
    (delete,) = _deletes(calls)
    assert "COALESCE" not in delete[1] and delete[2] == ("AAA",)
    assert _inserted_ex_dates(calls) == [OLD, NEW, NEWER]
    assert "for all dates" in capsys.readouterr().out


def test_the_deletion_runs_before_the_inserts_and_only_once(harness):
    calls, _ = harness
    add_income._backfill(object(), "AAA", since=SINCE, replace_estimates=True)
    assert [c[0] for c in calls] == ["DELETE", "INSERT", "INSERT"]


def test_a_cutoff_after_the_last_dividend_deletes_nothing(harness, capsys):
    calls, _ = harness
    result = add_income._backfill(object(), "AAA", since=date(2027, 1, 1), replace_estimates=True)
    assert result == (0, 0)
    assert calls == [], "no DELETE and no INSERT"
    assert "nothing rebuilt and no estimate removed" in capsys.readouterr().out


def test_no_vendor_history_deletes_nothing(harness, capsys):
    calls, set_dividends = harness
    set_dividends({})
    assert add_income._backfill(object(), "AAA", since=None, replace_estimates=True) == (0, 0)
    assert calls == []
    assert "No dividend history" in capsys.readouterr().out


def test_without_the_flag_since_only_filters_the_inserts(harness):
    calls, _ = harness
    add_income._backfill(object(), "AAA", since=SINCE)
    assert _deletes(calls) == []
    assert _inserted_ex_dates(calls) == [NEW, NEWER]


def test_a_failed_insert_propagates_so_the_transaction_rolls_back(harness, monkeypatch):
    """_backfill must not swallow the error: db.transaction is what restores
    the deleted rows, and it only can if the exception reaches it."""
    calls, _ = harness

    def failing_run(conn, sql_text, params=None):
        calls.append((sql_text.split()[0], " ".join(sql_text.split()), tuple(params or ())))
        if sql_text.lstrip().startswith("INSERT"):
            raise RuntimeError("constraint violation")
        return 2

    monkeypatch.setattr(add_income, "run", failing_run)
    with pytest.raises(RuntimeError, match="constraint violation"):
        add_income._backfill(object(), "AAA", since=SINCE, replace_estimates=True)
    assert [c[0] for c in calls] == ["DELETE", "INSERT"]


def test_db_transaction_rolls_back_and_closes_on_error(monkeypatch):
    """The other half of the atomicity claim, on a fake connection."""
    events: list[str] = []
    conn = types.SimpleNamespace(
        commit=lambda: events.append("commit"),
        rollback=lambda: events.append("rollback"),
        close=lambda: events.append("close"),
    )
    monkeypatch.setattr(db, "connect", lambda cfg: conn)
    with pytest.raises(RuntimeError):
        with db.transaction(cfg=object()):
            raise RuntimeError("boom")
    assert events == ["rollback", "close"]
    events.clear()
    with db.transaction(cfg=object()):
        pass
    assert events == ["commit", "close"]
