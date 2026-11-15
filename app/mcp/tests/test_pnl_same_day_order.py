"""The MCP realized-P&L replay processes a day's BUYs before its SELLs.

Audit F-1: the engines sorted by (trade_date, id), so a same-day SELL stored
before its BUY was dropped. The engines now use ledger_core.processing_key;
the average-cost replay in pnl._all_realized_matches had its own copy of the
old sort and is held to the same rule here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

DAY = date(2026, 3, 2)
COLS = ["id", "symbol", "account", "side", "trade_date", "quantity", "price", "fees"]
# The SELL was stored first (id 1).
ROWS = [
    (1, "AAA", "IBKR", "SELL", DAY, Decimal("10"), Decimal("110"), Decimal("0")),
    (2, "AAA", "IBKR", "BUY", DAY, Decimal("10"), Decimal("100"), Decimal("0")),
]


@pytest.fixture
def patched(monkeypatch, env_token, fake_db):
    fake_db(responses=[(COLS, ROWS)], cycle=True)
    from app.mcp.services import pnl as pnl_service
    monkeypatch.setattr(pnl_service.corporate_actions, "fetch_actions", lambda conn: [])


@pytest.mark.parametrize("method", ["fifo", "avg"])
def test_a_same_day_sale_stored_first_is_matched(patched, method):
    from app.mcp.services import pnl
    matches = pnl._all_realized_matches(method)
    assert sum(m["qty"] for m in matches) == pytest.approx(10.0)
    assert sum(m["realized_pnl"] for m in matches) == pytest.approx(100.0)
