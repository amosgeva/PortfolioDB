"""Money-weighted return (XIRR).

Run from app/ (bare-module imports), like the other engine suites.
"""

from __future__ import annotations

from datetime import date

import pytest

import xirr
from xirr import CashFlow


def cf(y, m, d, amount, kind=""):
    return CashFlow(date(y, m, d), amount, kind)


class TestKnownAnswers:
    def test_doubling_over_one_year_is_100_pct(self):
        r = xirr.compute([
            cf(2026, 1, 1, -100.0),
            cf(2027, 1, 1, 200.0),
        ])
        assert r["status"] == "ok"
        assert r["rate_pct"] == pytest.approx(100.0, abs=0.01)

    def test_flat_over_one_year_is_zero(self):
        r = xirr.compute([
            cf(2026, 1, 1, -100.0),
            cf(2027, 1, 1, 100.0),
        ])
        assert r["rate_pct"] == pytest.approx(0.0, abs=1e-6)

    def test_halving_over_one_year(self):
        r = xirr.compute([
            cf(2026, 1, 1, -100.0),
            cf(2027, 1, 1, 50.0),
        ])
        assert r["rate_pct"] == pytest.approx(-50.0, abs=0.01)

    def test_ten_percent_over_two_years_annualises(self):
        """+21% over two years is 10% a year compounded, not 10.5%."""
        r = xirr.compute([
            cf(2026, 1, 1, -100.0),
            cf(2028, 1, 1, 121.0),
        ])
        assert r["rate_pct"] == pytest.approx(10.0, abs=0.05)

    def test_timing_changes_the_answer(self):
        """The whole reason XIRR exists alongside TWR: the same profit earned
        on money committed later is a higher annualised rate."""
        early = xirr.compute([
            cf(2026, 1, 1, -1000.0),
            cf(2027, 1, 1, 1100.0),
        ])
        late = xirr.compute([
            cf(2026, 7, 1, -1000.0),
            cf(2027, 1, 1, 1100.0),
        ])
        assert late["rate_pct"] > early["rate_pct"]


class TestNpv:
    def test_npv_at_the_solved_rate_is_zero(self):
        flows = [cf(2026, 1, 1, -100.0), cf(2026, 7, 1, -50.0), cf(2027, 1, 1, 170.0)]
        r = xirr.compute(flows)
        assert abs(xirr.npv(r["rate"], sorted(flows, key=lambda f: f.when), date(2026, 1, 1))) < 1e-6

    def test_npv_decreases_as_the_rate_rises(self):
        """Monotonic, which is what makes bisection safe."""
        flows = [cf(2026, 1, 1, -100.0), cf(2027, 1, 1, 150.0)]
        base = date(2026, 1, 1)
        assert xirr.npv(0.0, flows, base) > xirr.npv(0.5, flows, base)


class TestUnsolvable:
    def test_single_flow(self):
        r = xirr.compute([cf(2026, 1, 1, -100.0)])
        assert r["status"] == "unavailable"
        assert r["null_reason"] == "insufficient_flows"
        assert r["rate_pct"] is None

    def test_no_flows(self):
        assert xirr.compute([])["null_reason"] == "insufficient_flows"

    def test_all_outflows_has_no_root(self):
        """Money only ever went in — there is no rate, not a rate of zero."""
        r = xirr.compute([cf(2026, 1, 1, -100.0), cf(2026, 6, 1, -50.0)])
        assert r["null_reason"] == "no_sign_change"

    def test_all_inflows_has_no_root(self):
        r = xirr.compute([cf(2026, 1, 1, 100.0), cf(2026, 6, 1, 50.0)])
        assert r["null_reason"] == "no_sign_change"

    def test_same_day_flows_have_no_duration(self):
        r = xirr.compute([cf(2026, 1, 1, -100.0), cf(2026, 1, 1, 120.0)])
        assert r["null_reason"] == "zero_duration"

    def test_catastrophic_loss_outside_the_bracket(self):
        """A near-total loss over a very short window implies a rate below
        -99.99% annualised; refused rather than clamped to the bracket edge."""
        r = xirr.compute([cf(2026, 1, 1, -1000.0), cf(2026, 1, 2, 0.01)])
        assert r["status"] == "unavailable"
        assert r["null_reason"] == "rate_outside_bracket"


class TestFromLedger:
    def test_builds_flows_with_the_right_signs(self):
        r = xirr.from_ledger(
            lots=[
                {"side": "BUY", "trade_date": date(2026, 1, 1),
                 "quantity": 10, "price": 100, "fees": 0},
                {"side": "SELL", "trade_date": date(2027, 1, 1),
                 "quantity": 10, "price": 120, "fees": 0},
            ],
            income=[],
            closing_value=0.0,
            closing_date=date(2027, 1, 1),
        )
        assert r["rate_pct"] == pytest.approx(20.0, abs=0.01)

    def test_buy_fees_increase_the_outflow(self):
        with_fee = xirr.from_ledger(
            lots=[
                {"side": "BUY", "trade_date": date(2026, 1, 1),
                 "quantity": 10, "price": 100, "fees": 50},
                {"side": "SELL", "trade_date": date(2027, 1, 1),
                 "quantity": 10, "price": 120, "fees": 0},
            ],
            income=[], closing_value=0.0, closing_date=date(2027, 1, 1),
        )
        assert with_fee["rate_pct"] < 20.0

    def test_open_position_contributes_its_closing_value(self):
        """Without the closing value an open position would look like a total
        loss — money out and nothing back."""
        r = xirr.from_ledger(
            lots=[{"side": "BUY", "trade_date": date(2026, 1, 1),
                   "quantity": 10, "price": 100, "fees": 0}],
            income=[],
            closing_value=1100.0,
            closing_date=date(2027, 1, 1),
        )
        assert r["rate_pct"] == pytest.approx(10.0, abs=0.01)

    def test_income_counts_as_a_positive_flow(self):
        without = xirr.from_ledger(
            lots=[{"side": "BUY", "trade_date": date(2026, 1, 1),
                   "quantity": 10, "price": 100, "fees": 0}],
            income=[], closing_value=1000.0, closing_date=date(2027, 1, 1),
        )
        with_div = xirr.from_ledger(
            lots=[{"side": "BUY", "trade_date": date(2026, 1, 1),
                   "quantity": 10, "price": 100, "fees": 0}],
            income=[{"pay_date": date(2026, 7, 1), "amount": 50.0}],
            closing_value=1000.0, closing_date=date(2027, 1, 1),
        )
        assert with_div["rate_pct"] > without["rate_pct"]

    def test_no_closing_value_is_omitted_not_zero(self):
        r = xirr.from_ledger(
            lots=[{"side": "BUY", "trade_date": date(2026, 1, 1),
                   "quantity": 10, "price": 100, "fees": 0}],
            income=[], closing_value=0.0, closing_date=date(2027, 1, 1),
        )
        assert r["flow_count"] == 1
        assert r["null_reason"] == "insufficient_flows"


def test_scope_is_always_labelled():
    """Every payload states that this is invested capital only — there is no
    external-flow ledger, so a portfolio-level MWR is not what this is."""
    solved = xirr.compute([cf(2026, 1, 1, -100.0), cf(2027, 1, 1, 110.0)])
    unsolved = xirr.compute([])
    assert solved["scope"] == "invested_capital_only"
    assert unsolved["scope"] == "invested_capital_only"
