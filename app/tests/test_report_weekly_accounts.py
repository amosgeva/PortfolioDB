"""The weekly report handles unnamed and named accounts together.

Re-audit N01: `positions_as_of` sorted `(account, symbol)` keys with the
default ordering, and Python refuses to compare None with a string, so a
ledger with one unnamed account and one named account crashed the report
before it printed anything. The old SQL aggregation never had that problem.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ledger_inputs  # noqa: E402
import report_weekly_db as weekly  # noqa: E402

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


def test_value_positions_groups_by_the_original_account_value():
    positions = weekly.positions_as_of(None, D2)
    by_symbol, by_account, missing = weekly.value_positions(positions, {"AAA": Decimal("2"), "CCC": Decimal("1")})
    assert by_symbol["AAA"] == Decimal("24")            # (5 + 7) × 2
    assert by_account[None] == Decimal("11")            # 5×2 + 1×1
    assert by_account["TEST"] == Decimal("14")
    assert missing == ["BBB"]
