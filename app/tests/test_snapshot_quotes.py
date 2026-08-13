"""Quote parsing and the stale-quote guard in snapshot_prices.

The guard exists because of a real incident: on 2026-08-06 Yahoo served the
previous day's closes (~1193 minutes old) for every symbol while still
reporting marketState=REGULAR. Writing those under that day's timestamp would
have put fake prices in the series with nothing to show anything was wrong.

Pure tests — no network, no database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import snapshot_prices as sp

NOW = datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc)


def quote(state: str | None, age_min: float | None, **kw) -> sp.Quote:
    return sp.Quote(
        last_price=kw.get("last_price", 100.0),
        bid=kw.get("bid"),
        ask=kw.get("ask"),
        market_time=None if age_min is None else NOW - timedelta(minutes=age_min),
        market_state=state,
    )


class TestCheckFresh:
    def test_fresh_regular_quote_passes(self):
        sp.check_fresh(quote("REGULAR", 5), NOW)

    def test_exactly_at_the_threshold_passes(self):
        sp.check_fresh(quote("REGULAR", sp.QUOTE_MAX_AGE_MIN), NOW)

    def test_just_past_the_threshold_is_rejected(self):
        with pytest.raises(sp.StaleQuote):
            sp.check_fresh(quote("REGULAR", sp.QUOTE_MAX_AGE_MIN + 1), NOW)

    def test_the_2026_08_06_incident_is_rejected(self):
        """Previous session's close served as a live regular-session quote."""
        with pytest.raises(sp.StaleQuote, match="1193 min old"):
            sp.check_fresh(quote("REGULAR", 1193), NOW)

    def test_message_names_the_trade_time_and_state(self):
        with pytest.raises(sp.StaleQuote) as e:
            sp.check_fresh(quote("REGULAR", 120), NOW)
        assert "marketState=REGULAR" in str(e.value)
        assert "2026-08-06T14:00:00" in str(e.value)

    @pytest.mark.parametrize("state", ["PRE", "POST", "CLOSED"])
    def test_stale_price_outside_regular_session_is_legitimate(self, state):
        """The snapshot window opens at 15:15 Jerusalem, before the 16:30 US
        open. During that pre-market slice the last regular print really is
        hours old, so enforcing freshness there would fire every single day."""
        sp.check_fresh(quote(state, 1193), NOW)

    def test_missing_state_is_not_enforced(self):
        sp.check_fresh(quote(None, 1193), NOW)

    def test_missing_trade_time_is_not_enforced(self):
        """No timestamp means nothing to compare — degrade to accepting rather
        than rejecting every symbol."""
        sp.check_fresh(quote("REGULAR", None), NOW)

    def test_state_comparison_is_case_insensitive(self):
        with pytest.raises(sp.StaleQuote):
            sp.check_fresh(quote("regular", 1193), NOW)


class TestGetQuote:
    """Field extraction from the yfinance `info` payload."""

    def _patch_info(self, monkeypatch, info: dict):
        class FakeTicker:
            def __init__(self, symbol): self.symbol = symbol
            @property
            def info(self): return info

        monkeypatch.setattr(sp.yf, "Ticker", FakeTicker)

    def test_reads_regular_market_price(self, monkeypatch):
        self._patch_info(monkeypatch, {
            "regularMarketPrice": 217.5, "bid": 217.4, "ask": 217.6,
            "regularMarketTime": 1785340800, "marketState": "REGULAR",
        })
        q = sp.get_quote("NVDA")
        assert q.last_price == 217.5
        assert q.bid == 217.4
        assert q.ask == 217.6
        assert q.market_state == "REGULAR"
        assert q.market_time == datetime.fromtimestamp(1785340800, timezone.utc)

    def test_falls_back_to_current_price(self, monkeypatch):
        self._patch_info(monkeypatch, {"currentPrice": 55.25})
        assert sp.get_quote("X").last_price == 55.25

    def test_raises_when_no_price_at_all(self, monkeypatch):
        self._patch_info(monkeypatch, {"marketState": "REGULAR"})
        with pytest.raises(RuntimeError, match="No price"):
            sp.get_quote("X")

    def test_missing_bid_ask_stays_none(self, monkeypatch):
        self._patch_info(monkeypatch, {"regularMarketPrice": 10.0})
        q = sp.get_quote("X")
        assert q.bid is None and q.ask is None

    def test_non_numeric_market_time_disables_the_check(self, monkeypatch):
        """yfinance has shipped this as a datetime in some versions. Anything
        that isn't an epoch number degrades to 'unknown' rather than crashing
        the whole run."""
        self._patch_info(monkeypatch, {
            "regularMarketPrice": 10.0,
            "regularMarketTime": "2026-08-06 16:00:00",
            "marketState": "REGULAR",
        })
        q = sp.get_quote("X")
        assert q.market_time is None
        sp.check_fresh(q, NOW)  # no timestamp → not enforced


class TestVolume:
    """Session volume, recorded so liquidity history accumulates. Nothing reads
    it yet — see the caveat in docs/methodology.md before it ever does."""

    def _patch_info(self, monkeypatch, info: dict):
        class FakeTicker:
            def __init__(self, symbol): self.symbol = symbol
            @property
            def info(self): return info

        monkeypatch.setattr(sp.yf, "Ticker", FakeTicker)

    def test_reads_regular_market_volume(self, monkeypatch):
        self._patch_info(monkeypatch, {
            "regularMarketPrice": 100.0, "regularMarketVolume": 57056361,
        })
        assert sp.get_quote("X").volume == 57056361.0

    def test_falls_back_to_the_volume_alias(self, monkeypatch):
        self._patch_info(monkeypatch, {"regularMarketPrice": 100.0, "volume": 1234})
        assert sp.get_quote("X").volume == 1234.0

    def test_missing_volume_is_null_not_zero(self, monkeypatch):
        """A thin instrument that reports nothing must not be recorded as
        having traded zero shares — those are different facts."""
        self._patch_info(monkeypatch, {"regularMarketPrice": 100.0})
        assert sp.get_quote("X").volume is None

    def test_zero_volume_is_preserved(self, monkeypatch):
        """An explicit zero is a real observation: the instrument genuinely did
        not trade."""
        self._patch_info(monkeypatch, {
            "regularMarketPrice": 100.0, "regularMarketVolume": 0,
        })
        assert sp.get_quote("X").volume == 0.0

    def test_float_volume_is_accepted(self, monkeypatch):
        """yfinance returns floats for some instruments, which is why the
        column is NUMERIC rather than BIGINT."""
        self._patch_info(monkeypatch, {
            "regularMarketPrice": 100.0, "regularMarketVolume": 1234.5,
        })
        assert sp.get_quote("X").volume == 1234.5
