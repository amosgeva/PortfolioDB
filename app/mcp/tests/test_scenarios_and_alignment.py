"""Stress scenarios (§8.2) and the benchmark alignment guard (§8.1).

Both exist to stop a number being reported more confidently than the data
supports: scenarios are labelled as arithmetic rather than forecast, and the
alignment guard refuses a comparison the observations cannot support.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.mcp.services.cutoff import Cutoff

CUTOFF_TS = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)


def make_cutoff() -> Cutoff:
    return Cutoff(ts=CUTOFF_TS, trade_date=date(2026, 8, 12))


def position(symbol, mv, weight):
    return {"symbol": symbol, "market_value": mv, "weight_pct": weight,
            "qty": 1.0, "open_cost": mv}


# ────────────────────────── stress scenarios ──────────────────────────


@pytest.fixture
def scenarios(env_token, fake_db, monkeypatch):
    from app.mcp.services import analytics, positions as positions_service

    def apply(positions_list, sectors=None, clusters=None, observations=64):
        monkeypatch.setattr(
            positions_service, "current_positions", lambda *a, **kw: positions_list)
        monkeypatch.setattr(
            analytics, "_attribute_map",
            lambda syms, dim: sectors or {p: "Tech" for p in syms})
        monkeypatch.setattr(
            analytics, "correlation_matrix",
            lambda *a, **kw: {"clusters": clusters or {},
                              "observations": observations,
                              "symbols": [p["symbol"] for p in positions_list]})
        return analytics
    return apply


class TestStressScenarios:
    def _book(self):
        return [position("AAA", 500.0, 50.0),
                position("BBB", 300.0, 30.0),
                position("CCC", 200.0, 20.0)]

    def test_largest_holding_decline(self, scenarios):
        analytics = scenarios(self._book())
        out = analytics.stress_scenarios(cutoff=make_cutoff())
        s = next(x for x in out["scenarios"] if x["key"] == "largest_holding_decline")
        assert s["symbols"] == ["AAA"]
        # 30% of 500 = 150, against a 1000 book.
        assert s["portfolio_loss"] == pytest.approx(-150.0)
        assert s["portfolio_loss_pct"] == pytest.approx(-15.0)
        assert s["portfolio_value_after"] == pytest.approx(850.0)

    def test_top_n_decline_covers_n_holdings(self, scenarios):
        analytics = scenarios(self._book())
        out = analytics.stress_scenarios(cutoff=make_cutoff(), top_n=2)
        s = next(x for x in out["scenarios"] if x["key"] == "top_n_decline")
        assert s["symbols"] == ["AAA", "BBB"]
        # 20% of (500+300) = 160.
        assert s["portfolio_loss"] == pytest.approx(-160.0)

    def test_sector_shock_picks_the_heaviest_sector(self, scenarios):
        analytics = scenarios(
            self._book(),
            sectors={"AAA": "Tech", "BBB": "Energy", "CCC": "Energy"},
        )
        out = analytics.stress_scenarios(cutoff=make_cutoff())
        s = next(x for x in out["scenarios"] if x["key"] == "sector_shock")
        # Energy is 300+200=500 vs Tech 500 — tie broken deterministically, but
        # whichever wins must be a real sector holding real weight.
        assert s["sector"] in {"Tech", "Energy"}
        assert s["affected_market_value"] == pytest.approx(500.0)

    def test_cluster_shock_uses_the_largest_cluster(self, scenarios):
        analytics = scenarios(self._book(), clusters={"AAA": ["BBB"], "BBB": ["AAA"]})
        out = analytics.stress_scenarios(cutoff=make_cutoff())
        s = next(x for x in out["scenarios"] if x["key"] == "correlated_cluster_shock")
        assert set(s["symbols"]) == {"AAA", "BBB"}
        assert s["portfolio_loss"] == pytest.approx(-160.0)   # 20% of 800

    def test_cluster_unavailable_without_correlated_pairs(self, scenarios):
        analytics = scenarios(self._book(), clusters={})
        out = analytics.stress_scenarios(cutoff=make_cutoff())
        s = next(x for x in out["scenarios"] if x["key"] == "correlated_cluster_shock")
        assert s["status"] == "unavailable"
        assert s["null_reason"] == "no_cluster_above_threshold"

    def test_cluster_unavailable_without_price_history(self, scenarios):
        analytics = scenarios(self._book(), clusters={}, observations=0)
        out = analytics.stress_scenarios(cutoff=make_cutoff())
        s = next(x for x in out["scenarios"] if x["key"] == "correlated_cluster_shock")
        assert s["null_reason"] == "insufficient_price_history"

    def test_assumptions_are_echoed(self, scenarios):
        """A scenario is only interpretable next to the assumption that made
        it, so the numbers travel together."""
        analytics = scenarios(self._book())
        out = analytics.stress_scenarios(
            cutoff=make_cutoff(), largest_holding_decline_pct=50.0)
        assert out["assumptions"]["largest_holding_decline_pct"] == 50.0
        s = next(x for x in out["scenarios"] if x["key"] == "largest_holding_decline")
        assert s["decline_pct"] == 50.0
        assert s["portfolio_loss"] == pytest.approx(-250.0)

    def test_labelled_analytical_not_forecast(self, scenarios):
        analytics = scenarios(self._book())
        out = analytics.stress_scenarios(cutoff=make_cutoff())
        assert out["basis"] == "analytical_derived"
        assert "not forecasts" in out["disclaimer"]

    def test_empty_book(self, scenarios):
        analytics = scenarios([])
        out = analytics.stress_scenarios(cutoff=make_cutoff())
        assert out["scenarios"] == []
        assert out["null_reason"] == "no_priced_positions"


# ────────────────────────── benchmark alignment ──────────────────────────


class TestAlignmentGuard:
    """The decision: refuse rather than report a shaky comparison — but never
    refuse bare. A code, an explanation and the counts always travel with it."""

    def _patch(self, monkeypatch, port_days, bench_days, symbol="SPY"):
        from app.mcp.services import returns

        prices = {
            d: ({symbol: 100.0 + i} if d in bench_days else {})
            for i, d in enumerate(sorted(set(port_days) | set(bench_days)))
        }
        records = [
            {"day": d, "mv": 1000.0, "flow": 0.0, "div": 0.0} for d in sorted(port_days)
        ]
        monkeypatch.setattr(returns, "_price_by_day", lambda *a, **kw: prices)
        monkeypatch.setattr(returns, "_fetch_lots", lambda *a, **kw: [])
        monkeypatch.setattr(returns, "_fetch_dividends", lambda *a, **kw: [])
        monkeypatch.setattr(
            returns.twr, "build_daily_records", lambda *a, **kw: records)
        return returns

    def test_refuses_when_overlap_is_too_thin(self, env_token, fake_db, monkeypatch):
        days = [date(2026, 8, d) for d in range(1, 21)]
        returns = self._patch(monkeypatch, port_days=days, bench_days=days[:3])
        out = returns.benchmark_comparison("MAX", today=date(2026, 8, 20))

        assert out["status"] == "insufficient_alignment"
        assert out["portfolio_return_pct"] is None
        assert out["benchmark_return_pct"] is None
        assert out["relative_return_pct"] is None

    def test_the_refusal_carries_a_reason_and_the_counts(
        self, env_token, fake_db, monkeypatch
    ):
        days = [date(2026, 8, d) for d in range(1, 21)]
        returns = self._patch(monkeypatch, port_days=days, bench_days=days[:3])
        out = returns.benchmark_comparison("MAX", today=date(2026, 8, 20))

        assert out["null_reasons"]["benchmark_return_pct"] == "insufficient_alignment"
        a = out["alignment"]
        assert a["aligned_observations"] == 3
        assert a["expected_observations"] == 20
        assert a["missing_observations"] == 17
        assert a["aligned_pct"] == pytest.approx(15.0)
        assert a["required_pct"] == 80.0
        assert "SPY" in out["explanation"]
        assert "3 of the 20" in out["explanation"] or "only 3" in out["explanation"]

    def test_allows_a_well_covered_comparison(self, env_token, fake_db, monkeypatch):
        days = [date(2026, 8, d) for d in range(1, 21)]
        returns = self._patch(monkeypatch, port_days=days, bench_days=days)
        out = returns.benchmark_comparison("MAX", today=date(2026, 8, 20))

        assert out["status"] == "ok"
        assert out["alignment"]["aligned_pct"] == pytest.approx(100.0)

    def test_boundary_at_exactly_the_threshold_passes(
        self, env_token, fake_db, monkeypatch
    ):
        days = [date(2026, 8, d) for d in range(1, 21)]
        returns = self._patch(monkeypatch, port_days=days, bench_days=days[:16])
        out = returns.benchmark_comparison("MAX", today=date(2026, 8, 20))
        assert out["alignment"]["aligned_pct"] == pytest.approx(80.0)
        assert out["status"] == "ok"

    def test_minimum_observations_applies_even_at_full_coverage(
        self, env_token, fake_db, monkeypatch
    ):
        """100% aligned over five days is still five days."""
        days = [date(2026, 8, d) for d in range(1, 6)]
        returns = self._patch(monkeypatch, port_days=days, bench_days=days)
        out = returns.benchmark_comparison("MAX", today=date(2026, 8, 5))
        assert out["alignment"]["aligned_pct"] == pytest.approx(100.0)
        assert out["status"] == "insufficient_alignment"
        assert "below the 10 required" in out["explanation"]

    def test_thresholds_are_overridable(self, env_token, fake_db, monkeypatch):
        days = [date(2026, 8, d) for d in range(1, 6)]
        returns = self._patch(monkeypatch, port_days=days, bench_days=days)
        out = returns.benchmark_comparison(
            "MAX", today=date(2026, 8, 5),
            min_observations=2, min_alignment_pct=50.0,
        )
        assert out["status"] == "ok"

    def test_flags_are_booleans_not_prose(self, env_token, fake_db, monkeypatch):
        """dividends_included and fx_applied were buried in a note string; an
        agent needs to branch on them."""
        days = [date(2026, 8, d) for d in range(1, 21)]
        returns = self._patch(monkeypatch, port_days=days, bench_days=days)
        out = returns.benchmark_comparison("MAX", today=date(2026, 8, 20))
        assert out["dividends_included"] is False
        assert out["fx_applied"] is False
        assert out["benchmark_source"] == "price_snapshots"
