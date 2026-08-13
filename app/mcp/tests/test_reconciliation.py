"""Cross-endpoint reconciliation, against the real database.

Plan §9. Everything else in the suite stubs its collaborators, which proves each
service composes correctly but cannot prove the services *agree*. These run the
real code against real data and assert the relationships that must hold no
matter what the data says:

  - portfolio value equals invested plus cash
  - allocation weights sum to invested market value
  - gross realized minus fees equals net realized
  - every endpoint given the same cutoff reports the same totals
  - a fixed as_of reproduces byte-identical payloads

They assert *relationships*, never values, so they stay valid as the ledger
changes. Skips when Postgres is unreachable, following test_dedupe_guards.py.
Read-only throughout — nothing here writes.

Lives in the MCP suite (repo-root working directory) because it imports
app.mcp.*; running it from app/ would shadow the official `mcp` SDK with the
local app/mcp package.
"""

from __future__ import annotations

from datetime import date

import pytest

# Importing deps first puts app/ on sys.path so bare modules resolve.
from app.mcp.deps import get_conn  # noqa: F401

from db import connect, load_config  # noqa: E402

TOLERANCE = 1e-6   # far below a cent; absorbs float summation ordering

# Runs the real services against real data, which takes ~30s. Excluded from
# the pre-commit hook — that exists to catch fast mechanical errors, and it
# must not depend on a database being up. Run deliberately:
#     pytest app/mcp/tests/ -m slow
pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def live_db():
    """Skip the module unless a real database is reachable."""
    try:
        cfg = load_config()
    except Exception as e:
        pytest.skip(f"DB config unavailable: {e}")
    try:
        conn = connect(cfg)
    except Exception as e:
        pytest.skip(f"DB unreachable: {e}")
    conn.close()
    return True


@pytest.fixture(scope="module")
def cutoff(live_db):
    from app.mcp.services import cutoff as cutoff_service

    return cutoff_service.resolve()


@pytest.fixture(scope="module")
def review(cutoff):
    from app.mcp.services import review as review_service

    return review_service.portfolio_review_snapshot(cutoff=cutoff)


def close(a, b, tol=TOLERANCE):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


# ────────────────────────── §9.1 value identity ──────────────────────────


class TestPortfolioValueIdentity:
    @pytest.mark.parametrize("detail_level", ["summary", "standard", "full"])
    def test_value_equals_invested_plus_cash(self, cutoff, detail_level):
        from app.mcp.services import review as review_service

        s = review_service.portfolio_review_snapshot(
            cutoff=cutoff, detail_level=detail_level
        )["summary"]
        assert close(s["portfolio_value"], s["invested_market_value"] + s["cash"])

    @pytest.mark.parametrize("method", ["fifo", "avg"])
    def test_holds_for_both_cost_basis_methods(self, cutoff, method):
        from app.mcp.services import review as review_service

        s = review_service.portfolio_review_snapshot(
            cutoff=cutoff, method=method
        )["summary"]
        assert close(s["portfolio_value"], s["invested_market_value"] + s["cash"])

    def test_matches_the_kpi_endpoint(self, cutoff, review):
        from app.mcp.services import kpis as kpis_service

        k = kpis_service.portfolio_kpis("fifo", cutoff=cutoff)
        assert close(review["summary"]["portfolio_value"], k["aum"])
        assert close(review["summary"]["cash"], k["cash"])
        assert close(review["summary"]["invested_market_value"], k["market_value"])


# ────────────────────────── §9.2 P&L ties to the ledger ──────────────────────────


class TestPnlTiesToLedger:
    def test_realized_matches_a_direct_engine_replay(self, cutoff, review):
        """Recompute realized P&L straight from the lot ledger and compare."""
        from app.mcp.services import pnl as pnl_service

        matches = pnl_service._all_realized_matches(method="fifo")
        matches = [m for m in matches if m["sell_date"] <= cutoff.trade_date]
        direct = sum(m["realized_pnl"] for m in matches)
        assert close(review["summary"]["realized_pnl"], direct)

    def test_unrealized_equals_market_value_minus_cost_basis(self, review):
        s = review["summary"]
        assert close(s["unrealized_pnl"], s["invested_market_value"] - s["cost_basis"])

    def test_summary_realized_matches_attribution(self, review):
        assert close(
            review["summary"]["realized_pnl"],
            review["attribution"]["realized"]["net_realized_pnl"],
        )

    def test_total_economic_pnl_is_the_stated_formula(self, review):
        s = review["summary"]
        assert close(
            s["total_economic_pnl"],
            s["realized_pnl"] + s["unrealized_pnl"] + s["income_total"],
        )


# ────────────────────────── §9.3 fees are not double-counted ──────────────────────────


class TestFeesNotDoubleCounted:
    @pytest.mark.parametrize("method", ["fifo", "avg"])
    def test_gross_minus_fees_equals_net(self, cutoff, method):
        from app.mcp.services import pnl as pnl_service

        q = pnl_service.trade_quality(method, cutoff=cutoff)
        assert close(
            q["gross_realized_pnl"] - q["fees"], q["net_realized_pnl"]
        )

    def test_net_realized_equals_the_realized_pnl_endpoint(self, cutoff):
        """trade_quality's net and realized_pnl's total are the same money,
        computed by different paths."""
        from app.mcp.services import pnl as pnl_service

        q = pnl_service.trade_quality("fifo", cutoff=cutoff)
        r = pnl_service.realized_pnl(
            "fifo", group_by="none", until=cutoff.trade_date
        )
        assert close(q["net_realized_pnl"], r["total_realized"])

    def test_allocated_fees_never_exceed_the_ledger(self, cutoff):
        """Fees on shares still open belong in open cost basis, so the amount
        attributed to closed parcels must be a subset of what was paid."""
        from app.mcp.services import pnl as pnl_service

        q = pnl_service.trade_quality("fifo", cutoff=cutoff)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM(fees), 0) FROM lots WHERE trade_date <= %s",
                    (cutoff.trade_date,),
                )
                ledger_total = float(cur.fetchone()[0])
        assert q["fees"] <= ledger_total + TOLERANCE


