"""Undefined metrics are null with a stated reason — never a silent 0.0.

Zero and "unknown" are different facts. A 0% return is a flat period; a return
with no cost basis to divide by has no answer at all. Reporting the second as
the first is undetectable downstream, so every ratio whose denominator is
missing returns None and records why in ``null_reasons``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from app.mcp.services import cutoff as cutoff_service

FIXED_TS = datetime(2026, 8, 12, 13, 5, tzinfo=timezone.utc)


# ────────────────────────── kpis ──────────────────────────


class TestRatioHelper:
    def test_returns_none_on_zero_denominator(self):
        from app.mcp.services import kpis
        assert kpis._ratio_pct(100.0, 0.0) is None

    def test_returns_none_on_missing_denominator(self):
        from app.mcp.services import kpis
        assert kpis._ratio_pct(0.0, 0) is None

    def test_computes_normally_otherwise(self):
        from app.mcp.services import kpis
        assert kpis._ratio_pct(50.0, 200.0) == pytest.approx(25.0)

    def test_genuine_zero_numerator_is_still_zero(self):
        """A flat period must report 0.0, not None — the distinction only
        applies to a missing denominator."""
        from app.mcp.services import kpis
        assert kpis._ratio_pct(0.0, 200.0) == 0.0


class TestNullReasons:
    def test_no_cost_basis_covers_every_return_metric(self):
        from app.mcp.services import kpis
        reasons = kpis._null_reasons(cost_basis=0.0, prev_total_value=100.0)
        assert reasons["unrealized_pct"] == "no_cost_basis"
        assert reasons["total_return_pct"] == "no_cost_basis"
        assert reasons["total_return_with_income_pct"] == "no_cost_basis"
        assert "daily_change_pct" not in reasons

    def test_no_prior_price_is_reported_separately(self):
        from app.mcp.services import kpis
        reasons = kpis._null_reasons(cost_basis=1000.0, prev_total_value=0.0)
        assert reasons == {"daily_change_pct": "no_prior_price"}

    def test_empty_when_everything_is_defined(self):
        from app.mcp.services import kpis
        assert kpis._null_reasons(cost_basis=1000.0, prev_total_value=900.0) == {}


def test_empty_kpis_nulls_percentages_but_keeps_zero_totals(env_token, fake_db, monkeypatch):
    """No positions: the absolute totals really are zero, the ratios are not."""
    from app.mcp.services import kpis

    monkeypatch.setattr(kpis, "_cash_totals", lambda *_a, **_kw: (0.0, []))
    monkeypatch.setattr(kpis, "_watchlist_count", lambda: 0)
    monkeypatch.setattr(kpis, "_income_total", lambda *_a, **_kw: 0.0)

    out = kpis._empty_kpis("fifo", cutoff_service.Cutoff(ts=FIXED_TS))

    assert out["market_value"] == 0.0
    assert out["cost_basis"] == 0.0
    assert out["unrealized_pnl"] == 0.0
    for field in ("unrealized_pct", "total_return_pct",
                  "total_return_with_income_pct", "daily_change_pct"):
        assert out[field] is None, field
        assert field in out["null_reasons"]


# ────────────────────────── positions ──────────────────────────


def test_positions_summary_empty_returns_null_ratios(env_token, fake_db, monkeypatch):
    from app.mcp.services import positions

    monkeypatch.setattr(
        positions, "positions_dataframe", lambda *a, **kw: pd.DataFrame()
    )
    out = positions.positions_summary("fifo")

    assert out["market_value"] == 0.0
    assert out["unrealized_pct"] is None
    assert out["total_return_pct"] is None
    assert out["null_reasons"]["total_return_pct"] == "no_cost_basis"


def test_positions_summary_zero_cost_basis_returns_null_ratios(
    env_token, fake_db, monkeypatch
):
    """Every position closed: market value and cost basis are both zero, so the
    return percentages are undefined rather than flat."""
    from app.mcp.services import positions

    df = pd.DataFrame([{
        "symbol": "AAA", "qty": 0.0, "open_cost": 0.0, "market_value": 0.0,
        "unrealized_pnl": 0.0, "realized_pnl": 250.0,
    }])
    monkeypatch.setattr(positions, "positions_dataframe", lambda *a, **kw: df)
    out = positions.positions_summary("fifo")

    assert out["realized_pnl"] == 250.0
    assert out["unrealized_pct"] is None
    assert out["total_return_pct"] is None


def test_pnl_by_symbol_closed_position_has_no_return_on_cost(
    env_token, fake_db, monkeypatch
):
    from app.mcp.services import pnl, positions as positions_service

    df = pd.DataFrame([{
        "symbol": "PRIM", "qty": 0.0, "open_cost": 0.0,
        "unrealized_pnl": 0.0, "realized_pnl": -35.0,
    }])
    monkeypatch.setattr(
        positions_service, "positions_dataframe", lambda *a, **kw: df
    )
    rows = pnl.pnl_by_symbol("fifo")

    assert rows[0]["realized_pnl"] == -35.0
    assert rows[0]["total_return_pct"] is None


# ────────────────────────── rendering ──────────────────────────


def test_summary_resource_renders_nulls_as_dashes():
    from app.mcp.resources.summary import _pct
    assert _pct(None) == "—"
    assert _pct(12.5) == "+12.50%"


def test_prompt_number_helper_renders_nulls_as_dashes():
    from app.mcp.prompts.prompts import _num
    assert _num(None, 4) == "—"
    assert _num(0.2512, 4) == "0.2512"
