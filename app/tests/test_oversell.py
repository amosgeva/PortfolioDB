"""A SELL bigger than the position is warned about when it is entered.

The FIFO engine's truncation warning fires at read time; these pin the check
the write paths run before the INSERT, through the prepared ledger (so a
pre-split BUY counts in post-split shares) and scoped to the account.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ledger_inputs  # noqa: E402
import oversell  # noqa: E402
from corporate_actions import CorporateAction  # noqa: E402

D1, D2, D3 = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)
LOTS = [
    {"id": 1, "symbol": "AAA", "account": "IBKR", "side": "BUY", "trade_date": D1,
     "quantity": Decimal("10"), "price": Decimal("100"), "fees": Decimal("0")},
    {"id": 2, "symbol": "AAA", "account": "SCHW", "side": "BUY", "trade_date": D1,
     "quantity": Decimal("2"), "price": Decimal("100"), "fees": Decimal("0")},
    {"id": 3, "symbol": "AAA", "account": "IBKR", "side": "SELL", "trade_date": D3,
     "quantity": Decimal("4"), "price": Decimal("55"), "fees": Decimal("0")},
]
ACTIONS = [CorporateAction(symbol="AAA", kind="SPLIT", ex_date=D2, ratio=Decimal("2"))]


@pytest.fixture(autouse=True)
def fake_ledger(monkeypatch):
    def load(conn, *, symbol=None, account=None, as_of=None):
        rows = [r for r in LOTS
                if (symbol is None or r["symbol"] == symbol.upper())
                and (account is None or r["account"] == account)
                and (as_of is None or r["trade_date"] <= as_of)]
        acts = [a for a in ACTIONS if as_of is None or a.ex_date <= as_of]
        return ledger_inputs.prepare(rows, acts)
    monkeypatch.setattr(oversell.ledger_inputs, "load", load)


def test_open_quantity_is_per_account_and_split_adjusted():
    assert oversell.open_quantity(None, "AAA", "IBKR", D1) == Decimal("10")   # pre-split day
    assert oversell.open_quantity(None, "AAA", "IBKR", D2) == Decimal("20")   # after the 2:1
    assert oversell.open_quantity(None, "AAA", "IBKR", D3) == Decimal("16")   # minus the sale that day
    assert oversell.open_quantity(None, "AAA", "SCHW", D3) == Decimal("4")
    assert oversell.open_quantity(None, "AAA", "NONE", D3) == Decimal("0")


def test_a_sale_within_the_position_is_silent():
    assert oversell.oversell_warning(None, "AAA", "IBKR", D3, Decimal("16")) is None
    assert oversell.oversell_warning(None, "aaa", "IBKR", D3, 5) is None


def test_a_sale_beyond_the_position_names_the_numbers_and_the_likely_causes():
    msg = oversell.oversell_warning(None, "AAA", "IBKR", D3, Decimal("20"))
    assert msg is not None
    assert "selling 20 AAA in IBKR" in msg
    assert "holds 16" in msg
    assert "Shorts are not supported" in msg
    assert "account" in msg and "trade date" in msg and "BUY" in msg


def test_the_wrong_account_is_the_common_case():
    """Twelve shares exist across two accounts; selling twelve in one of them
    is the mistake the warning exists for."""
    msg = oversell.oversell_warning(None, "AAA", "SCHW", D3, Decimal("12"))
    assert msg is not None and "holds 4" in msg


def test_no_position_at_all():
    msg = oversell.oversell_warning(None, "ZZZ", None, D3, 1)
    assert msg is not None and "holds 0" in msg and "(no account)" in msg
