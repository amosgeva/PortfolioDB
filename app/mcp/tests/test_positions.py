"""Tests for the positions service.

Most position behavior is already covered indirectly by the KPI parity test
(test_kpi_parity.py). This module focuses on the position-specific shape:
held_only filter, per-account breakdown, open-lots reporting.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

LOTS = [
    (1, "NVDA", "IBKR",  "BUY",  date(2025, 1, 10), "5", "120.00", "1.00"),
    (2, "NVDA", "Blink", "BUY",  date(2025, 4, 1),  "2", "130.00", "0.00"),
    (3, "AAPL", "IBKR",  "BUY",  date(2025, 2, 15), "10", "180.00", "1.00"),
    (4, "AAPL", "IBKR",  "SELL", date(2025, 9, 1),  "10", "200.00", "1.00"),
    # AAPL is now flat
]
LATEST = {"NVDA": 165.50, "AAPL": 195.00}


@pytest.fixture
def patched(monkeypatch, env_token, fake_db):
    """Patch the few seams the positions service touches.

    ``fake_db`` matters even though every data function below is stubbed:
    positions_dataframe still opens ``with get_conn() as conn`` to hand a
    connection to ``_fetch_lots``, which ignores it. Without the fake pool
    these tests quietly required a live Postgres — and the read-only role —
    to run at all.
    """
    from app.mcp.services import positions as positions_service
    from app.mcp.services import prices as prices_service

    def fake_fetch_lots(conn, *, account=None, symbol=None, as_of=None):
        return [
            {"id": r[0], "symbol": r[1], "account": r[2], "side": r[3],
             "trade_date": r[4], "quantity": Decimal(r[5]),
             "price": Decimal(r[6]), "fees": Decimal(r[7])}
            for r in LOTS
            if (symbol is None or r[1] == symbol.upper())
            and (account is None or r[2] == account)
            and (as_of is None or r[4] <= as_of)
        ]
    monkeypatch.setattr(positions_service, "_fetch_lots", fake_fetch_lots)
    monkeypatch.setattr(
        prices_service, "latest_price_map_with_ts",
        lambda **_kw: {s: {"last_price": p, "ts": "2026-05-20T19:00:00+00:00"}
                 for s, p in LATEST.items()},
    )


def test_current_positions_held_only_excludes_zero_qty(patched):
    from app.mcp.services import positions
    rows = positions.current_positions("fifo", held_only=True)
    symbols = sorted(r["symbol"] for r in rows)
    # AAPL was fully sold — must NOT appear when held_only=True
    assert symbols == ["NVDA"]


def test_current_positions_unheld_returned_when_flag_off(patched):
    from app.mcp.services import positions
    rows = positions.current_positions("fifo", held_only=False)
    symbols = sorted(r["symbol"] for r in rows)
    assert symbols == ["AAPL", "NVDA"]
    aapl = next(r for r in rows if r["symbol"] == "AAPL")
    assert aapl["qty"] == 0
    # AAPL is closed → realized P&L = (200-180.1)*10 - 1 ≈ 198 (approx)
    assert aapl["realized_pnl"] > 0


def test_positions_summary_totals(patched):
    from app.mcp.services import positions
    s = positions.positions_summary("fifo")
    # market_value = 7 * 165.50 = 1158.50
    assert s["market_value"] == pytest.approx(7 * 165.50)
    # cost_basis: NVDA(5*120+1)=601 + NVDA(2*130)=260 + AAPL(10*180+1)=1801 - AAPL closed cost basis
    # AAPL fully closed so open_cost = 0 for AAPL.
    # NVDA open_cost = 601 + 260 = 861 (across two accounts).
    assert s["cost_basis"] == pytest.approx(601 + 260)
    assert s["active_symbols"] == 1


def test_position_detail_per_account(patched):
    from app.mcp.services import positions
    detail = positions.position_detail("NVDA", "fifo")
    assert detail["symbol"] == "NVDA"
    # Two accounts.
    accts = sorted(b["account"] for b in detail["per_account"])
    assert accts == ["Blink", "IBKR"]
    # Merged row present
    assert detail["merged"] is not None
    assert detail["merged"]["qty"] == 7
    # Two open lots
    assert len(detail["open_lots"]) == 2


def test_open_lots_for_nvda(patched):
    from app.mcp.services import positions
    lots = positions.open_lots(symbol="NVDA")
    assert len(lots) == 2
    # Sorted by trade_date then buy_lot_id — IBKR (2025-01-10) before Blink (2025-04-01)
    assert lots[0]["account"] == "IBKR"
    assert lots[0]["qty_remaining"] == 5
    assert lots[1]["account"] == "Blink"
    assert lots[1]["qty_remaining"] == 2
