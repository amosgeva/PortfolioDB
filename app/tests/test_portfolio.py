"""Tests for portfolio.compute_fifo_merged / compute_avg_cost_merged.

This is the single entry point used by streamlit_app.py, positions.py and
report_portfolio_db.py (see CLAUDE.md) — its output contract must stay stable:
a DataFrame with columns symbol, qty, open_cost, avg_cost, realized_pnl,
sorted by symbol, FIFO/avg-cost run per (symbol, account) then merged per
symbol.
"""

from datetime import date

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from portfolio import compute_fifo_merged, compute_avg_cost_merged

COLUMNS = ["symbol", "qty", "open_cost", "avg_cost", "realized_pnl"]


def _row(lot_id, symbol, account, side, qty, price, fees=0.0, trade_date=None):
    return {
        "id": lot_id,
        "symbol": symbol,
        "account": account,
        "side": side,
        "trade_date": trade_date or date(2026, 1, 1),
        "quantity": qty,
        "price": price,
        "fees": fees,
    }


class TestEmptyInput:
    def test_fifo_empty(self):
        df = compute_fifo_merged([])
        assert list(df.columns) == COLUMNS
        assert df.empty

    def test_avg_cost_empty(self):
        df = compute_avg_cost_merged([])
        assert list(df.columns) == COLUMNS
        assert df.empty


class TestSingleAccount:
    def test_fifo_open_position(self):
        df = compute_fifo_merged([_row(1, "AAA", "IBKR", "BUY", 10, 50.0)])
        rec = df.iloc[0]
        assert rec["symbol"] == "AAA"
        assert rec["qty"] == 10.0
        assert rec["open_cost"] == 500.0
        assert rec["avg_cost"] == 50.0
        assert rec["realized_pnl"] == 0.0

    def test_fifo_realized(self):
        rows = [
            _row(1, "AAA", "IBKR", "BUY", 10, 50.0),
            _row(2, "AAA", "IBKR", "SELL", 10, 60.0, trade_date=date(2026, 1, 2)),
        ]
        rec = compute_fifo_merged(rows).iloc[0]
        assert rec["qty"] == 0.0
        assert rec["realized_pnl"] == 100.0


class TestCrossAccountMerge:
    ROWS = [
        # Account A: still open
        _row(1, "AAA", "Blink", "BUY", 10, 100.0),
        # Account B: opened and closed at a profit
        _row(2, "AAA", "IBKR", "BUY", 5, 200.0),
        _row(3, "AAA", "IBKR", "SELL", 5, 210.0, trade_date=date(2026, 1, 2)),
    ]

    def test_merged_per_symbol(self):
        df = compute_fifo_merged(self.ROWS)
        assert len(df) == 1  # one row per symbol, accounts merged
        rec = df.iloc[0]
        assert rec["qty"] == 10.0
        assert rec["open_cost"] == 1000.0
        assert rec["avg_cost"] == 100.0
        assert rec["realized_pnl"] == 50.0

    def test_matching_is_scoped_per_account(self):
        # The IBKR SELL must consume the IBKR buy (realized 50), never the
        # cheaper Blink buy (which would realize 550).
        rec = compute_fifo_merged(self.ROWS).iloc[0]
        assert rec["realized_pnl"] == 50.0

    def test_avg_cost_merge_matches_shape(self):
        df = compute_avg_cost_merged(self.ROWS)
        assert len(df) == 1
        rec = df.iloc[0]
        assert rec["qty"] == 10.0
        assert rec["realized_pnl"] == 50.0


class TestMultipleSymbolsSorted:
    def test_sorted_by_symbol(self):
        rows = [
            _row(1, "ZZZ", None, "BUY", 1, 10.0),
            _row(2, "AAA", None, "BUY", 1, 10.0),
        ]
        df = compute_fifo_merged(rows)
        assert list(df["symbol"]) == ["AAA", "ZZZ"]


class TestStringNumericInputs:
    def test_numeric_strings_accepted(self):
        # DB drivers may hand back Decimal/str — to_decimal(str(x)) must cope.
        rows = [_row(1, "AAA", "IBKR", "BUY", "10", "50.5", fees="0.5")]
        rec = compute_fifo_merged(rows).iloc[0]
        assert rec["qty"] == 10.0
        assert rec["open_cost"] == 505.5
