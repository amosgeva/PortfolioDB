"""The whole weekly report, run over a week that contains a split.

1.7.2 re-audit, F03 case A: `positions_as_of()` restated share counts into
each date's units while `price_map()` returned raw quotes, and the trade
branches consumed raw rows, so a pure 2:1 split inside the week was printed as
a 50% loss with a `qty Δ +10`. These tests run the real `main()` — the helper
tests that existed did not reach the arithmetic that was wrong — with the
database replaced at `fetch_all` / `ledger_inputs.load` and the clock fixed.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ledger_inputs  # noqa: E402
import report_weekly_db as weekly  # noqa: E402
from corporate_actions import CorporateAction  # noqa: E402

MON, TUE, WED, THU, FRI, SAT = (date(2026, 3, 2) + timedelta(days=i) for i in range(6))
BEFORE = date(2026, 2, 20)
NOW = datetime.combine(SAT, time(10, 0)).replace(tzinfo=weekly.IL_TZ)
START_TS = datetime.combine(MON, time(23, 0)).replace(tzinfo=weekly.IL_TZ)
END_TS = datetime.combine(FRI, time(23, 0)).replace(tzinfo=weekly.IL_TZ)


def _lot(i, symbol, side, qty, price, day, account="IBKR"):
    return {"id": i, "symbol": symbol, "account": account, "side": side, "trade_date": day,
            "quantity": Decimal(qty), "price": Decimal(price), "fees": Decimal("0"), "notes": None}


# AAA splits 2:1 on Wednesday; CCC reverse-splits 1:4 the same day; BBB is an
# ordinary holding that gains 20% so the contributor block has a real entry.
ACTIONS = [
    CorporateAction(symbol="AAA", kind="SPLIT", ex_date=WED, ratio=Decimal("2")),
    CorporateAction(symbol="CCC", kind="REVERSE_SPLIT", ex_date=WED, ratio=Decimal("0.25")),
]
HELD_BEFORE = [
    _lot(1, "AAA", "BUY", "10", "100", BEFORE),
    _lot(2, "BBB", "BUY", "5", "10", BEFORE),
    _lot(3, "CCC", "BUY", "8", "10", BEFORE),
]
# Entered in the units of their day: the Tuesday sale is 2 pre-split shares at
# $104 (→ 4 at $52), the Thursday buy is 2 post-split shares at $52.
WEEK_TRADES = [
    _lot(4, "AAA", "SELL", "2", "104", TUE),
    _lot(5, "AAA", "BUY", "2", "52", THU),
]
# Raw quotes as the collector stored them: AAA 100 → 50 and CCC 10 → 40 are the
# actions, BBB 10 → 12 is a move.
PRICES = {
    START_TS: {"AAA": Decimal("100"), "BBB": Decimal("10"), "CCC": Decimal("10")},
    END_TS: {"AAA": Decimal("50"), "BBB": Decimal("12"), "CCC": Decimal("40")},
}


class _FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run(monkeypatch, capsys, lots):
    def fake_load(conn, *, symbol=None, account=None, as_of=None, units="as_of"):
        rows = [r for r in lots if as_of is None or r["trade_date"] <= as_of]
        return ledger_inputs.prepare(rows, ACTIONS, as_of=None if units == "current" else as_of)

    def fake_fetch_all(conn, sql, params=None):
        sql_one = " ".join(sql.split())
        if "MIN(ts)" in sql_one:
            return [{"ts": START_TS}]
        if "MAX(ts)" in sql_one:
            return [{"ts": END_TS}]
        if "FROM price_snapshots WHERE ts=%s" in sql_one:
            return [{"symbol": s, "last_price": p} for s, p in PRICES[params[0]].items()]
        if "FROM cash_snapshots" in sql_one:
            return []
        if "FROM lots" in sql_one:
            lo, hi = params
            return [dict(r) for r in lots if lo <= r["trade_date"] <= hi]
        raise AssertionError(f"unexpected query: {sql_one}")

    no_fd = SimpleNamespace(
        ETF_SYMBOLS=set(),
        latest_facts=lambda conn, sym: None, latest_metrics=lambda conn, sym: None,
        recent_financials=lambda conn, sym, kind, limit=1: [],
        recent_earnings=lambda conn, sym, limit=1: [], recent_filings=lambda conn, sym, limit=20: [],
        recent_news=lambda conn, sym, limit=10: [],
    )
    monkeypatch.setattr(weekly, "datetime", _FixedClock)
    monkeypatch.setattr(weekly, "load_config", lambda: {})
    monkeypatch.setattr(weekly, "connect", lambda cfg: _Conn())
    monkeypatch.setattr(weekly, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(weekly.ledger_inputs, "load", fake_load)
    monkeypatch.setattr(weekly, "fd_store", no_fd)
    weekly.main()
    return capsys.readouterr().out


def _section(out: str, header: str) -> list[str]:
    """The lines of one report block: from its header to the next blank line."""
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if header in ln) + 1
    block = []
    for ln in lines[start:]:
        if not ln.strip():
            break
        block.append(ln)
    return block


def _line(out: str, prefix: str, header: str = "TOP CONTRIBUTORS") -> str:
    matches = [ln for ln in _section(out, header) if ln.startswith(prefix)]
    assert len(matches) == 1, (header, prefix, matches)
    return matches[0]


def test_a_pure_split_contributes_nothing_and_changes_no_value(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, HELD_BEFORE)
    # Start and end are both $1,000 (AAA) + $50 → $60 (BBB) + $80 (CCC): only BBB moved.
    assert "Weekly change:    $10.00" in out
    assert "Start securities: $1,130.00" in out and "End securities:   $1,140.00" in out
    aaa = _line(out, "AAA:")
    assert aaa.startswith("AAA: $0.00 (+0.00%) | $1,000.00 → $1,000.00")
    assert "qty Δ" not in aaa, "the extra shares are the split, not a purchase"
    ccc = _line(out, "CCC:")
    assert ccc.startswith("CCC: $0.00 (+0.00%) | $80.00 → $80.00") and "qty Δ" not in ccc
    assert _line(out, "BBB:").startswith("BBB: $10.00 (+20.00%) | $50.00 → $60.00")
    # The action is reported as what it is, in its own block.
    assert "🔀 CORPORATE ACTIONS THIS WEEK" in out
    assert "AAA: SPLIT ×2 on 2026-03-04" in out and "CCC: REVERSE_SPLIT ×0.25 on 2026-03-04" in out


def test_trades_on_both_sides_of_the_ex_date_are_measured_in_one_unit_basis(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, HELD_BEFORE + WEEK_TRADES)
    # Held 20 (post-split) at a flat $50: no price move. Sold 4 at $52 against
    # the $50 start quote: +$8. Bought 2 at $52, worth $50 at the end: −$4.
    aaa = _line(out, "AAA:")
    assert aaa.startswith("AAA: $4.00 (+0.40%) | $1,000.00 → $900.00 | trade P&L $4.00 | qty Δ -2.0000")
    # The listing shows what was entered, in the units of the day.
    assert "2026-03-03 IBKR SELL AAA 2.0000 @ $104.00" in out
    assert "2026-03-05 IBKR BUY AAA 2.0000 @ $52.00" in out


def test_the_report_runs_to_its_last_section(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, HELD_BEFORE + WEEK_TRADES)
    assert "⚠️ No FD data on file for: AAA, BBB, CCC" in out.replace("\n", " ") or "No FD data" in out


def test_a_week_without_actions_prints_no_action_block(monkeypatch, capsys):
    monkeypatch.setattr(weekly.ledger_inputs, "prepare", ledger_inputs.prepare)
    lots = [_lot(1, "BBB", "BUY", "5", "10", BEFORE)]
    out = _run(monkeypatch, capsys, lots)
    assert "CORPORATE ACTIONS" not in out
    assert _line(out, "BBB:").startswith("BBB: $10.00 (+20.00%)")


@pytest.mark.parametrize("ts,expected", [(START_TS, Decimal("50")), (END_TS, Decimal("50"))])
def test_price_map_restates_through_the_ledger(monkeypatch, ts, expected):
    monkeypatch.setattr(weekly, "fetch_all",
                        lambda conn, sql, params=None: [{"symbol": "AAA", "last_price": PRICES[params[0]]["AAA"]}])
    ledger = ledger_inputs.prepare(HELD_BEFORE, ACTIONS, as_of=FRI)
    assert weekly.price_map(object(), ts, ledger) == {"AAA": expected}
    # Without a ledger the raw quote comes back, as before.
    assert weekly.price_map(object(), ts) == {"AAA": PRICES[ts]["AAA"]}
