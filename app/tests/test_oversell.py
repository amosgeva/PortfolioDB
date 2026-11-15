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


# ── 1.8.0: the account is matched exactly, exact arithmetic, same-day order ──


def _with_lots(monkeypatch, lots, actions=()):
    def load(conn, *, symbol=None, account=None, as_of=None):
        rows = [r for r in lots
                if (symbol is None or r["symbol"] == symbol.upper())
                and (account is None or r["account"] == account)
                and (as_of is None or r["trade_date"] <= as_of)]
        acts = [a for a in actions if as_of is None or a.ex_date <= as_of]
        return ledger_inputs.prepare(rows, acts)
    monkeypatch.setattr(oversell.ledger_inputs, "load", load)


def _lot(i, side, qty, day, account="IBKR", symbol="AAA"):
    return {"id": i, "symbol": symbol, "account": account, "side": side, "trade_date": day,
            "quantity": Decimal(str(qty)), "price": Decimal("100"), "fees": Decimal("0")}


def test_a_sale_with_no_account_is_checked_against_the_unassigned_account(monkeypatch):
    """Audit F-2. Ten shares in IBKR; a sale of ten entered with no account.
    None used to mean "every account", so the check passed — and FIFO, which
    matches per (symbol, account), then dropped the sale."""
    _with_lots(monkeypatch, [_lot(1, "BUY", 10, D1)])
    check = oversell.check_sell(None, "AAA", None, D2, 10)
    assert not check.ok and check.held == 0 and check.shortfall == 10
    assert "(no account)" in check.message() and "holds 0" in check.message()


def test_shares_in_the_unassigned_account_do_count_for_it(monkeypatch):
    _with_lots(monkeypatch, [_lot(1, "BUY", 10, D1, account=None)])
    assert oversell.oversell_warning(None, "AAA", None, D2, 10) is None


def test_fractional_shares_do_not_trip_a_false_warning(monkeypatch):
    """Audit F-13: 0.7 + 0.1 was 0.7999999999999999 in floats, so selling 0.8
    warned."""
    _with_lots(monkeypatch, [_lot(1, "BUY", "0.7", D1), _lot(2, "BUY", "0.1", D1)])
    assert oversell.oversell_warning(None, "AAA", "IBKR", D2, Decimal("0.8")) is None
    assert oversell.open_quantity(None, "AAA", "IBKR", D2) == Decimal("0.8")


def test_a_buy_the_same_day_covers_the_sale(monkeypatch):
    """Same-day order is BUY before SELL (audit F-1), so a buy recorded that day
    counts for a sale dated that day, whatever order they were entered in."""
    _with_lots(monkeypatch, [_lot(1, "BUY", 5, D2)])
    assert oversell.oversell_warning(None, "AAA", "IBKR", D2, 5) is None


def test_a_backdated_sale_that_starves_a_later_one_is_reported(monkeypatch):
    """Ten bought D1, ten sold D3 (fine). Entering a further sale of 4 dated
    D2 leaves only 6 for the D3 sale: 4 of it would be dropped."""
    _with_lots(monkeypatch, [_lot(1, "BUY", 10, D1), _lot(7, "SELL", 10, D3)])
    check = oversell.check_sell(None, "AAA", "IBKR", D2, 4)
    assert check.shortfall == 0                    # D2 itself held ten
    assert check.later_shortfall == 4 and check.later_lot_ids == (7,)
    msg = check.message()
    assert "lot id 7" in msg and "4 AAA" in msg


def test_check_sell_is_ok_for_a_covered_sale(monkeypatch):
    _with_lots(monkeypatch, LOTS, ACTIONS)
    check = oversell.check_sell(None, "AAA", "IBKR", D3, "16")
    assert check.ok and check.message() is None
    assert check.held == Decimal("16") and isinstance(check.quantity, Decimal)


def test_a_pre_split_sale_is_judged_in_pre_split_units(monkeypatch):
    """Ten held before the 2:1; selling 12 on D1 (pre-split) is an oversell of
    2, not a sale within twenty restated shares."""
    _with_lots(monkeypatch, LOTS, ACTIONS)
    check = oversell.check_sell(None, "AAA", "IBKR", D1, 12)
    assert check.held == 10 and check.shortfall == 2
