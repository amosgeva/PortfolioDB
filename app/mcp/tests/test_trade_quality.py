"""Trade-quality metrics: win rate, payoff, profit factor, holding buckets.

The engine-level fee split is tested in app/tests/test_fifo_fees.py. These cover
the service on top: how parcels aggregate into trades, which metrics go null and
why, and the no-double-counting invariant.
"""

from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture
def pnl(env_token, fake_db):
    """The service, imported lazily.

    pnl.py imports the bare `fifo` module, which only resolves once
    app.mcp.deps has put app/ on sys.path — so importing at module scope
    breaks when this file runs on its own. Same deferred-import convention
    as test_pnl.py.
    """
    from app.mcp.services import pnl as module

    return module


def match(
    sell_id: int,
    net: float,
    *,
    symbol: str = "X",
    account: str = "A",
    fees: float = 0.0,
    sell_day: date = date(2026, 6, 1),
    holding_days: int | None = 30,
    qty: float = 1.0,
):
    """One parcel. gross is derived so the identity always holds on input."""
    return {
        "symbol": symbol,
        "account": account,
        "sell_lot_id": sell_id,
        "buy_lot_id": 100 + sell_id,
        "buy_date": None,
        "sell_date": sell_day,
        "holding_days": holding_days,
        "qty": qty,
        "buy_cost_ps": 100.0,
        "sell_proceeds_ps": 100.0 + net,
        "buy_cost": 100.0,
        "sell_proceeds": 100.0 + net,
        "realized_pnl": net,
        "gross_realized_pnl": net + fees,
        "fees": fees,
    }


def run(pnl, matches, monkeypatch, **kw):
    """Call trade_quality with the DB-backed helpers stubbed.

    Notional overrides are popped eagerly: inside the lambda they would run
    after kw had already been forwarded to trade_quality, which rejects them.
    """
    notional = {
        "buy_notional": kw.pop("buy_notional", 1000.0),
        "sell_notional": kw.pop("sell_notional", 1000.0),
        "traded_notional": kw.pop("traded_notional", 2000.0),
    }
    monkeypatch.setattr(pnl, "_all_realized_matches", lambda **_kw: matches)
    monkeypatch.setattr(pnl, "_traded_notional", lambda **_kw: notional)
    return pnl.trade_quality(**kw)


class TestValidation:
    def test_rejects_unknown_group_by(self, pnl):
        try:
            pnl.trade_quality(group_by="nonsense")
        except ValueError as e:
            assert "group_by" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_rejects_unknown_method(self, pnl):
        try:
            pnl.trade_quality("lifo")
        except ValueError as e:
            assert "method" in str(e)
        else:
            raise AssertionError("expected ValueError")


class TestTradeAggregation:
    def test_parcels_of_one_sell_collapse_into_one_trade(
        self, pnl, monkeypatch
    ):
        """FIFO can split one SELL across several BUYs. Those parcels were never
        separate decisions, so they must not each count toward the win rate."""
        r = run(pnl, [match(1, +10.0), match(1, -4.0)], monkeypatch)
        assert r["trade_count"] == 1
        assert r["match_count"] == 2
        # Net +6 across the two parcels — one winning trade, not one of each.
        assert r["winning_trades"] == 1
        assert r["losing_trades"] == 0

    def test_separate_sells_are_separate_trades(self, pnl, monkeypatch):
        r = run(pnl, [match(1, +10.0), match(2, -4.0)], monkeypatch)
        assert r["trade_count"] == 2
        assert (r["winning_trades"], r["losing_trades"]) == (1, 1)


