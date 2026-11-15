"""One processing order, one lot, one net quantity — and every reader agrees.

Audit F-1: a same-day SELL stored before its BUY met an empty FIFO queue, was
dropped, and left the BUY open — while every net-quantity reader counted zero.
Audit R-1: net quantity was implemented eight times and none truncated the way
the engines do. Audit R-4: two Lot dataclasses, three Decimal helpers.

These drive the real engines, the real portfolio merge and the real readers
(holdings, the weekly report, the exec report), and hold them to the same
answer on the cases that used to split them, plus a seeded random ledger.
"""

from __future__ import annotations

import os
import random
import sys
from datetime import date, timedelta
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import avg_cost  # noqa: E402
import exec_report  # noqa: E402
import fifo  # noqa: E402
import holdings  # noqa: E402
import ledger_core  # noqa: E402
import portfolio  # noqa: E402
import report_weekly_db  # noqa: E402

DAY = date(2026, 11, 9)


def row(i, side, qty, price=100, day=DAY, account="A", symbol="TEST", fees=0):
    return {"id": i, "symbol": symbol, "account": account, "side": side, "trade_date": day,
            "quantity": Decimal(str(qty)), "price": Decimal(str(price)), "fees": Decimal(str(fees))}


def lots_of(rows):
    return [ledger_core.Lot(**{k: v for k, v in r.items()}) for r in rows]


def every_reader(rows, symbol="TEST"):
    """{reader: open quantity of `symbol`} across every surface's reader."""
    as_of = max(r["trade_date"] for r in rows)
    out = {
        "fifo": portfolio.compute_fifo_merged(rows).set_index("symbol")["qty"].get(symbol, 0.0),
        "avg_cost": portfolio.compute_avg_cost_merged(rows).set_index("symbol")["qty"].get(symbol, 0.0),
        "holdings": float(holdings.open_quantities(rows).get(symbol, 0)),
        "holdings_on": holdings.holdings_on(rows, as_of).get(symbol, 0.0),
        "weekly_report": float(sum(q for (a, s), q in report_weekly_db.positions_from(rows, as_of).items()
                                   if s == symbol)),
        "exec_report": exec_report._holdings_at_date(rows, as_of).get(symbol, 0.0),
    }
    return out


def assert_all(rows, expected, symbol="TEST"):
    got = every_reader(rows, symbol)
    assert got == {k: pytest.approx(expected) for k in got}, got


class TestSameDaySellStoredFirst:
    """F-1: SELL id 1, BUY id 2, same date (a CSV listing sales first)."""

    ROWS = [row(1, "SELL", 10, 110), row(2, "BUY", 10, 100)]

    def test_fifo_matches_the_buy_and_books_the_gain(self):
        r = fifo.run_fifo(lots_of(self.ROWS))
        assert r.open_qty == 0
        assert r.realized_pnl == Decimal("100")       # (110 − 100) × 10, was 0
        assert [m.buy_lot_id for m in r.matches] == [2]

    def test_avg_cost_agrees(self):
        r = avg_cost.run_avg_cost(lots_of(self.ROWS))
        assert r.open_qty == 0 and r.realized_pnl == Decimal("100")

    def test_no_phantom_position_on_any_surface(self):
        assert_all(self.ROWS, 0.0)

    def test_a_day_round_trip_books_the_same_pnl_in_either_order(self):
        """Buys-first is exact for a day trade: flat, and (sell − buy) × qty."""
        a = fifo.run_fifo(lots_of([row(1, "BUY", 5, 100), row(2, "SELL", 5, 90)]))
        b = fifo.run_fifo(lots_of([row(1, "SELL", 5, 90), row(2, "BUY", 5, 100)]))
        assert (a.open_qty, a.realized_pnl) == (b.open_qty, b.realized_pnl) == (0, Decimal("-50"))

    def test_a_sale_bigger_than_the_day_is_still_truncated(self, caplog):
        """Buys-first never invents a short: selling 15 against 10 bought that
        day still drops 5 with the engines' warning."""
        r = fifo.run_fifo(lots_of([row(1, "SELL", 15, 110), row(2, "BUY", 10, 100)]))
        assert r.open_qty == 0 and r.realized_pnl == Decimal("100")
        assert "SELL exceeds BUYs" in caplog.text

    def test_earlier_days_are_still_processed_first(self):
        rows = [row(5, "SELL", 4, 120, day=DAY), row(9, "BUY", 4, 100, day=DAY - timedelta(days=1))]
        assert fifo.run_fifo(lots_of(rows)).realized_pnl == Decimal("80")


