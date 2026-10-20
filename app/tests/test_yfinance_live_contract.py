"""What the app reads from yfinance, checked against the live vendor.

Every other test of the collector, the split check and the income backfill
stubs yfinance, so they pin our handling of a response shape — not the shape
itself. yfinance is a scraper; a release that renames an `info` key or changes
what `.splits` returns turns every snapshot into "No price" with all tests
green.

Network-dependent and rate-limited, so it is opt-in and never runs in CI or
the pre-commit hook:

    PORTFOLIODB_LIVE_VENDOR_TESTS=1 pytest tests/test_yfinance_live_contract.py

Run it after upgrading yfinance (or curl_cffi, which it fetches through).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

if (os.getenv("PORTFOLIODB_LIVE_VENDOR_TESTS") or "").strip().lower() not in ("1", "true", "yes"):
    pytest.skip("live vendor checks are opt-in: set PORTFOLIODB_LIVE_VENDOR_TESTS=1",
                allow_module_level=True)

import yfinance as yf  # noqa: E402

import snapshot_prices  # noqa: E402

MARKET_STATES = {"PRE", "PREPRE", "REGULAR", "POST", "POSTPOST", "CLOSED"}


def test_get_quote_reads_a_price_and_a_trade_time():
    q = snapshot_prices.get_quote("SPY")
    assert q.last_price > 0
    # The staleness guard needs the timestamp. Allow a long weekend plus a
    # holiday; anything older means the key moved.
    assert q.market_time is not None
    assert datetime.now(timezone.utc) - q.market_time < timedelta(days=5)
    assert q.market_state in MARKET_STATES
    for v in (q.bid, q.ask, q.volume):
        assert v is None or isinstance(v, float)


def test_info_carries_the_vendor_split_fields():
    # NVDA's last split (10:1, 2024-06-10) is what the collector records.
    q = snapshot_prices.get_quote("NVDA")
    assert q.split_factor == "10:1"
    assert q.split_date == date(2024, 6, 10)


def test_info_carries_the_enrichment_fields():
    info = yf.Ticker("AAPL").info
    assert info.get("quoteType") == "EQUITY"
    assert info.get("sector")
    assert info.get("country")
    assert info.get("longName") or info.get("shortName")
    assert info.get("exchange")
    assert yf.Ticker("SPY").info.get("quoteType") == "ETF"


def test_splits_is_a_dated_ratio_series():
    splits = yf.Ticker("AAPL").splits
    assert isinstance(splits, pd.Series) and len(splits) > 0
    by_day = {ts.date(): float(r) for ts, r in splits.items()}
    assert by_day.get(date(2020, 8, 31)) == 4.0


def test_dividends_is_a_dated_per_share_series():
    divs = yf.Ticker("SPY").dividends
    assert isinstance(divs, pd.Series) and len(divs) > 0
    assert isinstance(divs.index, pd.DatetimeIndex)
    assert (divs > 0).all()
    assert divs.index.max().date() > date.today() - timedelta(days=200)
