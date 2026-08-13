"""Data-quality rollup: severity, materiality, and the explanation.

The SQL-backed detectors are exercised against a real database in
app/tests/test_data_quality_sql.py. These tests cover the orchestration —
which issues escalate, which stay minor, and what the overall status becomes —
with the query helpers stubbed out.

The property under test is the one the plan states directly: the overall status
must never read 'complete' while a material holding has stale or missing data.
A health check that reports healthy during an outage is worse than no health
check, because it is trusted.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.mcp.services import data_quality as dq
from app.mcp.services.cutoff import Cutoff

CUTOFF_TS = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
RUN_TS = datetime(2026, 8, 12, 13, 55, tzinfo=timezone.utc)


def make_cutoff(**kw) -> Cutoff:
    defaults = dict(
        ts=CUTOFF_TS,
        trade_date=date(2026, 8, 12),
        price_ts_by_symbol={"BIG": RUN_TS, "TINY": RUN_TS},
        cash_ts_by_account={"IBKR": CUTOFF_TS - timedelta(days=1)},
        coverage_start=date(2025, 9, 22),
        coverage_end=date(2026, 8, 12),
    )
    defaults.update(kw)
    return Cutoff(**defaults)


def position(symbol: str, weight: float, qty: float = 10.0, open_cost: float = 100.0):
    return {
        "symbol": symbol, "qty": qty, "open_cost": open_cost,
        "weight_pct": weight, "market_value": weight * 10,
    }


@pytest.fixture
def stub(monkeypatch):
    """Neutral stubs for every query helper; tests override what they need."""
    def apply(**overrides):
        defaults = {
            "_targeted_symbols": lambda c: {"BIG", "TINY"},
            "_last_successful_run": lambda c: {
                "id": 5006, "ts_start": RUN_TS, "status": "ok",
                "symbols_total": 2, "symbols_ok": 2, "symbols_failed": 0,
                "error": None,
            },
            "_symbols_priced_at": lambda ts: {"BIG", "TINY"},
            "_classification_gaps": lambda syms: {},
            "_orphan_sells": lambda c: {},
            "_suspect_duplicate_lots": lambda c: {},
            "_impossible_values": lambda c: {},
            "_first_trade_dates": lambda c: {"BIG": date(2026, 1, 5), "TINY": date(2026, 1, 5)},
            "_suspected_splits": lambda c: {},
        }
        defaults.update(overrides)
        for name, fn in defaults.items():
            monkeypatch.setattr(dq, name, fn)

        from app.mcp.services import positions as positions_service
        monkeypatch.setattr(
            positions_service, "current_positions",
            lambda *a, **kw: overrides.pop("_positions", None) or [
                position("BIG", 40.0), position("TINY", 0.5),
            ],
        )
    return apply


class TestWorst:
    def test_empty_is_complete(self):
        assert dq.worst([]) == "complete"

    def test_picks_the_most_severe(self):
        assert dq.worst(["complete", "partial", "stale"]) == "stale"

    def test_inconsistent_outranks_unavailable(self):
        """Contradictory data yields a confidently wrong number; missing data
        at least fails visibly."""
        assert dq.worst(["unavailable", "inconsistent"]) == "inconsistent"

    def test_every_status_is_ranked(self):
        for s in dq.STATUS_ORDER:
            assert dq.worst([s]) == s


class TestCleanPortfolio:
    def test_reports_complete_with_no_issues(self, env_token, fake_db, stub):
        stub()
        r = dq.portfolio_data_quality(cutoff=make_cutoff())
        assert r["overall_status"] == "complete"
        assert r["counts"]["material_issues"] == 0
        assert r["counts"]["symbols_complete"] == 2
        assert "Every targeted symbol" in r["overall_explanation"]

    def test_carries_the_provenance_block(self, env_token, fake_db, stub):
        stub()
        r = dq.portfolio_data_quality(cutoff=make_cutoff(), method="avg")
        assert r["meta"]["as_of"] == CUTOFF_TS.isoformat()
        assert r["meta"]["cost_basis_method"] == "avg"
        assert r["meta"]["materiality_pct"] == dq.DEFAULT_MATERIALITY_PCT


class TestStalePrice:
    def test_symbol_missed_by_a_successful_run_is_stale(self, env_token, fake_db, stub):
        """The core rule: the collector ran and succeeded, but this symbol got
        no price in it."""
        stub(_symbols_priced_at=lambda ts: {"TINY"})  # BIG was missed
        r = dq.portfolio_data_quality(cutoff=make_cutoff())

        assert r["overall_status"] == "stale"
        codes = [i["code"] for i in r["material_issues"]]
        assert codes == ["stale_price"]
        assert r["material_issues"][0]["symbol"] == "BIG"

    def test_immaterial_stale_symbol_stays_minor(self, env_token, fake_db, stub):
        stub(_symbols_priced_at=lambda ts: {"BIG"})  # only TINY (0.5%) missed
        r = dq.portfolio_data_quality(cutoff=make_cutoff())

        assert r["counts"]["material_issues"] == 0
        assert [i["code"] for i in r["minor_issues"]] == ["stale_price"]
        assert r["overall_status"] == "complete"

    def test_materiality_threshold_is_configurable(self, env_token, fake_db, stub):
        stub(_symbols_priced_at=lambda ts: {"BIG"})
        r = dq.portfolio_data_quality(
            cutoff=make_cutoff(), materiality_pct=0.1
        )
        assert r["counts"]["material_issues"] == 1
        assert r["overall_status"] == "stale"

    def test_no_successful_run_means_nothing_is_called_stale(
        self, env_token, fake_db, stub
    ):
        """With no run to compare against, per-symbol staleness is unanswerable
        — the collector-level check reports it instead. This is what keeps a
        quiet weekend from flagging every symbol."""
        stub(_last_successful_run=lambda c: None)
        r = dq.portfolio_data_quality(cutoff=make_cutoff())

        assert not any(
            i["code"] == "stale_price" for i in r["material_issues"] + r["minor_issues"]
        )

    def test_prices_without_a_run_is_missing_diagnostics_not_an_outage(
        self, env_token, fake_db, stub
    ):
        """`snapshot_runs` was added after collection had been running for
        months (first run 2026-04-17, first price 2025-09-22), so every
        historical review before that date has prices but no run. That must not
        read as 'the collector never succeeded' — the data is fine, only the
        means to verify liveness is missing."""
        stub(_last_successful_run=lambda c: None)
        r = dq.portfolio_data_quality(cutoff=make_cutoff())   # cutoff has prices

        assert r["collector"]["status"] == "partial"
        codes = [i["code"] for i in r["collector"]["issues"]]
        assert "run_history_unavailable" in codes
        assert "collector_never_succeeded" not in codes
        assert r["overall_status"] == "partial"

    def test_no_run_and_no_prices_is_a_genuine_outage(
        self, env_token, fake_db, stub
    ):
        stub(_last_successful_run=lambda c: None)
        cutoff = make_cutoff(price_ts_by_symbol={})
        r = dq.portfolio_data_quality(cutoff=cutoff)

        assert r["collector"]["status"] == "unavailable"
        assert any(
            i["code"] == "collector_never_succeeded"
            for i in r["collector"]["issues"]
        )


class TestCorrectnessIssues:
    def test_orphan_sell_is_material_even_on_a_tiny_position(
        self, env_token, fake_db, stub
    ):
        """A wrong number is wrong regardless of position size — realized P&L
        is understated whatever the weight."""
        stub(_orphan_sells=lambda c: {
            "TINY": {
                "account": "IBKR", "trade_date": date(2026, 5, 1),
                "lot_id": 42, "sell_quantity": 5.0, "shortfall": -2.0,
            }
        })
        r = dq.portfolio_data_quality(cutoff=make_cutoff())

        assert r["overall_status"] == "inconsistent"
        issue = r["material_issues"][0]
        assert issue["code"] == "orphan_sell"
        assert issue["symbol"] == "TINY"
        assert issue["weight_pct"] < dq.DEFAULT_MATERIALITY_PCT

    def test_missing_cost_basis_on_a_held_symbol(self, env_token, fake_db, stub, monkeypatch):
        from app.mcp.services import positions as positions_service
        stub()
        monkeypatch.setattr(
            positions_service, "current_positions",
            lambda *a, **kw: [position("BIG", 40.0, open_cost=0.0), position("TINY", 0.5)],
        )
        r = dq.portfolio_data_quality(cutoff=make_cutoff())
        assert any(i["code"] == "missing_cost_basis" for i in r["material_issues"])
        assert r["overall_status"] == "inconsistent"

    def test_suspected_split_is_reported_with_its_evidence(
        self, env_token, fake_db, stub
    ):
        stub(_suspected_splits=lambda c: {
            "BIG": {
                "day": "2026-05-06", "observed_ratio": 2.0012,
                "nearest_ratio": 2.0, "prev_price": 202.92, "price": 101.365,
            }
        })
        r = dq.portfolio_data_quality(cutoff=make_cutoff())
        issue = next(i for i in r["material_issues"] if i["code"] == "suspected_split")
        assert issue["severity"] == "inconsistent"
        assert issue["nearest_ratio"] == 2.0
        assert "check_splits" in issue["message"]

    def test_zero_price_buy_is_inconsistent(self, env_token, fake_db, stub):
        stub(_impossible_values=lambda c: {
            "BIG": {"message": "1 BUY lot(s) recorded at a price of zero.",
                    "count": 1, "first_seen": "2026-02-01"}
        })
        r = dq.portfolio_data_quality(cutoff=make_cutoff())
        assert r["overall_status"] == "inconsistent"


class TestMissingPrice:
    def test_symbol_with_no_price_at_all_is_unavailable(
        self, env_token, fake_db, stub
    ):
        stub()
        cutoff = make_cutoff(price_ts_by_symbol={"TINY": RUN_TS})  # BIG absent
        r = dq.portfolio_data_quality(cutoff=cutoff)

        issue = next(i for i in r["material_issues"] if i["code"] == "missing_price")
        assert issue["severity"] == "unavailable"
        assert r["overall_status"] == "unavailable"

    def test_missing_price_takes_precedence_over_stale(
        self, env_token, fake_db, stub
    ):
        """No point reporting 'the run skipped it' for a symbol that has never
        had a price at all."""
        stub(_symbols_priced_at=lambda ts: set())
        cutoff = make_cutoff(price_ts_by_symbol={})
        r = dq.portfolio_data_quality(cutoff=cutoff)

        codes = {i["code"] for i in r["material_issues"] + r["minor_issues"]}
        assert "missing_price" in codes
        assert "stale_price" not in codes


class TestPartialIssues:
    def test_missing_classification_names_the_fields(self, env_token, fake_db, stub):
        stub(_classification_gaps=lambda syms: {"BIG": ["sector", "country"]})
        r = dq.portfolio_data_quality(cutoff=make_cutoff())
        issue = next(
            i for i in r["material_issues"] if i["code"] == "missing_classification"
        )
        assert issue["fields"] == ["sector", "country"]
        assert issue["severity"] == "partial"

    def test_partial_history_when_trading_predates_coverage(
        self, env_token, fake_db, stub
    ):
        stub(_first_trade_dates=lambda c: {"BIG": date(2024, 12, 3), "TINY": date(2026, 1, 5)})
        r = dq.portfolio_data_quality(cutoff=make_cutoff())
        issue = next(i for i in r["material_issues"] if i["code"] == "partial_history")
        assert issue["first_trade"] == "2024-12-03"
        assert issue["coverage_start"] == "2025-09-22"


class TestCollector:
    def test_silent_collector_is_flagged_past_the_weekend_gap(
        self, env_token, fake_db, stub
    ):
        """64.2h is the measured Friday-to-Monday gap, so the alarm sits above
        it at 72h — a quiet weekend must not look like an outage."""
        old_run = CUTOFF_TS - timedelta(hours=80)
        stub(_last_successful_run=lambda c: {
            "id": 1, "ts_start": old_run, "status": "ok",
            "symbols_total": 2, "symbols_ok": 2, "symbols_failed": 0, "error": None,
        })
        r = dq.portfolio_data_quality(cutoff=make_cutoff())
        assert r["collector"]["status"] == "stale"
        assert any(i["code"] == "collector_silent" for i in r["collector"]["issues"])

    def test_a_weekend_gap_alone_is_not_an_outage(self, env_token, fake_db, stub):
        weekend = CUTOFF_TS - timedelta(hours=64.2)
        stub(_last_successful_run=lambda c: {
            "id": 1, "ts_start": weekend, "status": "ok",
            "symbols_total": 2, "symbols_ok": 2, "symbols_failed": 0, "error": None,
        })
        r = dq.portfolio_data_quality(cutoff=make_cutoff())
        assert r["collector"]["status"] == "complete"

    def test_partial_run_is_surfaced(self, env_token, fake_db, stub):
        stub(_last_successful_run=lambda c: {
            "id": 4654, "ts_start": RUN_TS, "status": "partial",
            "symbols_total": 12, "symbols_ok": 11, "symbols_failed": 1,
            "error": "stale: NVDA: last trade 64 min old",
        })
        r = dq.portfolio_data_quality(cutoff=make_cutoff())
        assert any(i["code"] == "last_run_partial" for i in r["collector"]["issues"])
        assert r["overall_status"] == "partial"

    def test_stale_cash_is_flagged(self, env_token, fake_db, stub):
        stub()
        cutoff = make_cutoff(
            cash_ts_by_account={"IBKR": CUTOFF_TS - timedelta(days=30)}
        )
        r = dq.portfolio_data_quality(cutoff=cutoff, cash_max_age_days=14)
        issue = next(i for i in r["collector"]["issues"] if i["code"] == "stale_cash")
        assert issue["accounts"][0]["account"] == "IBKR"
        assert r["overall_status"] == "stale"

    def test_recent_cash_is_not_flagged(self, env_token, fake_db, stub):
        stub()
        r = dq.portfolio_data_quality(cutoff=make_cutoff(), cash_max_age_days=14)
        assert not any(
            i["code"] == "stale_cash" for i in r["collector"]["issues"]
        )


class TestOrdering:
    def test_most_severe_and_largest_first(self, env_token, fake_db, stub):
        stub(
            _symbols_priced_at=lambda ts: set(),
            _orphan_sells=lambda c: {
                "TINY": {"account": None, "trade_date": date(2026, 5, 1),
                         "lot_id": 1, "sell_quantity": 1.0, "shortfall": -1.0},
            },
        )
        r = dq.portfolio_data_quality(cutoff=make_cutoff())
        severities = [dq._RANK[i["severity"]] for i in r["material_issues"]]
        assert severities == sorted(severities, reverse=True)
