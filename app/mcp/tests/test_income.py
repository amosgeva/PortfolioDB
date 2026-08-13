"""Income service tests — summaries, grouping, yield-on-cost.

No live DB: income rows come from the in-memory ``fake_db`` fixture and the
cost-basis denominator is monkeypatched on positions_service.
"""

from __future__ import annotations

from datetime import date

import pytest

# Column order matches income._fetch_income's SELECT.
_COLS = ["symbol", "account", "kind", "pay_date", "amount", "tax_withheld"]


def test_income_summary_rejects_bad_group(env_token):
    from app.mcp.services import income
    with pytest.raises(ValueError):
        income.income_summary(group_by="planet")


def test_income_summary_grouped_by_symbol(env_token, fake_db):
    from app.mcp.services import income
    fake_db(responses=[(_COLS, [
        ("NVDA", "IBKR", "DIVIDEND", date(2026, 3, 15), 4.0, 0.6),
        ("NVDA", "IBKR", "DIVIDEND", date(2026, 6, 15), 4.0, 0.6),
        ("MSFT", "ROTH", "DIVIDEND", date(2026, 3, 10), 10.0, 0.0),
    ])])

    out = income.income_summary(group_by="symbol")
    assert out["total_income"] == pytest.approx(18.0)
    assert out["total_tax_withheld"] == pytest.approx(1.2)
    assert out["count"] == 3

    byk = {r["bucket"]: r for r in out["rows"]}
    assert byk["NVDA"]["income"] == pytest.approx(8.0)
    assert byk["NVDA"]["count"] == 2
    assert byk["MSFT"]["income"] == pytest.approx(10.0)
    # sorted by income desc → MSFT first
    assert out["rows"][0]["bucket"] == "MSFT"


def test_income_summary_none_group(env_token, fake_db):
    from app.mcp.services import income
    fake_db(responses=[(_COLS, [
        ("NVDA", "IBKR", "DIVIDEND", date(2026, 3, 15), 4.0, 0.6),
    ])])
    out = income.income_summary(group_by="none")
    assert out["total_income"] == pytest.approx(4.0)
    assert "rows" not in out


def test_yield_on_cost(env_token, monkeypatch, fake_db):
    from app.mcp.services import income, positions as positions_service
    monkeypatch.setattr(
        positions_service, "positions_summary", lambda *a, **kw: {"cost_basis": 1000.0}
    )
    fake_db(responses=[(_COLS, [
        ("NVDA", "IBKR", "DIVIDEND", date(2026, 5, 1), 12.0, 0.0),
        ("MSFT", "ROTH", "DIVIDEND", date(2026, 4, 1), 8.0, 0.0),
    ])])
    out = income.yield_on_cost(today=date(2026, 6, 5))
    assert out["ttm_income"] == pytest.approx(20.0)
    assert out["cost_basis"] == 1000.0
    assert out["yield_on_cost_pct"] == pytest.approx(2.0)


def test_yield_on_cost_zero_basis(env_token, monkeypatch, fake_db):
    from app.mcp.services import income, positions as positions_service
    monkeypatch.setattr(
        positions_service, "positions_summary", lambda *a, **kw: {"cost_basis": 0.0}
    )
    fake_db(responses=[(_COLS, [])])
    out = income.yield_on_cost(today=date(2026, 6, 5))
    assert out["yield_on_cost_pct"] == 0.0   # no division by zero