class TestMetrics:
    def test_win_rate_excludes_breakeven_from_the_denominator(
        self, pnl, monkeypatch
    ):
        r = run(pnl, [match(1, +10.0), match(2, -5.0), match(3, 0.0)], monkeypatch)
        assert r["breakeven_trades"] == 1
        assert r["win_rate_pct"] == 50.0  # 1 of 2 decided, not 1 of 3

    def test_average_loss_is_signed(self, pnl, monkeypatch):
        """Negative, matching the sign convention of realized_pnl everywhere
        else — the payoff ratio takes the magnitude itself."""
        r = run(pnl, [match(1, +20.0), match(2, -10.0)], monkeypatch)
        assert r["average_gain"] == 20.0
        assert r["average_loss"] == -10.0
        assert r["payoff_ratio"] == 2.0

    def test_profit_factor(self, pnl, monkeypatch):
        r = run(pnl, [match(1, +30.0), match(2, +10.0), match(3, -20.0)], monkeypatch)
        assert r["profit_factor"] == 2.0   # 40 / 20

    def test_fee_ratios(self, pnl, monkeypatch):
        r = run(pnl, [match(1, 90.0, fees=10.0)], monkeypatch)
        assert r["gross_realized_pnl"] == 100.0
        assert r["fees"] == 10.0
        assert r["net_realized_pnl"] == 90.0
        assert r["fee_to_gross_profit_pct"] == 10.0
        assert r["fee_to_traded_notional_pct"] == 0.5   # 10 / 2000


class TestNoDoubleCounting:
    def test_gross_minus_fees_equals_net(self, pnl, monkeypatch):
        """The invariant the whole fee decomposition rests on."""
        r = run(pnl, [match(1, 50.0, fees=5.0), match(2, -20.0, fees=3.0)], monkeypatch)
        assert r["gross_realized_pnl"] - r["fees"] == r["net_realized_pnl"]

    def test_net_matches_the_realized_pnl_endpoint_convention(
        self, pnl, monkeypatch
    ):
        """net_realized_pnl must equal the sum of realized_pnl — the same number
        realized_pnl() reports — so the two endpoints cannot disagree."""
        matches = [match(1, 50.0, fees=5.0), match(2, -20.0, fees=3.0)]
        r = run(pnl, matches, monkeypatch)
        assert r["net_realized_pnl"] == sum(m["realized_pnl"] for m in matches)


class TestNulls:
    def test_no_trades_at_all(self, pnl, monkeypatch):
        r = run(pnl, [], monkeypatch)
        assert r["trade_count"] == 0
        for field in ("win_rate_pct", "average_gain", "average_loss",
                      "payoff_ratio", "profit_factor", "fee_to_gross_profit_pct"):
            assert r[field] is None, field
            assert field in r["null_reasons"]

    def test_all_winners_leaves_payoff_undefined(
        self, pnl, monkeypatch
    ):
        r = run(pnl, [match(1, +10.0), match(2, +5.0)], monkeypatch)
        assert r["win_rate_pct"] == 100.0
        assert r["average_loss"] is None
        assert r["payoff_ratio"] is None
        assert r["profit_factor"] is None
        assert r["null_reasons"]["profit_factor"] == "no_losing_trades"

    def test_all_losers(self, pnl, monkeypatch):
        r = run(pnl, [match(1, -10.0), match(2, -5.0)], monkeypatch)
        assert r["win_rate_pct"] == 0.0
        assert r["average_gain"] is None
        assert r["payoff_ratio"] is None
        assert r["null_reasons"]["payoff_ratio"] == "no_winning_trades"

    def test_fee_to_gross_is_null_against_a_loss(
        self, pnl, monkeypatch
    ):
        """Divided by a gross loss the ratio inverts sign and reads as
        nonsense, so it is refused rather than reported."""
        r = run(pnl, [match(1, -50.0, fees=5.0)], monkeypatch)
        assert r["gross_realized_pnl"] < 0
        assert r["fee_to_gross_profit_pct"] is None
        assert r["null_reasons"]["fee_to_gross_profit_pct"] == "no_gross_profit"

    def test_zero_notional(self, pnl, monkeypatch):
        r = run(pnl, [match(1, +10.0)], monkeypatch, traded_notional=0.0)
        assert r["fee_to_traded_notional_pct"] is None


