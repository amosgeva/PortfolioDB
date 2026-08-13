"""Fees service tests — total, per-symbol / per-account breakdown, fee drag.

Pure-function tests: the fee rows come from the in-memory ``fake_db`` fixture
and the cost-basis denominator is monkeypatched on positions_service, so no
live Postgres is needed.
"""

from __future__ import annotations

from datetime import date

import pytest


def test_fees_summary_totals_and_breakdown(env_token, monkeypatch, fake_db):
    from app.mcp.services import fees, positions as positions_service

    monkeypatch.setattr(
        positions_service, "positions_summary", lambda *a, **kw: {"cost_basis": 1000.0}
    )
    # One SELECT returns (symbol, account, fees) rows.
    fake_db(responses=[(
        ["symbol", "account", "fees"],
        [
            ("NVDA", "IBKR", 2.5),
            ("NVDA", "IBKR", 1.5),
            ("AAPL", "IBKR", 0.0),   # zero-fee lot: counts toward nothing
            ("MSFT", "ROTH", 4.0),
        ],
    )])

    out = fees.fees_summary()
    assert out["total_fees"] == pytest.approx(8.0)
    assert out["fee_count"] == 3            # the 0.0-fee lot is excluded
    assert out["cost_basis"] == 1000.0
    assert out["fee_drag_pct"] == pytest.approx(0.8)

    by_sym = {r["symbol"]: r for r in out["by_symbol"]}
    assert by_sym["NVDA"]["fees"] == pytest.approx(4.0)
    assert by_sym["NVDA"]["count"] == 2
    assert by_sym["AAPL"]["fees"] == 0.0
    assert by_sym["AAPL"]["count"] == 0

    by_acct = {r["account"]: r for r in out["by_account"]}
    assert by_acct["IBKR"]["fees"] == pytest.approx(4.0)
    assert by_acct["ROTH"]["fees"] == pytest.approx(4.0)


def test_fees_summary_empty(env_token, monkeypatch, fake_db):
    from app.mcp.services import fees, positions as positions_service

    monkeypatch.setattr(
        positions_service, "positions_summary", lambda *a, **kw: {"cost_basis": 0.0}
    )
    fake_db(responses=[(["symbol", "account", "fees"], [])])

    out = fees.fees_summary()
    assert out["total_fees"] == 0.0
    assert out["fee_count"] == 0
    assert out["fee_drag_pct"] == 0.0       # no division by zero
    assert out["by_symbol"] == []
    assert out["by_account"] == []


def test_fees_summary_echoes_window(env_token, monkeypatch, fake_db):
    from app.mcp.services import fees, positions as positions_service

    monkeypatch.setattr(
        positions_service, "positions_summary", lambda *a, **kw: {"cost_basis": 100.0}
    )
    fake_db(responses=[(["symbol", "account", "fees"], [("NVDA", "IBKR", 1.0)])])

    out = fees.fees_summary(since=date(2026, 1, 1), until=date(2026, 3, 31))
    assert out["since"] == "2026-01-01"
    assert out["until"] == "2026-03-31"
    assert out["total_fees"] == pytest.approx(1.0)
