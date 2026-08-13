"""Best/worst period statistics.

Built on the TWR growth curve so a contribution never registers as a good week.
The judgement calls worth pinning down: partial periods are excluded from the
records, flat days break streaks, and unobserved months are null rather than
zero.

Run from app/ (bare-module imports).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import period_stats as ps


def curve_from_daily(start: date, returns: list[float]) -> list[tuple[date, float]]:
    """Growth curve from a list of daily returns, one calendar day apart."""
    out = [(start, 1.0)]
    g = 1.0
    for i, r in enumerate(returns, start=1):
        g *= (1.0 + r)
        out.append((start + timedelta(days=i), g))
    return out


class TestDailyReturns:
    def test_recovers_the_input_returns(self):
        curve = curve_from_daily(date(2026, 1, 1), [0.10, -0.05])
        got = ps.daily_returns(curve)
        assert [round(r, 6) for _d, r in got] == [0.10, -0.05]

    def test_first_point_has_no_predecessor(self):
        assert ps.daily_returns([(date(2026, 1, 1), 1.0)]) == []

    def test_empty_curve(self):
        assert ps.daily_returns([]) == []


class TestRecords:
    def _month_spanning(self):
        # Jan (partial, first), Feb, Mar, Apr (partial, last).
        curve = [(date(2026, 1, 20), 1.0)]
        g = 1.0
        for day, r in [
            (date(2026, 1, 21), 0.01),
            (date(2026, 2, 10), 0.05),    # Feb: +5%
            (date(2026, 3, 10), -0.08),   # Mar: -8%
            (date(2026, 4, 2), 0.02),
        ]:
            g *= (1 + r)
            curve.append((day, g))
        return curve

    def test_excludes_partial_first_and_last_periods(self):
        """A half-month is not comparable with a full one."""
        out = ps.build(self._month_spanning())
        m = out["month"]
        assert m["total"] == 4
        assert m["complete"] == 2
        assert m["partial_excluded"] == 2
        assert m["best"]["label"] == "Feb 2026"
        assert m["worst"]["label"] == "Mar 2026"

    def test_best_and_worst_carry_their_dates(self):
        out = ps.build(self._month_spanning())
        best = out["month"]["best"]
        assert best["return_pct"] == pytest.approx(5.0)
        assert best["start"] == "2026-02-10"

    def test_hit_rate_excludes_flat_periods(self):
        curve = curve_from_daily(date(2026, 1, 1), [0.0, 0.05, -0.02, 0.0, 0.03, 0.0])
        d = ps.build(curve)["day"]
        assert d["positive"] == 2
        assert d["negative"] == 1
        assert d["flat"] == 3
        assert d["hit_rate_pct"] == pytest.approx(2 / 3 * 100.0)

    def test_average_up_and_down_reported_separately(self):
        curve = curve_from_daily(date(2026, 1, 1), [0.10, 0.20, -0.30])
        d = ps.build(curve)["day"]
        assert d["best_average_pct"] == pytest.approx(15.0)
        assert d["worst_average_pct"] == pytest.approx(-30.0)

    def test_no_complete_periods_reports_a_reason(self):
        """Two months of data, both partial — records are refused, not faked."""
        curve = [(date(2026, 1, 20), 1.0), (date(2026, 2, 3), 1.05)]
        m = ps.build(curve)["month"]
        assert m["best"] is None
        assert m["null_reason"] == "no_complete_periods"

    def test_days_are_never_partial(self):
        """Every observation is a whole day, so nothing is excluded."""
        curve = curve_from_daily(date(2026, 1, 1), [0.01, 0.02, 0.03])
        d = ps.build(curve)["day"]
        assert d["total"] == d["complete"] == 3
        assert d["partial_excluded"] == 0


class TestWeeklyGrouping:
    def test_groups_by_iso_week(self):
        # 2026-01-05 is a Monday; 2026-01-12 the next Monday.
        curve = [(date(2026, 1, 5), 1.0)]
        g = 1.0
        for day, r in [
            (date(2026, 1, 6), 0.02),
            (date(2026, 1, 7), 0.03),
            (date(2026, 1, 12), 0.01),
            (date(2026, 1, 19), -0.04),
        ]:
            g *= (1 + r)
            curve.append((day, g))
        w = ps.build(curve)["week"]
        assert w["total"] == 3

    def test_week_returns_compound_within_the_week(self):
        """Two +10% days in one ISO week is +21%, not +20%.

        The 21% week is placed in the middle deliberately: the first and last
        groups are partial and excluded from the records.
        """
        curve = [(date(2026, 1, 5), 1.0)]
        g = 1.0
        for day, r in [
            (date(2026, 1, 6), 0.01),    # week 2 — partial (first)
            (date(2026, 1, 13), 0.10),
            (date(2026, 1, 14), 0.10),   # week 3 — 1.1*1.1-1 = 21%
            (date(2026, 1, 20), 0.05),
            (date(2026, 1, 27), 0.02),   # week 5 — partial (last)
        ]:
            g *= (1 + r)
            curve.append((day, g))
        w = ps.build(curve)["week"]
        assert w["best"]["return_pct"] == pytest.approx(21.0)
        assert w["best"]["observations"] == 2


class TestStreaks:
    def test_longest_up_and_down(self):
        curve = curve_from_daily(
            date(2026, 1, 1), [0.01, 0.01, 0.01, -0.01, -0.01, 0.02]
        )
        s = ps.build(curve)["streaks"]
        assert s["longest_up"]["days"] == 3
        assert s["longest_down"]["days"] == 2

    def test_current_streak_is_the_last_run(self):
        curve = curve_from_daily(date(2026, 1, 1), [0.01, -0.01, -0.02])
        s = ps.build(curve)["streaks"]
        assert s["current"]["direction"] == "down"
        assert s["current"]["days"] == 2

    def test_a_flat_day_breaks_a_run(self):
        """A run of gains interrupted by an unchanged day is two runs."""
        curve = curve_from_daily(date(2026, 1, 1), [0.01, 0.01, 0.0, 0.01, 0.01])
        s = ps.build(curve)["streaks"]
        assert s["longest_up"]["days"] == 2

    def test_streak_return_compounds(self):
        curve = curve_from_daily(date(2026, 1, 1), [0.10, 0.10])
        s = ps.build(curve)["streaks"]
        assert s["longest_up"]["return_pct"] == pytest.approx(21.0)

    def test_no_observations(self):
        assert ps._streaks([])["null_reason"] == "no_observations"


class TestMonthlyTable:
    def test_unobserved_months_are_null_not_zero(self):
        """The portfolio did not return 0% in a month it was not priced."""
        curve = [(date(2026, 3, 1), 1.0), (date(2026, 3, 2), 1.05)]
        table = ps.build(curve)["monthly_table"]
        row = table["rows"][0]
        assert row["year"] == 2026
        assert row["months"][0] is None       # January
        assert row["months"][2] is not None   # March
        assert row["months_observed"] == 1

    def test_year_total_compounds_observed_months(self):
        curve = [(date(2026, 1, 15), 1.0)]
        g = 1.0
        for day, r in [
            (date(2026, 2, 15), 0.10),
            (date(2026, 3, 15), 0.10),
        ]:
            g *= (1 + r)
            curve.append((day, g))
        row = ps.build(curve)["monthly_table"]["rows"][0]
        assert row["year_pct"] == pytest.approx(21.0)

    def test_spans_multiple_years(self):
        curve = [(date(2025, 12, 20), 1.0)]
        g = 1.0
        for day, r in [(date(2025, 12, 30), 0.02), (date(2026, 1, 15), 0.03)]:
            g *= (1 + r)
            curve.append((day, g))
        years = [r["year"] for r in ps.build(curve)["monthly_table"]["rows"]]
        assert years == [2025, 2026]

    def test_partial_months_are_labelled(self):
        curve = [(date(2026, 1, 20), 1.0)]
        g = 1.0
        for day, r in [(date(2026, 1, 21), 0.01), (date(2026, 2, 10), 0.05)]:
            g *= (1 + r)
            curve.append((day, g))
        rows = ps.build(curve)["monthly_table"]["rows"]
        assert "Jan" in rows[0]["partial_months"]


class TestBuildEnvelope:
    def test_insufficient_history(self):
        out = ps.build([(date(2026, 1, 1), 1.0)])
        assert out["ok"] is False
        assert out["null_reason"] == "insufficient_history"

    def test_empty_curve(self):
        assert ps.build([])["ok"] is False

    def test_carries_basis_and_coverage(self):
        curve = curve_from_daily(date(2026, 1, 1), [0.01, 0.02])
        out = ps.build(curve)
        assert out["basis"] == "time_weighted_return"
        assert out["coverage"]["days"] == 2
        assert out["coverage"]["start"] == "2026-01-02"

    def test_contribution_does_not_register_as_performance(self):
        """The reason this is built on the TWR curve: a flat curve means flat
        statistics however much money moved in or out underneath it."""
        flat = [(date(2026, 1, 1) + timedelta(days=i), 1.0) for i in range(10)]
        out = ps.build(flat)
        assert out["day"]["positive"] == 0
        assert out["day"]["negative"] == 0
        assert out["day"]["flat"] == 9
