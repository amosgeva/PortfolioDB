"""Guards at the edges where outside data comes in.

* F-6: dividend entitlement is the position at the close BEFORE the ex-date.
* F-15: the collector refuses a zero/negative/NaN last price and stores a
  zero bid/ask as NULL (Yahoo serves 0.0 outside regular hours).
* F-17: a retry never sleeps past the run's deadline.
* E-2: a failed or throttled yfinance lookup never overwrites metadata.
* A-8: the Financial Datasets cache path only takes real symbols.
"""

from __future__ import annotations

import os
import sys
import time
import types
from datetime import date
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import add_income  # noqa: E402
import enrich_instruments  # noqa: E402
import fd_weekly_enrichment  # noqa: E402
import ledger_inputs  # noqa: E402
import snapshot_prices as sp  # noqa: E402

EX = date(2026, 3, 10)


def _lot(i, side, qty, day, account="IBKR"):
    return {"id": i, "symbol": "AAA", "account": account, "side": side, "trade_date": day,
            "quantity": Decimal(str(qty)), "price": Decimal("100"), "fees": Decimal("0")}


class _Ts:
    def __init__(self, d): self._d = d
    def date(self): return self._d


@pytest.fixture
def backfill(monkeypatch):
    """_backfill with the ledger and yfinance replaced; returns the rows written."""
    state = {"lots": [], "written": []}

    def load(conn, *, symbol=None, account=None, as_of=None, units="as_of"):
        rows = [r for r in state["lots"] if as_of is None or r["trade_date"] <= as_of]
        return ledger_inputs.prepare(rows, [])

    monkeypatch.setattr(add_income.ledger_inputs, "load", load)
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(
        Ticker=lambda s: types.SimpleNamespace(dividends={_Ts(EX): 0.5})))

    def fake_run(conn, sql_text, params):
        state["written"].append(params)
        return 1

    monkeypatch.setattr(add_income, "run", fake_run)
    return state


class TestDividendEntitlement:
    def test_a_purchase_on_the_ex_date_is_not_entitled(self, backfill):
        backfill["lots"] = [_lot(1, "BUY", 10, date(2026, 3, 1)), _lot(2, "BUY", 10, EX)]
        add_income._backfill(object(), "AAA", since=None)
        (row,) = backfill["written"]
        assert row[5] == Decimal("5.00000000")       # 10 shares × $0.50, not 20

    def test_a_sale_on_the_ex_date_is_still_entitled(self, backfill):
        backfill["lots"] = [_lot(1, "BUY", 10, date(2026, 3, 1)), _lot(2, "SELL", 10, EX)]
        add_income._backfill(object(), "AAA", since=None)
        (row,) = backfill["written"]
        assert row[5] == Decimal("5.00000000")       # was nothing written at all

    def test_amounts_are_exact_decimals(self, backfill):
        backfill["lots"] = [_lot(1, "BUY", "0.3", date(2026, 3, 1))]
        add_income._backfill(object(), "AAA", since=None)
        (row,) = backfill["written"]
        assert row[5] == Decimal("0.15") and isinstance(row[5], Decimal)

    def test_the_entitlement_date_is_the_day_before(self):
        assert add_income.entitlement_date(EX) == date(2026, 3, 9)


class _FakeTicker:
    info: dict = {}

    def __init__(self, symbol):
        self.symbol = symbol


def _quote_with(monkeypatch, info):
    monkeypatch.setattr(sp.yf, "Ticker", type("T", (_FakeTicker,), {"info": info}))
    return sp.get_quote("X")