class TestTruncationAgreement:
    """R-1: a plain running sum went negative and let a later buy 'repay' it."""

    def test_oversold_then_bought_again(self):
        rows = [row(1, "BUY", 10), row(2, "SELL", 15, day=DAY + timedelta(days=1)),
                row(3, "BUY", 5, day=DAY + timedelta(days=2))]
        # FIFO matched 10, dropped 5, then 5 more were bought: 5 open. The old
        # SUM(CASE …) readers said 0.
        assert_all(rows, 5.0)

    def test_a_sale_in_the_wrong_account_reduces_nothing(self):
        """F-2's shape: the shares are in A, the sale was entered with no
        account. The engines drop it; a per-symbol sum subtracted it."""
        rows = [row(1, "BUY", 10, account="A"), row(2, "SELL", 10, account=None, day=DAY + timedelta(days=1))]
        assert_all(rows, 10.0)

    def test_empty_and_null_accounts_are_different_streams(self):
        rows = [row(1, "BUY", 10, account=""), row(2, "SELL", 10, account=None, day=DAY + timedelta(days=1))]
        assert_all(rows, 10.0)

    def test_truncations_names_the_dropped_rows(self):
        rows = [row(1, "BUY", 10), row(2, "SELL", 15, day=DAY + timedelta(days=1))]
        (t,) = holdings.truncations(rows)
        assert (t["id"], t["held"], t["dropped"]) == (2, Decimal("10"), Decimal("5"))


class TestExactQuantities:
    """F-13: float residue."""

    def test_no_residue_after_closing_a_fractional_position(self):
        rows = [row(1, "BUY", "0.7"), row(2, "BUY", "0.1"), row(3, "SELL", "0.8")]
        assert holdings.open_quantities(rows) == {}
        assert holdings.holdings_on(rows, DAY) == {}

    def test_float_inputs_are_read_exactly(self):
        rows = [dict(row(1, "BUY", 0), quantity=0.7), dict(row(2, "BUY", 0), quantity=0.1),
                dict(row(3, "SELL", 0), quantity=0.8)]
        assert holdings.open_quantities(rows) == {}

    def test_open_quantities_are_decimal(self):
        q = holdings.open_quantities([row(1, "BUY", "0.33333333")])["TEST"]
        assert q == Decimal("0.33333333") and isinstance(q, Decimal)


class TestOneLotOneHelper:
    """R-4."""

    def test_both_engines_share_one_lot(self):
        assert fifo.Lot is avg_cost.Lot is ledger_core.Lot

    def test_the_lot_answers_both_engines_names(self):
        lot = lots_of([row(1, "BUY", 4, 10, fees=2)])[0]
        assert lot.per_share_cost == lot.net_per_share == Decimal("10.5")
        assert lot.per_share_fee == Decimal("0.5")

    def test_one_decimal_helper(self):
        import positions
        import report_portfolio_db
        assert portfolio.to_decimal is positions.to_decimal is report_portfolio_db._dec is ledger_core.to_decimal
        assert ledger_core.to_decimal(0.1) == Decimal("0.1")


def test_a_seeded_random_ledger_agrees_everywhere():
    """Several hundred random trades over three symbols, two named accounts, the
    unnamed one and the empty one, with same-day pairs in both orders and
    oversells. Every reader must give FIFO's open quantity for every symbol."""
    rng = random.Random(20261115)
    rows = []
    for i in range(1, 601):
        rows.append(row(
            rng.randint(1, 10_000) * 1000 + i,           # ids not in date order
            rng.choice(["BUY", "BUY", "SELL"]),
            Decimal(rng.randint(1, 40)) / Decimal(rng.choice([1, 4, 10])),
            price=rng.randint(50, 150),
            day=DAY + timedelta(days=rng.randint(0, 40)),
            account=rng.choice(["A", "B", None, ""]),
            symbol=rng.choice(["AAA", "BBB", "CCC"]),
        ))
    engine = portfolio.compute_fifo_merged(rows).set_index("symbol")["qty"]
    avg = portfolio.compute_avg_cost_merged(rows).set_index("symbol")["qty"]
    mine = holdings.open_quantities(rows)
    as_of = max(r["trade_date"] for r in rows)
    weekly: dict[str, Decimal] = {}
    for (_acct, sym), q in report_weekly_db.positions_from(rows, as_of).items():
        weekly[sym] = weekly.get(sym, Decimal(0)) + q
    for sym in ("AAA", "BBB", "CCC"):
        assert float(mine.get(sym, 0)) == pytest.approx(engine.get(sym, 0.0)), sym
        assert avg.get(sym, 0.0) == pytest.approx(engine.get(sym, 0.0)), sym
        assert float(weekly.get(sym, 0)) == pytest.approx(engine.get(sym, 0.0)), sym
        assert holdings.holdings_on(rows, as_of).get(sym, 0.0) == pytest.approx(engine.get(sym, 0.0)), sym
