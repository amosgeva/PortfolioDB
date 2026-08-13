"""Vendor split recording, and the cross-reference that would have caught PRIM.

The heuristic in corporate_actions.detect_suspected_splits cannot distinguish a
split from a one-day crash — both halve the price overnight. PRIM was recorded
as a 2:1 split on that evidence alone and was actually a real decline, which
distorted every return spanning 2026-05-06 until it was corrected. These cover
the two mechanisms added so that cannot recur: the collector records what the
*vendor* reports, and the scanner asks the vendor before anyone acts.

Run from app/ (bare-module imports).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import check_splits as cs
import snapshot_prices as sp


class TestParseSplitFactor:
    def test_forward_split(self):
        assert sp.parse_split_factor("10:1") == Decimal(10)

    def test_two_for_one(self):
        assert sp.parse_split_factor("2:1") == Decimal(2)

    def test_three_for_two(self):
        assert sp.parse_split_factor("3:2") == Decimal("1.5")

    def test_reverse_split(self):
        assert sp.parse_split_factor("1:10") == Decimal("0.1")

    def test_whitespace_tolerated(self):
        assert sp.parse_split_factor(" 4 : 1 ") == Decimal(4)

    @pytest.mark.parametrize(
        "bad", [None, "", "nonsense", "2", "2:0", "0:1", "a:b", "-2:1"]
    )
    def test_unparseable_returns_none_rather_than_guessing(self, bad):
        """A corporate action recorded from a misread string would adjust real
        numbers, so anything ambiguous is refused."""
        assert sp.parse_split_factor(bad) is None


class TestRecordSplit:
    def _quote(self, factor="2:1", when=date(2026, 5, 6)):
        return sp.Quote(
            last_price=100.0, bid=None, ask=None, market_time=None,
            market_state="REGULAR", split_factor=factor, split_date=when,
        )

    class _Cur:
        def __init__(self, outer): self.outer = outer
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params=None): self.outer.executed.append((sql, params))
        def fetchone(self): return (1,) if self.outer.inserts else None

    class _Conn:
        def __init__(self, inserts=True):
            self.executed = []
            self.inserts = inserts
            self.committed = False
        def cursor(self): return TestRecordSplit._Cur(self)
        def commit(self): self.committed = True

    def test_records_a_forward_split(self):
        conn = self._Conn()
        out = sp.record_split(conn, "NVDA", self._quote("10:1"), date(2024, 1, 1))
        assert out is not None
        params = conn.executed[0][1]
        assert params[0] == "NVDA"
        assert params[1] == "SPLIT"
        assert params[3] == Decimal(10)

    def test_records_a_reverse_split_as_such(self):
        conn = self._Conn()
        sp.record_split(conn, "X", self._quote("1:10"), date(2024, 1, 1))
        assert conn.executed[0][1][1] == "REVERSE_SPLIT"

    def test_adjusts_prices_but_never_lots(self):
        """The vendor knows the quote series was rebased. It cannot know whether
        the broker credited shares, so defaulting adjust_lots TRUE would rewrite
        cost basis on a guess."""
        conn = self._Conn()
        sp.record_split(conn, "X", self._quote(), date(2024, 1, 1))
        sql = conn.executed[0][0]
        assert "TRUE, FALSE, 'yfinance', FALSE" in sql

    def test_notes_say_lots_are_not_adjusted(self):
        conn = self._Conn()
        sp.record_split(conn, "X", self._quote(), date(2024, 1, 1))
        assert "LOTS ARE NOT" in conn.executed[0][1][4]

    def test_skips_a_split_predating_the_ledger(self):
        """A split before both the first trade and the first price cannot affect
        anything stored here."""
        conn = self._Conn()
        out = sp.record_split(
            conn, "AAPL", self._quote("4:1", date(2020, 8, 31)), date(2024, 12, 3)
        )
        assert out is None
        assert conn.executed == []

    def test_skips_when_no_split_reported(self):
        conn = self._Conn()
        assert sp.record_split(conn, "PRIM", self._quote(None), date(2024, 1, 1)) is None

    def test_skips_when_no_date(self):
        conn = self._Conn()
        q = self._quote("2:1", None)
        assert sp.record_split(conn, "X", q, date(2024, 1, 1)) is None

    def test_skips_a_one_for_one_non_event(self):
        conn = self._Conn()
        assert sp.record_split(conn, "X", self._quote("1:1"), date(2024, 1, 1)) is None

    def test_returns_none_when_already_recorded(self):
        """ON CONFLICT DO NOTHING returns no row; the run must not report it as
        newly found."""
        conn = self._Conn(inserts=False)
        assert sp.record_split(conn, "X", self._quote(), date(2024, 1, 1)) is None

    def test_no_floor_still_records(self):
        conn = self._Conn()
        assert sp.record_split(conn, "X", self._quote(), None) is not None


class TestVendorVerdict:
    """The cross-check that would have prevented the PRIM error."""

    def _patch_splits(self, monkeypatch, series, resolves=True):
        class FakeTicker:
            def __init__(self, symbol): self.symbol = symbol
            @property
            def splits(self): return series
            @property
            def info(self):
                return {"regularMarketPrice": 100.0} if resolves else {}

        import yfinance
        monkeypatch.setattr(yfinance, "Ticker", FakeTicker)

    def test_confirms_a_matching_split(self, monkeypatch):
        self._patch_splits(
            monkeypatch,
            __import__("pandas").Series(
                [10.0], index=[datetime(2024, 6, 10, tzinfo=timezone.utc)]
            ),
        )
        v = cs.vendor_verdict("NVDA", date(2024, 6, 10))
        assert v["status"] == "confirmed"
        assert "10:1" in v["detail"]

    def test_tolerates_a_few_days_of_drift(self, monkeypatch):
        """Vendor ex-dates and the day a step becomes visible in our snapshots
        can differ by a session."""
        self._patch_splits(
            monkeypatch,
            __import__("pandas").Series(
                [2.0], index=[datetime(2026, 5, 7, tzinfo=timezone.utc)]
            ),
        )
        assert cs.vendor_verdict("X", date(2026, 5, 6))["status"] == "confirmed"

    def test_contradicts_when_the_symbol_never_split(self, monkeypatch):
        """The PRIM case exactly: a real symbol, a real price step, no split."""
        self._patch_splits(monkeypatch, __import__("pandas").Series(dtype=float))
        v = cs.vendor_verdict("PRIM", date(2026, 5, 6))
        assert v["status"] == "contradicted"
        assert "no splits" in v["detail"]

    def test_contradicts_when_splits_exist_but_not_near_the_date(self, monkeypatch):
        self._patch_splits(
            monkeypatch,
            __import__("pandas").Series(
                [10.0], index=[datetime(2024, 6, 10, tzinfo=timezone.utc)]
            ),
        )
        v = cs.vendor_verdict("NVDA", date(2026, 5, 6))
        assert v["status"] == "contradicted"
        assert "2024-06-10" in v["detail"]

    def test_unresolvable_symbol_is_unknown_not_contradicted(self, monkeypatch):
        """An empty series from a ticker yfinance cannot resolve looks identical
        to 'never split'. Reporting 'not a split' because the lookup failed is
        the same over-confidence this check exists to prevent."""
        self._patch_splits(
            monkeypatch, __import__("pandas").Series(dtype=float), resolves=False
        )
        assert cs.vendor_verdict("ZZZZ", date(2026, 5, 6))["status"] == "unknown"

    def test_lookup_failure_is_unknown(self, monkeypatch):
        class Boom:
            def __init__(self, symbol): raise RuntimeError("network down")

        import yfinance
        monkeypatch.setattr(yfinance, "Ticker", Boom)
        v = cs.vendor_verdict("X", date(2026, 5, 6))
        assert v["status"] == "unknown"
        assert "failed" in v["detail"]
