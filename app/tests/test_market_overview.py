"""Market overview: the setting parse, and the change math.

The parse is the part worth testing hardest. It reads a free-text field on a
settings page, so it is the one place in the feature where a human types
something and a dashboard has to survive it.
"""

from __future__ import annotations

import market_overview
import pytest


@pytest.fixture(autouse=True)
def _no_db_settings(monkeypatch):
    """Read the setting from the environment, not the database — these tests are
    about parsing, and settings.get falls back to env when no row exists."""
    monkeypatch.setattr(market_overview.settings, "get",
                        lambda key, env=None, default=None: _no_db_settings.value
                        if _no_db_settings.value is not None else default)
    _no_db_settings.value = None
    yield


def _set(raw):
    _no_db_settings.value = raw


class TestConfigured:
    def test_default_is_the_us_futures_set(self):
        syms = [s for s, _ in market_overview.configured()]
        assert syms == ["ES=F", "NQ=F", "YM=F", "^VIX"]

    def test_labels_come_from_the_setting(self):
        _set("ES=F:S&P Futures")
        assert market_overview.configured() == [("ES=F", "S&P Futures")]

    def test_a_bare_symbol_gets_a_derived_label(self):
        """Someone will type `GC=F` without a label. Showing the raw symbol with
        its Yahoo suffix is worse than deriving something readable."""
        _set("GC=F,^N225")
        assert market_overview.configured() == [("GC=F", "GC"), ("^N225", "N225")]

    def test_whitespace_and_empty_entries_are_tolerated(self):
        _set("  ES=F : S&P Futures ,, , ^VIX:VIX  ")
        assert market_overview.configured() == [("ES=F", "S&P Futures"), ("^VIX", "VIX")]

    def test_symbols_are_uppercased(self):
        _set("btc-usd:Bitcoin")
        assert market_overview.configured() == [("BTC-USD", "Bitcoin")]

    def test_a_repeat_keeps_its_first_position_and_label(self):
        """A duplicate should not render twice, and the first entry wins so the
        order the operator typed is what they see."""
        _set("ES=F:First,^VIX:VIX,ES=F:Second")
        assert market_overview.configured() == [("ES=F", "First"), ("^VIX", "VIX")]

    def test_empty_setting_disables_the_strip(self):
        """An empty list is a legitimate choice — the card hides rather than
        falling back to a default the operator deleted on purpose."""
        _set("")
        assert market_overview.configured() == []
        _set("   ,  , ")
        assert market_overview.configured() == []

    def test_a_label_may_contain_spaces_and_punctuation(self):
        _set("^STOXX50E:Euro Stoxx 50 (EUR)")
        assert market_overview.configured() == [("^STOXX50E", "Euro Stoxx 50 (EUR)")]


class TestFallbackLabel:
    @pytest.mark.parametrize("symbol,label", [
        ("ES=F", "ES"), ("^VIX", "VIX"), ("BTC-USD", "BTC-USD"), ("SPY", "SPY"),
    ])
    def test_suffix_and_caret_are_stripped(self, symbol, label):
        assert market_overview._fallback_label(symbol) == label

    def test_a_symbol_that_is_all_decoration_falls_back_to_itself(self):
        """`removesuffix` + `lstrip` could produce an empty string; an empty label
        would render a nameless card."""
        assert market_overview._fallback_label("=F") == "=F"
        assert market_overview._fallback_label("^") == "^"


class TestClosedMarketSkip:
    """Benchmarks are skipped while their market is shut.

    Without this, a 15-minute collector writes the same Friday close ~96 times a
    day and the strip's "as of" line claims a stale price is current.
    """

    def test_regular_is_collected(self):
        from snapshot_prices import benchmark_market_closed
        assert benchmark_market_closed("REGULAR") is False
        assert benchmark_market_closed("regular") is False

    @pytest.mark.parametrize("state", ["CLOSED", "PRE", "POST", "POSTPOST", "PREPRE"])
    def test_anything_else_is_skipped(self, state):
        """PRE and POST are skipped too: for an index future those are not
        sessions, and for a symbol that does have them the vendor's price is
        still the previous regular close."""
        from snapshot_prices import benchmark_market_closed
        assert benchmark_market_closed(state) is True

    def test_missing_state_is_treated_as_closed(self):
        """Absent metadata means we cannot tell, and writing a possibly-stale
        price under a fresh timestamp is the failure being avoided."""
        from snapshot_prices import benchmark_market_closed
        assert benchmark_market_closed(None) is True
        assert benchmark_market_closed("") is True
