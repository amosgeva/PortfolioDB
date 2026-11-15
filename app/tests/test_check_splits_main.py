"""check_splits.main(): the vendor cross-check is actually run (audit F-4),
--no-vendor-check is honoured, pending reviews are listed, and --approve is
the way a pending split takes effect (F-3).

Driven through main() with the database and yfinance replaced: the daily
price series has a clean 2:1-shaped step, which the heuristic flags."""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import check_splits as cs  # noqa: E402
import corporate_actions  # noqa: E402
from corporate_actions import CorporateAction  # noqa: E402

STEP = {"PRIM": [(date(2026, 5, 5), 100.0), (date(2026, 5, 6), 49.97)]}


class _Conn:
    def __init__(self):
        self.closed = False
        self.executed = []
        self.committed = False
        self.update_rows = [(1,)]

    def close(self):
        self.closed = True

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, q, p=None): conn.executed.append((" ".join(q.split()), p))
            def fetchall(s): return conn.update_rows
        return Cur()


@pytest.fixture
def env(monkeypatch):
    conn = _Conn()
    state = {"recorded": [], "vendor_calls": []}
    monkeypatch.setattr(cs, "load_config", lambda: None)
    monkeypatch.setattr(cs, "connect", lambda cfg: conn)
    monkeypatch.setattr(cs, "fetch_daily_prices", lambda c, sym: STEP)
    monkeypatch.setattr(corporate_actions, "fetch_recorded_actions", lambda c: list(state["recorded"]))

    def verdict(symbol, day):
        state["vendor_calls"].append((symbol, day))
        return {"status": "contradicted", "detail": "yfinance lists no splits for this symbol, ever"}

    monkeypatch.setattr(cs, "vendor_verdict", verdict)
    state["conn"] = conn
    return state


def test_every_hit_is_checked_against_the_vendor(env, capsys):
    assert cs.main([]) == 1
    assert env["vendor_calls"] == [("PRIM", date(2026, 5, 6))]
    out = capsys.readouterr().out
    assert "NOT A SPLIT (vendor contradicts)" in out and "no splits for this symbol" in out


def test_no_vendor_check_is_honoured(env, capsys):
    assert cs.main(["--no-vendor-check"]) == 1
    assert env["vendor_calls"] == []
    assert "UNVERIFIED" in capsys.readouterr().out


def test_pending_reviews_are_listed_and_not_re_reported_as_suspects(env, capsys):
    env["recorded"] = [CorporateAction("PRIM", "SPLIT", date(2026, 5, 6), Decimal(2),
                                       source="yfinance", reviewed=False)]
    assert cs.main([]) == 1
    out = capsys.readouterr().out
    assert "1 split(s) PENDING REVIEW" in out and "--approve SYMBOL:YYYY-MM-DD" in out
    assert "No unrecorded splits detected." in out
    assert env["vendor_calls"] == []


def test_nothing_to_report_exits_zero(env, monkeypatch, capsys):
    monkeypatch.setattr(cs, "fetch_daily_prices", lambda c, sym: {"X": [(date(2026, 5, 5), 100.0),
                                                                      (date(2026, 5, 6), 101.0)]})
    assert cs.main([]) == 0


def test_approve_needs_the_lots_decision(env):
    with pytest.raises(SystemExit) as e:
        cs.main(["--approve", "NVDA:2024-06-10"])
    assert e.value.code == 2


def test_approve_marks_it_reviewed_and_sets_adjust_lots(env, capsys):
    assert cs.main(["--approve", "nvda:2024-06-10", "--shares-credited"]) == 0
    sql, params = env["conn"].executed[0]
    assert sql.startswith("UPDATE corporate_actions SET reviewed = TRUE")
    assert params[0] is True and params[2:] == ("NVDA", date(2024, 6, 10))
    assert env["conn"].committed
    assert "now applies to prices and lots" in capsys.readouterr().out


def test_approve_prices_only(env):
    assert cs.main(["--approve", "NVDA:2024-06-10", "--quantities-already-post-split"]) == 0
    assert env["conn"].executed[0][1][0] is False


def test_approve_reports_a_missing_row(env, capsys):
    env["conn"].update_rows = []
    assert cs.main(["--approve", "NVDA:2024-06-10", "--shares-credited"]) == 2
    assert "Nothing approved" in capsys.readouterr().out