# ────────────────────────── §9.4 allocation weights ──────────────────────────


class TestAllocationWeights:
    @pytest.mark.parametrize(
        "dimension", ["sector", "asset_class", "region", "account"]
    )
    def test_market_values_sum_to_invested(self, cutoff, review, dimension):
        from app.mcp.services import analytics as analytics_service

        rows = analytics_service.allocation_by(dimension, cutoff=cutoff)["rows"]
        assert close(
            sum(r["market_value"] for r in rows),
            review["summary"]["invested_market_value"],
        )

    @pytest.mark.parametrize(
        "dimension", ["sector", "asset_class", "region", "account"]
    )
    def test_weights_sum_to_100(self, cutoff, dimension):
        from app.mcp.services import analytics as analytics_service

        rows = analytics_service.allocation_by(dimension, cutoff=cutoff)["rows"]
        if not rows:
            pytest.skip(f"no {dimension} allocation rows")
        assert close(sum(r["weight_pct"] for r in rows), 100.0, tol=1e-6)

    def test_review_carries_the_same_rows(self, cutoff, review):
        from app.mcp.services import analytics as analytics_service

        for dimension, key in (
            ("sector", "by_sector"), ("asset_class", "by_asset_class"),
            ("region", "by_region"), ("account", "by_account"),
        ):
            direct = analytics_service.allocation_by(dimension, cutoff=cutoff)["rows"]
            assert review["concentration"][key] == direct, dimension


# ────────────────────────── §9.5 endpoints agree ──────────────────────────


class TestEndpointsAgreeUnderOneCutoff:
    """The property the whole cutoff design exists to guarantee. Without it,
    two services querying microseconds apart legitimately disagree — and no
    individual response shows anything wrong."""

    def test_invested_market_value(self, cutoff, review):
        from app.mcp.services import positions as positions_service

        assert close(
            review["summary"]["invested_market_value"],
            positions_service.positions_summary("fifo", cutoff=cutoff)["market_value"],
        )

    def test_cost_basis(self, cutoff, review):
        from app.mcp.services import positions as positions_service

        assert close(
            review["summary"]["cost_basis"],
            positions_service.positions_summary("fifo", cutoff=cutoff)["cost_basis"],
        )

    def test_concentration(self, cutoff, review):
        from app.mcp.services import analytics as analytics_service

        c = analytics_service.concentration(10, cutoff=cutoff)
        assert close(review["concentration"]["hhi"], c["hhi"])
        assert close(review["concentration"]["effective_n"], c["effective_n"])
        assert close(
            review["concentration"]["largest_weight_pct"], c["single_largest_pct"]
        )

    def test_drawdown(self, cutoff, review):
        from app.mcp.services import analytics as analytics_service

        d = analytics_service.drawdown_stats(cutoff=cutoff)
        assert close(review["risk"]["max_drawdown_pct"], d["max_drawdown_pct"])
        assert close(
            review["risk"]["current_drawdown_pct"], d["current_drawdown_pct"]
        )

    def test_time_weighted_returns(self, cutoff, review):
        from app.mcp.services import returns as returns_service

        periods = returns_service.period_returns(cutoff=cutoff)["periods"]
        assert review["returns"]["twr"] == periods

    def test_data_quality(self, cutoff, review):
        from app.mcp.services import data_quality as dq_service

        dq = dq_service.portfolio_data_quality(cutoff=cutoff)
        assert review["data_quality"]["overall_status"] == dq["overall_status"]
        assert review["data_quality"]["counts"] == dq["counts"]

    def test_position_count_matches_the_positions_endpoint(self, cutoff, review):
        from app.mcp.services import positions as positions_service

        held = positions_service.current_positions(
            "fifo", held_only=True, cutoff=cutoff
        )
        assert review["summary"]["position_count"] == len(held)


# ────────────────────────── §9.6 determinism ──────────────────────────


class TestDeterminism:
    def test_same_cutoff_reproduces_the_payload(self, cutoff):
        from app.mcp.services import review as review_service

        a = review_service.portfolio_review_snapshot(cutoff=cutoff)
        b = review_service.portfolio_review_snapshot(cutoff=cutoff)
        assert a == b

    def test_a_fixed_historical_as_of_is_stable(self, live_db):
        """Nothing below the cutoff may read the clock."""
        from app.mcp.services import cutoff as cutoff_service
        from app.mcp.services import review as review_service

        as_of = date(2026, 6, 30)
        a = review_service.portfolio_review_snapshot(
            cutoff=cutoff_service.resolve(as_of)
        )
        b = review_service.portfolio_review_snapshot(
            cutoff=cutoff_service.resolve(as_of)
        )
        # meta.app_version and the cutoff instant are identical by construction;
        # any difference here means a service reached for `now`.
        assert a == b

    def test_an_earlier_cutoff_sees_less_history(self, live_db):
        """Sanity check that as_of does something: coverage cannot extend past
        the cutoff."""
        from app.mcp.services import cutoff as cutoff_service

        early = cutoff_service.resolve(date(2026, 3, 31))
        late = cutoff_service.resolve()
        if early.coverage_end and late.coverage_end:
            assert early.coverage_end <= late.coverage_end
