"""Split adjustment — factors, invariants, and the detection heuristic.

Run from app/ (bare-module imports), like the other engine suites.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

import corporate_actions as ca

JER = ZoneInfo("Asia/Jerusalem")

# The real event this module was written for: PRIM 2:1 on 2026-05-06, effective
# at the US open (16:30 Jerusalem), recorded price-only because the ledger
# quantities were already correct.
PRIM = ca.CorporateAction(
    symbol="PRIM",
    kind="SPLIT",
    ex_date=date(2026, 5, 6),
    ratio=Decimal("2"),
    ex_ts=datetime(2026, 5, 6, 16, 30, tzinfo=JER),
    adjust_prices=True,
    adjust_lots=False,
)

# Same event, but with the shares actually credited.
PRIM_WITH_LOTS = ca.CorporateAction(
    symbol="PRIM",
    kind="SPLIT",
    ex_date=date(2026, 5, 6),
    ratio=Decimal("2"),
    ex_ts=datetime(2026, 5, 6, 16, 30, tzinfo=JER),
    adjust_prices=True,
    adjust_lots=True,
)


# ────────────────────────── factors ──────────────────────────


class TestFactors:
    def test_pre_split_lot_gets_the_ratio(self):
        assert ca.lot_factor([PRIM_WITH_LOTS], date(2026, 5, 5)) == Decimal("2")

    def test_lot_on_ex_date_is_already_post_split(self):
        assert ca.lot_factor([PRIM_WITH_LOTS], date(2026, 5, 6)) == Decimal("1")

    def test_adjust_lots_false_leaves_quantity_alone(self):
        assert ca.lot_factor([PRIM], date(2026, 5, 5)) == Decimal("1")

    def test_price_factor_uses_ex_ts_not_midnight(self):
        """A snapshot taken on the ex-date but before the open still carries
        the prior close, so it must be adjusted."""
        pre_open = datetime(2026, 5, 6, 15, 23, tzinfo=JER)
        post_open = datetime(2026, 5, 6, 16, 33, tzinfo=JER)
        assert ca.price_factor([PRIM], pre_open) == Decimal("2")
        assert ca.price_factor([PRIM], post_open) == Decimal("1")

    def test_price_factor_falls_back_to_midnight_without_ex_ts(self):
        action = ca.CorporateAction("X", "SPLIT", date(2026, 5, 6), Decimal("2"))
        assert ca.price_factor([action], datetime(2026, 5, 5, 23, 0, tzinfo=JER)) == 2
        assert ca.price_factor([action], datetime(2026, 5, 6, 0, 30, tzinfo=JER)) == 1

    def test_consecutive_splits_compound(self):
        first = ca.CorporateAction("X", "SPLIT", date(2026, 1, 1), Decimal("2"))
        second = ca.CorporateAction("X", "SPLIT", date(2026, 6, 1), Decimal("3"))
        assert ca.lot_factor([first, second], date(2025, 12, 1)) == Decimal("6")
        assert ca.lot_factor([first, second], date(2026, 3, 1)) == Decimal("3")
        assert ca.lot_factor([first, second], date(2026, 7, 1)) == Decimal("1")

    def test_reverse_split_shrinks_share_count(self):
        reverse = ca.CorporateAction(
            "X", "REVERSE_SPLIT", date(2026, 5, 6), Decimal("0.1")
        )
        assert ca.lot_factor([reverse], date(2026, 5, 5)) == Decimal("0.1")


# ────────────────────────── lot adjustment ──────────────────────────


class TestAdjustLotRows:
    def _row(self, trade_date: date) -> dict:
        return {
            "symbol": "PRIM",
            "side": "BUY",
            "trade_date": trade_date,
            "quantity": Decimal("0.7003"),
            "price": Decimal("133.04"),
        }

    def test_cost_is_invariant(self):
        """Quantity doubles and price halves, so the money is unchanged. This
        is what keeps cost basis and realized P&L untouched by an adjustment."""
        row = self._row(date(2025, 9, 23))
        before = row["quantity"] * row["price"]
        out = ca.adjust_lot_rows([row], [PRIM_WITH_LOTS])[0]
        assert out["quantity"] == Decimal("1.4006")
        assert out["price"] == Decimal("66.52")
        assert out["quantity"] * out["price"] == before

    def test_input_rows_are_not_mutated(self):
        row = self._row(date(2025, 9, 23))
        ca.adjust_lot_rows([row], [PRIM_WITH_LOTS])
        assert row["quantity"] == Decimal("0.7003")

    def test_returns_copies_even_when_nothing_applies(self):
        row = self._row(date(2026, 6, 1))
        out = ca.adjust_lot_rows([row], [PRIM_WITH_LOTS])[0]
        out["quantity"] = Decimal("999")
        assert row["quantity"] == Decimal("0.7003")

    def test_no_actions_is_a_no_op(self):
        row = self._row(date(2025, 9, 23))
        out = ca.adjust_lot_rows([row], [])[0]
        assert out["quantity"] == row["quantity"]
        assert out["price"] == row["price"]

    def test_price_only_action_leaves_lots_untouched(self):
        row = self._row(date(2025, 9, 23))
        out = ca.adjust_lot_rows([row], [PRIM])[0]
        assert out["quantity"] == Decimal("0.7003")

    def test_other_symbols_unaffected(self):
        row = dict(self._row(date(2025, 9, 23)), symbol="NVDA")
        out = ca.adjust_lot_rows([row], [PRIM_WITH_LOTS])[0]
        assert out["quantity"] == Decimal("0.7003")


# ────────────────────────── price adjustment ──────────────────────────


class TestAdjustPrices:
    def test_daily_map_restates_pre_split_days(self):
        prices = {
            date(2026, 5, 5): {"PRIM": 202.92, "NVDA": 100.0},
            date(2026, 5, 6): {"PRIM": 101.365, "NVDA": 101.0},
        }
        out = ca.adjust_price_by_day(prices, [PRIM])
        assert out[date(2026, 5, 5)]["PRIM"] == pytest.approx(101.46)
        assert out[date(2026, 5, 6)]["PRIM"] == pytest.approx(101.365)
        # Untouched symbol passes through on both days.
        assert out[date(2026, 5, 5)]["NVDA"] == 100.0

    def test_series_is_continuous_across_the_split(self):
        """The whole point: a 2:1 split must stop looking like a -50% day."""
        prices = {
            date(2026, 5, 5): {"PRIM": 202.92},
            date(2026, 5, 6): {"PRIM": 101.365},
        }
        raw_move = 101.365 / 202.92 - 1
        assert raw_move == pytest.approx(-0.5, abs=0.01)

        out = ca.adjust_price_by_day(prices, [PRIM])
        adjusted_move = out[date(2026, 5, 6)]["PRIM"] / out[date(2026, 5, 5)]["PRIM"] - 1
        assert adjusted_move == pytest.approx(0.0, abs=0.01)

    def test_price_points_respect_intraday_boundary(self):
        points = [
            (datetime(2026, 5, 6, 15, 23, tzinfo=JER), "PRIM", 202.92),
            (datetime(2026, 5, 6, 16, 33, tzinfo=JER), "PRIM", 123.234),
        ]
        out = ca.adjust_price_points(points, [PRIM])
        assert out[0][2] == pytest.approx(101.46)
        assert out[1][2] == pytest.approx(123.234)


# ────────────────────────── detection ──────────────────────────


class TestDetection:
    def test_finds_the_prim_split(self):
        series = {
            "PRIM": [
                (date(2026, 5, 5), 202.92),
                (date(2026, 5, 6), 101.365),
            ]
        }
        found = ca.detect_suspected_splits(series)
        assert len(found) == 1
        assert found[0]["symbol"] == "PRIM"
        assert found[0]["day"] == date(2026, 5, 6)
        assert found[0]["observed_ratio"] == pytest.approx(2.0, abs=0.01)
        assert found[0]["nearest_ratio"] == 2.0

    def test_ignores_a_large_but_non_split_move(self):
        """RVMD moved +41.35% in a day on real news. The nearest common ratio
        (1.5) is ~6% away, well outside tolerance."""
        series = {
            "RVMD": [
                (date(2026, 4, 10), 96.43),
                (date(2026, 4, 13), 136.30),
            ]
        }
        assert ca.detect_suspected_splits(series) == []

    def test_ignores_ordinary_volatility(self):
        series = {"X": [(date(2026, 1, 1), 100.0), (date(2026, 1, 2), 97.0)]}
        assert ca.detect_suspected_splits(series) == []

    def test_excludes_already_recorded_actions(self):
        series = {
            "PRIM": [
                (date(2026, 5, 5), 202.92),
                (date(2026, 5, 6), 101.365),
            ]
        }
        assert ca.detect_suspected_splits(series, known=[PRIM]) == []

    def test_detects_reverse_split(self):
        series = {"X": [(date(2026, 1, 1), 10.0), (date(2026, 1, 2), 100.0)]}
        found = ca.detect_suspected_splits(series)
        assert len(found) == 1
        assert found[0]["nearest_ratio"] == pytest.approx(0.1)

    def test_ignores_zero_and_missing_prices(self):
        series = {"X": [(date(2026, 1, 1), 0.0), (date(2026, 1, 2), 100.0)]}
        assert ca.detect_suspected_splits(series) == []


class TestInvestigatedNonEvent:
    """`kind='NONE'` records a discontinuity checked and found not to be a
    corporate action. PRIM fell 0.4997 in a day on 2026-05-06 and was recorded
    as a 2:1 split on that evidence; it was a real decline, and the adjustment
    distorted every return spanning the date until corrected.

    Such rows carry ratio 1 — the identity factor — so they cannot adjust
    anything even if the flags are set, and they suppress the heuristic so the
    finding is not re-litigated on every scan.
    """

    NON_EVENT = ca.CorporateAction(
        symbol="PRIM", kind="NONE", ex_date=date(2026, 5, 6), ratio=Decimal("1"),
        adjust_prices=False, adjust_lots=False, reviewed=True,
    )

    def test_identity_ratio_cannot_move_a_price(self):
        prices = {
            date(2026, 5, 5): {"PRIM": 202.92},
            date(2026, 5, 6): {"PRIM": 101.365},
        }
        out = ca.adjust_price_by_day(prices, [self.NON_EVENT])
        assert out[date(2026, 5, 5)]["PRIM"] == 202.92
        assert out[date(2026, 5, 6)]["PRIM"] == 101.365

    def test_identity_ratio_cannot_move_a_lot(self):
        row = {
            "symbol": "PRIM", "side": "BUY", "trade_date": date(2025, 9, 23),
            "quantity": Decimal("0.7003"), "price": Decimal("133.04"),
        }
        out = ca.adjust_lot_rows([row], [self.NON_EVENT])[0]
        assert out["quantity"] == Decimal("0.7003")
        assert out["price"] == Decimal("133.04")

    def test_inert_even_if_the_flags_are_wrongly_set(self):
        """Safe by construction, not by convention."""
        mistaken = ca.CorporateAction(
            symbol="PRIM", kind="NONE", ex_date=date(2026, 5, 6),
            ratio=Decimal("1"), adjust_prices=True, adjust_lots=True,
        )
        assert ca.price_factor([mistaken], date(2026, 5, 5)) == Decimal(1)
        assert ca.lot_factor([mistaken], date(2025, 9, 23)) == Decimal(1)

    def test_suppresses_the_heuristic(self):
        """Without this the scanner re-reports the same investigated event on
        every run, and the finding has nowhere to live."""
        series = {
            "PRIM": [(date(2026, 5, 5), 202.92), (date(2026, 5, 6), 101.365)]
        }
        assert ca.detect_suspected_splits(series) != []          # still detectable
        assert ca.detect_suspected_splits(series, known=[self.NON_EVENT]) == []

    def test_reviewed_defaults_false(self):
        """Auto-recorded rows must be visibly unconfirmed."""
        auto = ca.CorporateAction("X", "SPLIT", date(2026, 1, 1), Decimal("2"))
        assert auto.reviewed is False
