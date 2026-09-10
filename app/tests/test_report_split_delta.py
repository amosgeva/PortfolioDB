"""The daily/EOD report does not print a split as a loss.

Re-audit F03, case B: the report restated its lots but compared raw quotes,
so a 2:1 split between the previous quote (100) and the current one (50)
printed `Delta: $-1,000.00` on a position worth exactly what it was worth.
The real main() runs here; only the database and price fetchers are replaced.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ledger_inputs  # noqa: E402
import report_portfolio_db as rep  # noqa: E402
from corporate_actions import CorporateAction  # noqa: E402

D_BUY, D_EX = date(2026, 3, 2), date(2026, 3, 4)
T_PREV = datetime(2026, 3, 3, 20, tzinfo=timezone.utc)   # before the ex-date, quote 100
T_NOW = datetime(2026, 3, 4, 20, tzinfo=timezone.utc)    # after it, quote 50
LOTS = [{"id": 1, "symbol": "AAA", "account": "IBKR", "side": "BUY", "trade_date": D_BUY,
         "quantity": Decimal("10"), "price": Decimal("100"), "fees": Decimal("0")}]
SPLIT = [CorporateAction(symbol="AAA", kind="SPLIT", ex_date=D_EX, ratio=Decimal("2"))]


@pytest.fixture
def report(monkeypatch):
    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(rep, "utf8_stdout", lambda: None)
    monkeypatch.setattr(rep, "load_config", lambda: {})
    monkeypatch.setattr(rep, "connect", lambda cfg: _Conn())
    monkeypatch.setattr(rep, "collect_fresh_prices", lambda: None)
    monkeypatch.setattr(rep, "fetch_all", lambda conn, q, p=None: [])        # income rows
    monkeypatch.setattr(rep, "get_snapshot", lambda conn, mode: rep.Snapshot(
        ts=T_NOW, prices={"AAA": {"symbol": "AAA", "ts": T_NOW, "last_price": Decimal("50"),
                                  "bid": None, "ask": None, "source": "test"}}))
    monkeypatch.setattr(rep, "get_prev_snapshot_map", lambda conn, ts: {"AAA": (T_PREV, 100.0)})
    monkeypatch.setattr(rep, "get_day_start_snapshot_map", lambda conn, ts: (T_PREV, {"AAA": 100.0}))

    def run(actions, argv=("--mode", "eod")):
        monkeypatch.setattr(rep.ledger_inputs, "load", lambda conn, **kw: ledger_inputs.prepare(LOTS, actions))
        monkeypatch.setattr(sys, "argv", ["report_portfolio_db.py", *argv])
        rep.main()

    return run


def _lines(capsys, label):
    return [ln for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith(label)]


def test_a_pure_split_between_two_quotes_is_a_zero_delta(report, capsys):
    report(SPLIT)
    out = capsys.readouterr().out
    deltas = [ln for ln in out.splitlines() if ln.strip().startswith("Delta:")]
    assert len(deltas) == 2, out                       # since last snapshot, since day start
    for ln in deltas:
        assert "-1,000" not in ln and "0.00" in ln, ln
    assert "1,000.00" in next(ln for ln in out.splitlines() if "Portfolio Value" in ln)


def test_without_a_recorded_split_the_same_quotes_are_a_real_loss(report, capsys):
    """The control: no action recorded, so 100 → 50 on ten shares is −500."""
    report([])
    out = capsys.readouterr().out
    deltas = [ln for ln in out.splitlines() if ln.strip().startswith("Delta:")]
    assert all("-500.00" in ln for ln in deltas), deltas