class TestHoldingBuckets:
    def test_boundaries_are_inclusive_lower_exclusive_upper(self, pnl):
        assert pnl._holding_bucket(0) == "<1w"
        assert pnl._holding_bucket(6) == "<1w"
        assert pnl._holding_bucket(7) == "1w-1m"
        assert pnl._holding_bucket(29) == "1w-1m"
        assert pnl._holding_bucket(30) == "1m-3m"
        assert pnl._holding_bucket(89) == "1m-3m"
        assert pnl._holding_bucket(90) == "3m-1y"
        assert pnl._holding_bucket(364) == "3m-1y"
        assert pnl._holding_bucket(365) == ">1y"
        assert pnl._holding_bucket(5000) == ">1y"

    def test_unknown_holding_period_is_none(self, pnl):
        assert pnl._holding_bucket(None) is None

    def test_parcels_land_in_the_right_bucket(self, pnl, monkeypatch):
        r = run(pnl, [
                match(1, +10.0, holding_days=3),
                match(2, -5.0, holding_days=45),
                match(3, +20.0, holding_days=400),
            ],
            monkeypatch,
        )
        rows = {row["bucket"]: row for row in r["holding_periods"]["rows"]}
        assert rows["<1w"]["trades"] == 1
        assert rows["1m-3m"]["trades"] == 1
        assert rows[">1y"]["trades"] == 1
        assert rows["1w-1m"]["trades"] == 0

    def test_every_bucket_is_present_even_when_empty(
        self, pnl, monkeypatch
    ):
        """A stable row set means a consumer can rely on the shape."""
        r = run(pnl, [match(1, +10.0, holding_days=3)], monkeypatch)
        labels = [row["bucket"] for row in r["holding_periods"]["rows"]]
        assert labels == [b[0] for b in pnl.HOLDING_BUCKETS]

    def test_unavailable_when_no_parcel_has_a_buy_date(
        self, pnl, monkeypatch
    ):
        """Avg-cost pools purchases, so there is no per-parcel buy date."""
        r = run(pnl, [match(1, +10.0, holding_days=None)], monkeypatch)
        hp = r["holding_periods"]
        assert hp["available"] is False
        assert hp["null_reason"] == "avg_cost_pools_purchases_so_no_buy_date"
        assert hp["rows"] == []


class TestGrouping:
    def test_group_by_symbol(self, pnl, monkeypatch):
        r = run(pnl, [
                match(1, +30.0, symbol="AAA"),
                match(2, -10.0, symbol="BBB"),
            ],
            monkeypatch,
            group_by="symbol",
        )
        rows = {row["bucket"]: row for row in r["rows"]}
        assert rows["AAA"]["net_realized_pnl"] == 30.0
        assert rows["BBB"]["net_realized_pnl"] == -10.0

    def test_rows_are_sorted_best_first(self, pnl, monkeypatch):
        r = run(pnl, [
                match(1, -10.0, symbol="AAA"),
                match(2, +30.0, symbol="BBB"),
            ],
            monkeypatch,
            group_by="symbol",
        )
        assert [row["bucket"] for row in r["rows"]] == ["BBB", "AAA"]

    def test_group_by_month_uses_the_sell_date(self, pnl, monkeypatch):
        r = run(pnl, [
                match(1, +5.0, sell_day=date(2026, 3, 15)),
                match(2, +5.0, sell_day=date(2026, 4, 2)),
            ],
            monkeypatch,
            group_by="month",
        )
        assert {row["bucket"] for row in r["rows"]} == {"2026-03", "2026-04"}

    def test_group_by_holding_bucket(self, pnl, monkeypatch):
        r = run(pnl, [
                match(1, +5.0, holding_days=2),
                match(2, +7.0, holding_days=200),
            ],
            monkeypatch,
            group_by="holding_bucket",
        )
        assert {row["bucket"] for row in r["rows"]} == {"<1w", "3m-1y"}

    def test_notional_fields_are_dropped_from_groups(
        self, pnl, monkeypatch
    ):
        """Notional is portfolio-level and is not apportioned across groups, so
        it is omitted rather than reported as a misleading zero."""
        r = run(pnl, [match(1, +5.0)], monkeypatch, group_by="symbol")
        row = r["rows"][0]
        for field in ("traded_notional", "fee_to_traded_notional_pct",
                      "holding_periods"):
            assert field not in row