class TestCollectorQuotes:
    @pytest.mark.parametrize("price", [0, 0.0, -1.5, float("nan"), float("inf")])
    def test_a_non_price_is_refused(self, monkeypatch, price):
        with pytest.raises(sp.BadQuote, match="not a price"):
            _quote_with(monkeypatch, {"regularMarketPrice": price, "marketState": "REGULAR"})

    def test_a_zero_regular_price_falls_back_to_a_real_current_price(self, monkeypatch):
        q = _quote_with(monkeypatch, {"regularMarketPrice": 0.0, "currentPrice": 217.5})
        assert q.last_price == 217.5

    def test_zero_bid_and_ask_are_not_quotes(self, monkeypatch):
        """Collector-realistic: what Yahoo returns pre-market for SPY."""
        q = _quote_with(monkeypatch, {"regularMarketPrice": 512.3, "bid": 0.0, "ask": 0.0,
                                      "marketState": "PRE", "regularMarketVolume": 0})
        assert (q.bid, q.ask, q.volume) == (None, None, 0.0)

    def test_negative_volume_is_not_recorded(self, monkeypatch):
        assert _quote_with(monkeypatch, {"regularMarketPrice": 5.0, "volume": -3}).volume is None

    def test_a_retry_never_sleeps_past_the_deadline(self, monkeypatch):
        calls = []

        def failing(symbol):
            calls.append(symbol)
            raise RuntimeError("HTTP 429")

        monkeypatch.setattr(sp, "get_quote", failing)
        monkeypatch.setattr(sp.time, "sleep", lambda s: pytest.fail("slept past the deadline"))
        with pytest.raises(RuntimeError, match="429"):
            sp.get_quote_with_retry("X", deadline=time.monotonic() + 1)
        assert calls == ["X"]


class TestEnrichmentNeverClobbers:
    def _ticker(self, monkeypatch, info=None, exc=None):
        class T:
            def __init__(self, s): pass

            @property
            def info(self):
                if exc:
                    raise exc
                return info
        monkeypatch.setattr(enrich_instruments.yf, "Ticker", T)

    @pytest.mark.parametrize("info,exc", [(None, RuntimeError("429 Too Many Requests")), ({}, None),
                                          ({"trailingPegRatio": None}, None)])
    def test_a_failed_lookup_writes_nothing(self, monkeypatch, info, exc):
        self._ticker(monkeypatch, info, exc)
        assert enrich_instruments.classify("NVDA") is None

    def test_missing_fields_are_marked_as_placeholders(self, monkeypatch):
        self._ticker(monkeypatch, {"quoteType": "EQUITY", "longName": "Nvidia", "sector": "Technology"})
        m = enrich_instruments.classify("NVDA")
        assert m["sector"] == "Technology" and m["country"] == "Unknown"
        assert m["placeholders"] == {"country"}

    def test_curated_etf_metadata_survives_a_vendor_failure(self, monkeypatch):
        self._ticker(monkeypatch, exc=RuntimeError("down"))
        m = enrich_instruments.classify("GLD")
        assert m["asset_type"] == "Commodity" and m["placeholders"] == set()

    def test_the_run_skips_failures_and_says_so(self, monkeypatch, caplog):
        self._ticker(monkeypatch, exc=RuntimeError("down"))
        executed = []
        monkeypatch.setattr(enrich_instruments, "execute", lambda c, q, p: executed.append(p))
        args = types.SimpleNamespace(symbol=["NVDA"], held_only=False, missing_only=False)
        assert enrich_instruments._enrich(object(), args) == 1
        assert executed == [] and "existing metadata kept" in caplog.text

    def test_the_update_only_fills_a_placeholder_into_an_empty_column(self):
        sql = " ".join(enrich_instruments.UPDATE_SQL.split())
        assert "CASE WHEN %(sector_placeholder)s THEN COALESCE(sector, %(sector)s)" in sql
        assert "CASE WHEN %(country_placeholder)s THEN COALESCE(country, %(country)s)" in sql


class TestFinancialDatasetsCachePath:
    @pytest.mark.parametrize("bad", ["../../x", "..", "A/B", "<x>", ""])
    def test_a_traversal_symbol_gets_no_path(self, bad):
        with pytest.raises(ValueError):
            fd_weekly_enrichment.cache_path(bad, "facts")

    def test_a_real_symbol_stays_inside_the_cache(self):
        p = fd_weekly_enrichment.cache_path("brk.b", "facts")
        assert p.parent.parent == fd_weekly_enrichment.CACHE_DIR and p.parent.name == "BRK.B"

    def test_main_refuses_bad_symbols_before_any_work(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["x", "--symbols", "NVDA,../../etc", "--dry-run-cost"])
        assert fd_weekly_enrichment.main() == 2
        assert "not a valid symbol" in capsys.readouterr().out
