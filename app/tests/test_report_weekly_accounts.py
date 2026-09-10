"""The weekly report handles unnamed and named accounts together.

Re-audit N01: `positions_as_of` sorted `(account, symbol)` keys with the
default ordering, and Python refuses to compare None with a string, so a
ledger with one unnamed account and one named account crashed the report
before it printed anything. The old SQL aggregation never had that problem.

1.7.2 fixed that helper; the account-totals loop in `main()` then sorted the
same union of accounts (plus the cash accounts) the same way and crashed two
sections later. The helper tests below could not see it, so the second half
of this file runs the whole command.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ledger_inputs  # noqa: E402
import report_weekly_db as weekly  # noqa: E402
from test_report_weekly_split import BEFORE, END_TS, START_TS, TUE, _run, _section  # noqa: E402
from test_report_weekly_split import _lot as _raw_lot  # noqa: E402

D1, D2 = date(2026, 1, 5), date(2026, 1, 6)


def _lot(i, account, symbol, side, qty, day=D1):
    return {"id": i, "symbol": symbol, "account": account, "side": side, "trade_date": day,
            "quantity": Decimal(qty), "price": Decimal("10"), "fees": Decimal("0")}


LOTS = [
    _lot(1, None, "AAA", "BUY", "5"),
    _lot(2, "TEST", "AAA", "BUY", "7"),
    _lot(3, "", "BBB", "BUY", "3"),          # empty string is an account of its own
    _lot(4, "TEST", "BBB", "BUY", "4"),
    _lot(5, "TEST", "BBB", "SELL", "4"),     # closes TEST/BBB → dropped as zero
    _lot(6, None, "CCC", "BUY", "1", day=D2),
]


@pytest.fixture(autouse=True)
def fake_ledger(monkeypatch):
    def load(conn, *, symbol=None, account=None, as_of=None, units="as_of"):
        rows = [r for r in LOTS if as_of is None or r["trade_date"] <= as_of]
        return ledger_inputs.prepare(rows, [], as_of=as_of)
    monkeypatch.setattr(weekly.ledger_inputs, "load", load)


def test_unnamed_and_named_accounts_coexist():
    out = weekly.positions_as_of(None, D2)
    assert out == {
        (None, "AAA"): Decimal("5"),
        (None, "CCC"): Decimal("1"),
        ("", "BBB"): Decimal("3"),
        ("TEST", "AAA"): Decimal("7"),
    }


def test_ordering_is_stable_and_unnamed_first():
    keys = list(weekly.positions_as_of(None, D2))
    assert keys == [(None, "AAA"), (None, "CCC"), ("", "BBB"), ("TEST", "AAA")]


def test_none_and_empty_string_are_not_merged():
    out = weekly.positions_as_of(None, D2)
    assert (None, "AAA") in out and ("", "BBB") in out
    assert (None, "BBB") not in out and ("", "AAA") not in out


def test_as_of_excludes_later_trades():
    out = weekly.positions_as_of(None, D1)
    assert (None, "CCC") not in out
    assert out[(None, "AAA")] == Decimal("5")


class TestWholeCommand:
    LOTS = [
        _raw_lot(1, "BBB", "BUY", "5", "10", BEFORE, account=None),
        _raw_lot(2, "BBB", "BUY", "7", "10", BEFORE, account="TEST"),
        _raw_lot(3, "BBB", "BUY", "3", "10", BEFORE, account=""),
    ]
    CASH = [
        {"account": None, "cash": Decimal("100"), "ts": START_TS - timedelta(days=1)},
        {"account": "TEST", "cash": Decimal("50"), "ts": START_TS - timedelta(days=1)},
        {"account": "CASHONLY", "cash": Decimal("25"), "ts": START_TS - timedelta(days=1)},
        {"account": "CASHONLY", "cash": Decimal("30"), "ts": END_TS - timedelta(hours=1)},
    ]

    def test_the_report_completes_with_unnamed_blank_named_and_cash_only_accounts(self, monkeypatch, capsys):
        out = _run(monkeypatch, capsys, self.LOTS, cash=self.CASH)
        totals = _section(out, "ACCOUNT TOTALS")
        # Unnamed first, then the blank name, then the named ones; the
        # cash-only account is a row of its own; nothing is merged.
        assert totals == [
            "(no account): $150.00 → $160.00 ($10.00)",       # 5 BBB × 10 → 12, plus $100 cash
            "(blank account): $30.00 → $36.00 ($6.00)",       # 3 BBB × 10 → 12
            "CASHONLY: $25.00 → $30.00 ($5.00)",
            "TEST: $120.00 → $134.00 ($14.00)",               # 7 BBB × 10 → 12, plus $50 cash
        ]
        assert "Start securities: $150.00 | cash: $175.00 | total: $325.00" in out
        assert "End securities:   $180.00 | cash: $180.00 | total: $360.00" in out
        assert "Weekly change:    $35.00" in out
        assert "TRADES THIS WEEK" in out and "No FD data" in out, "the report ran to its last section"

    def test_an_unnamed_trade_is_listed_under_its_label(self, monkeypatch, capsys):
        out = _run(monkeypatch, capsys, self.LOTS + [_raw_lot(9, "BBB", "BUY", "1", "11", TUE, account=None)])
        assert "2026-03-03 (no account) BUY BBB 1.0000 @ $11.00" in out


def test_value_positions_groups_by_the_original_account_value():
    positions = weekly.positions_as_of(None, D2)
    by_symbol, by_account, missing = weekly.value_positions(positions, {"AAA": Decimal("2"), "CCC": Decimal("1")})
    assert by_symbol["AAA"] == Decimal("24")            # (5 + 7) × 2
    assert by_account[None] == Decimal("11")            # 5×2 + 1×1
    assert by_account["TEST"] == Decimal("14")
    assert missing == ["BBB"]
