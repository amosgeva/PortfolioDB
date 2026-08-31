"""The consolidated review endpoint — composition, gating, and reconciliation.

The endpoint adds no arithmetic of its own, so what needs testing is that it
composes faithfully: the same cutoff reaches every section, the totals
reconcile, the detail levels gate the right things, and nothing that matters is
dropped when the payload is trimmed.

The section internals are covered by their own suites (returns, analytics, pnl,
data_quality). Here the underlying services are stubbed so the composition is
visible on its own.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.mcp.services.cutoff import Cutoff

CUTOFF_TS = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)


def make_cutoff() -> Cutoff:
    return Cutoff(
        ts=CUTOFF_TS,
        trade_date=date(2026, 8, 12),
        price_ts_by_symbol={"AAA": CUTOFF_TS},
        cash_ts_by_account={"IBKR": CUTOFF_TS},
        coverage_start=date(2025, 9, 22),
        coverage_end=date(2026, 8, 12),
    )


@pytest.fixture
def review(env_token, fake_db):
    from app.mcp.services import review as module

    return module


@pytest.fixture
def stub(review, monkeypatch):
    """Stub every collaborator, recording the cutoff each one received."""
    seen: dict[str, object] = {}

    def apply(**overrides):
        from app.mcp.services import (
            analytics as analytics_service,
            data_quality as dq_service,
            fees as fees_service,
            income as income_service,
            pnl as pnl_service,
            positions as positions_service,
            returns as returns_service,
        )

        def record(name, value):
            def _fn(*a, **kw):
                seen[name] = kw.get("cutoff")
                return value
            return _fn

        positions = overrides.get("positions", [
            {"symbol": "AAA", "qty": 10.0, "market_value": 800.0,
             "open_cost": 700.0, "weight_pct": 80.0},
            {"symbol": "BBB", "qty": 5.0, "market_value": 200.0,
             "open_cost": 150.0, "weight_pct": 20.0},
        ])
        totals = overrides.get("totals", {
            "market_value": 1000.0, "cost_basis": 850.0,
            "unrealized_pnl": 150.0, "realized_pnl": 50.0,
            "unrealized_pct": 17.6, "total_return_pct": 23.5,
            "active_symbols": 2, "null_reasons": {},
        })

        monkeypatch.setattr(
            positions_service, "current_positions", record("positions", positions))
        monkeypatch.setattr(
            positions_service, "positions_summary", record("totals", totals))
        monkeypatch.setattr(
            income_service, "income_summary",
            lambda **kw: {"total_income": 25.0, "total_tax_withheld": 2.0, "count": 3})
        monkeypatch.setattr(
            fees_service, "fees_summary",
            record("fees", {"total_fees": 12.0, "fee_count": 4,
                            "fee_drag_pct": 1.4, "by_symbol": []}))
        monkeypatch.setattr(
            returns_service, "period_returns",
            record("returns", {
                "basis": "time_weighted_return", "as_of": "2026-08-12",
                "periods": {"1D": 0.5, "WTD": 1.0, "MTD": 2.0,
                            "YTD": 19.69, "1Y": 20.0, "MAX": 20.0},
                "observations": 189,
                "coverage": {"start": "2025-09-22", "end": "2026-08-12", "days": 189},
            }))
        monkeypatch.setattr(
            returns_service, "volatility",
            record("volatility", {"daily_stdev_pct": 1.34, "annualised_pct": 21.3,
                                  "observations": 189}))
        monkeypatch.setattr(
            returns_service, "benchmark_comparison",
            record("benchmark", overrides.get("benchmark", {
                "status": "ok", "benchmark_symbol": "SPY",
                "portfolio_return_pct": 19.69, "benchmark_return_pct": 13.21,
                "relative_return_pct": 6.48, "alignment": {"aligned_pct": 85.8},
            })))
        monkeypatch.setattr(
            returns_service, "_fetch_lots", lambda *a, **kw: [])
        monkeypatch.setattr(
            returns_service, "_fetch_dividends", lambda *a, **kw: [])
        monkeypatch.setattr(
            analytics_service, "drawdown_stats",
            record("drawdown", {"max_drawdown_pct": -24.05,
                                "current_drawdown_pct": -10.53,
                                "holdings_basis": "historical",
                                "peak_ts": None, "trough_ts": None,
                                "recovered": False}))
        monkeypatch.setattr(
            analytics_service, "correlation_matrix",
            record("correlation", {"symbols": ["AAA", "BBB"], "window": "3m",
                                   "observations": 64, "matrix": {},
                                   "pairs": [], "diversifiers": [],
                                   "clusters": {}}))
        monkeypatch.setattr(
            analytics_service, "concentration",
            record("concentration", {"single_largest_pct": 80.0, "top_n": 10,
                                     "top_n_share_pct": 100.0, "hhi": 0.68,
                                     "effective_n": 1.47, "total_positions": 2,
                                     "rows": [], "null_reasons": {}}))
        monkeypatch.setattr(
            analytics_service, "allocation_by",
            record("allocation", {"dimension": "sector", "total_market_value": 1000.0,
                                  "rows": [{"key": "Tech", "market_value": 1000.0,
                                            "weight_pct": 100.0, "symbols": []}]}))
        monkeypatch.setattr(
            analytics_service, "stress_scenarios",
            record("scenarios", {"basis": "analytical_derived", "scenarios": []}))
        monkeypatch.setattr(
            pnl_service, "trade_quality",
            record("trade_quality", {"net_realized_pnl": 50.0,
                                     "gross_realized_pnl": 62.0, "fees": 12.0,
                                     "win_rate_pct": 40.0}))
        monkeypatch.setattr(
            dq_service, "portfolio_data_quality",
            record("data_quality", overrides.get("data_quality", {
                "overall_status": "complete",
                "overall_explanation": "All clear.",
                "collector": {"status": "complete", "message": "ok"},
                "counts": {"symbols_checked": 2, "symbols_complete": 2,
                           "material_issues": 0, "minor_issues": 0},
                "material_issues": [], "minor_issues": [],
            })))
        monkeypatch.setattr(review, "_cash", lambda c, a: (500.0, []))
        monkeypatch.setattr(review, "_first_trade_date", lambda c: date(2024, 12, 3))
        return seen

    return apply


class TestValidation:
    def test_rejects_unknown_detail_level(self, review, stub):
        stub()
        with pytest.raises(ValueError, match="detail_level"):
            review.portfolio_review_snapshot(
                cutoff=make_cutoff(), detail_level="everything")

    def test_rejects_non_usd_reporting_currency(self, review, stub):
        """Refused rather than ignored: there is no FX table, so any other
        value would produce numbers that look converted and are not."""
        stub()
        with pytest.raises(ValueError, match="reporting_currency"):
            review.portfolio_review_snapshot(
                cutoff=make_cutoff(), reporting_currency="EUR")

    def test_accepts_usd_case_insensitively(self, review, stub):
        stub()
        review.portfolio_review_snapshot(
            cutoff=make_cutoff(), reporting_currency="usd")


class TestOneCutoffEverywhere:
    def test_every_section_receives_the_same_cutoff(self, review, stub):
        """The entire point of the endpoint. If any collaborator resolves its
        own instant, the sections describe different moments."""
        seen = stub()
        c = make_cutoff()
        review.portfolio_review_snapshot(cutoff=c, detail_level="full")

        assert seen, "no collaborator recorded a cutoff"
        for name, got in seen.items():
            assert got is c, f"{name} received {got!r}, not the shared cutoff"

    def test_meta_reports_that_cutoff(self, review, stub):
        stub()
        r = review.portfolio_review_snapshot(cutoff=make_cutoff())
        assert r["meta"]["as_of"] == CUTOFF_TS.isoformat()
        assert r["meta"]["reporting_currency"] == "USD"
        assert r["meta"]["cost_basis_method"] == "fifo"

    def test_repeated_calls_are_identical(self, review, stub):
        stub()
        c = make_cutoff()
        # Named locals rather than one inline comparison: the assertion is
        # about repeat calls agreeing, which the inline form hides behind what
        # looks like `x == x`.
        first = review.portfolio_review_snapshot(cutoff=c)
        second = review.portfolio_review_snapshot(cutoff=c)
        assert first == second


class TestReconciliation:
    def test_portfolio_value_equals_invested_plus_cash(self, review, stub):
        stub()
        s = review.portfolio_review_snapshot(cutoff=make_cutoff())["summary"]
        assert s["portfolio_value"] == s["invested_market_value"] + s["cash"]

    def test_cash_weight_is_of_total_not_invested(self, review, stub):
        stub()
        s = review.portfolio_review_snapshot(cutoff=make_cutoff())["summary"]
        assert s["cash_weight_pct"] == pytest.approx(500.0 / 1500.0 * 100.0)

    def test_total_economic_pnl_excludes_fees(self, review, stub):
        """Fees are reported but must NOT be subtracted again — realized and
        unrealized are already net of them."""
        stub()
        s = review.portfolio_review_snapshot(cutoff=make_cutoff())["summary"]
        assert s["fees_total"] == 12.0
        assert s["total_economic_pnl"] == (
            s["realized_pnl"] + s["unrealized_pnl"] + s["income_total"]
        )
        assert s["total_economic_pnl"] != (
            s["realized_pnl"] + s["unrealized_pnl"] + s["income_total"] - s["fees_total"]
        )

    def test_summary_realized_matches_attribution(self, review, stub):
        stub()
        r = review.portfolio_review_snapshot(cutoff=make_cutoff())
        assert r["summary"]["realized_pnl"] == r["attribution"]["realized"]["net_realized_pnl"]

    def test_no_portfolio_value_nulls_the_weight(self, review, stub, monkeypatch):
        stub(totals={
            "market_value": 0.0, "cost_basis": 0.0, "unrealized_pnl": 0.0,
            "realized_pnl": 0.0, "active_symbols": 0, "null_reasons": {},
        })
        monkeypatch.setattr(review, "_cash", lambda c, a: (0.0, []))
        s = review.portfolio_review_snapshot(cutoff=make_cutoff())["summary"]
        assert s["cash_weight_pct"] is None
        assert s["null_reasons"]["cash_weight_pct"] == "no_portfolio_value"


class TestDetailLevels:
    def test_summary_level_omits_the_detail_block(self, review, stub):
        stub()
        r = review.portfolio_review_snapshot(
            cutoff=make_cutoff(), detail_level="summary")
        assert "detail" not in r

    def test_standard_includes_positions_but_not_the_matrix(self, review, stub):
        stub()
        r = review.portfolio_review_snapshot(
            cutoff=make_cutoff(), detail_level="standard")
        assert r["detail"]["positions"]["returned_rows"] == 2
        assert r["detail"]["correlation_matrix"]["omitted"] is True

    def test_full_includes_the_matrix(self, review, stub):
        stub()
        r = review.portfolio_review_snapshot(
            cutoff=make_cutoff(), detail_level="full")
        assert "matrix" in r["detail"]["correlation_matrix"]

    @pytest.mark.parametrize("level", ["summary", "standard", "full"])
    def test_headline_sections_survive_every_level(self, review, stub, level):
        """Trimming the payload must never drop the totals or the warning that
        they cannot be trusted."""
        stub()
        r = review.portfolio_review_snapshot(
            cutoff=make_cutoff(), detail_level=level)
        assert r["summary"]["portfolio_value"] == 1500.0
        assert r["data_quality"]["overall_status"] == "complete"
        for section in ("meta", "summary", "returns", "benchmark",
                        "risk", "concentration", "attribution", "data_quality"):
            assert section in r, section

    def test_truncation_is_declared(self, review, stub, monkeypatch):
        monkeypatch.setattr(review, "MAX_DETAIL_ROWS", 1)
        stub()
        detail = review.portfolio_review_snapshot(cutoff=make_cutoff())["detail"]
        assert detail["positions"]["truncated"] is True
        assert detail["positions"]["total_rows"] == 2
        assert detail["positions"]["returned_rows"] == 1


class TestSectionsPassThrough:
    def test_benchmark_refusal_is_carried_verbatim(self, review, stub):
        """An insufficient-alignment refusal must reach the caller intact, not
        be smoothed into a number."""
        stub(benchmark={
            "status": "insufficient_alignment",
            "benchmark_symbol": "NEW",
            "portfolio_return_pct": None,
            "benchmark_return_pct": None,
            "relative_return_pct": None,
            "explanation": "NEW has prices for 3 of the 64 days...",
            "alignment": {"aligned_observations": 3},
        })
        b = review.portfolio_review_snapshot(cutoff=make_cutoff())["benchmark"]
        assert b["status"] == "insufficient_alignment"
        assert b["benchmark_return_pct"] is None
        assert "NEW" in b["explanation"]

    def test_data_quality_warning_travels_with_the_numbers(self, review, stub):
        stub(data_quality={
            "overall_status": "inconsistent",
            "overall_explanation": "1 material issue; orphan_sell on AAA.",
            "collector": {"status": "complete", "message": "ok"},
            "counts": {"symbols_checked": 2, "symbols_complete": 1,
                       "material_issues": 1, "minor_issues": 0},
            "material_issues": [{"code": "orphan_sell", "symbol": "AAA"}],
            "minor_issues": [],
        })
        r = review.portfolio_review_snapshot(
            cutoff=make_cutoff(), detail_level="summary")
        assert r["data_quality"]["overall_status"] == "inconsistent"
        assert r["data_quality"]["material_issues"][0]["code"] == "orphan_sell"

    def test_coverage_quality_flags_the_untracked_gap(self, review, stub):
        """Trading began 2024-12-03 but price coverage starts 2025-09-22 — no
        return figure can span that gap, and the payload says so."""
        stub()
        cq = review.portfolio_review_snapshot(
            cutoff=make_cutoff())["returns"]["coverage_quality"]
        assert cq["status"] == "partial"
        assert cq["untracked_days_before_coverage"] > 0
        assert "inception-of-coverage" in cq["note"]
