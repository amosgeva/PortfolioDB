"""Populate instrument metadata (asset_type / sector / country) used by the
dashboard's Allocation KPI.

The Allocation KPI reads three columns off `instruments`: `asset_type`,
`sector`, and `country`. Those columns are created with placeholder defaults
('stock' / NULL) and nothing else in the codebase ever fills them, so the KPI
renders "100% Stocks", "100% Unknown region", and a mostly-Unknown sector. This
script fetches the real values from yfinance `Ticker.info` and upserts them.

Equities get sector/country straight from yfinance. Funds/ETPs (where yfinance
exposes no sector/country) fall back to the curated ETF_META map below, keyed
off `fd_store.ETF_SYMBOLS`.

Usage (from app/, with PORTFOLIODB_PASSWORD set or .env present):
  python enrich_instruments.py                 # all instruments
  python enrich_instruments.py --symbol NVDA   # one symbol (repeatable)
  python enrich_instruments.py --held-only     # only symbols with open lots
  python enrich_instruments.py --missing-only  # only rows lacking sector/country
"""

from __future__ import annotations

import argparse
import logging

import yfinance as yf

import fd_store
from db import connect, execute, fetch_all, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Curated metadata for funds/ETPs. yfinance `info` does not reliably expose a
# sector or country for these, so we classify them here. asset_class also lets
# the Asset KPI split commodity/crypto/bond ETPs out of the generic "ETF" bucket.
ETF_META: dict[str, dict[str, str]] = {
    "GLD":  {"asset_class": "Commodity", "sector": "Gold",         "region": "Global"},
    "IAU":  {"asset_class": "Commodity", "sector": "Gold",         "region": "Global"},
    "IBIT": {"asset_class": "Crypto",    "sector": "Crypto",       "region": "Global"},
    "EZBC": {"asset_class": "Crypto",    "sector": "Crypto",       "region": "Global"},
    "TLT":  {"asset_class": "Bond",      "sector": "Treasuries",   "region": "United States"},
    "VOO":  {"asset_class": "ETF",       "sector": "Broad Equity", "region": "United States"},
    "VTI":  {"asset_class": "ETF",       "sector": "Broad Equity", "region": "United States"},
    "QQQ":  {"asset_class": "ETF",       "sector": "Broad Equity", "region": "United States"},
    "SPY":  {"asset_class": "ETF",       "sector": "Broad Equity", "region": "United States"},
    "IWM":  {"asset_class": "ETF",       "sector": "Broad Equity", "region": "United States"},
    "XLE":  {"asset_class": "ETF",       "sector": "Energy",       "region": "United States"},
    "XLK":  {"asset_class": "ETF",       "sector": "Technology",   "region": "United States"},
    "IEFA": {"asset_class": "ETF",       "sector": "Broad Equity", "region": "Developed ex-US"},
    "UFO":  {"asset_class": "ETF",       "sector": "Aerospace",    "region": "Global"},
}


def classify(symbol: str) -> dict[str, str | None]:
    """Return {asset_type, sector, country, name, exchange} for one symbol.

    Equities resolve from yfinance; funds prefer the curated ETF_META overrides
    (with yfinance as a fallback for whatever it does expose).
    """
    info: dict = {}
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:  # yfinance is flaky; degrade gracefully per symbol
        log.warning("yfinance info failed for %s: %s", symbol, exc)

    quote_type = (info.get("quoteType") or "").upper()
    is_etf = quote_type in {"ETF", "MUTUALFUND"} or symbol in fd_store.ETF_SYMBOLS

    meta = ETF_META.get(symbol)
    if is_etf:
        asset_type = (meta or {}).get("asset_class") or "ETF"
        sector = (meta or {}).get("sector") or info.get("sector") or "ETF"
        country = (meta or {}).get("region") or info.get("country") or "Global"
    else:
        asset_type = "stock"
        sector = info.get("sector") or "Other"
        country = info.get("country") or "Unknown"

    return {
        "asset_type": asset_type,
        "sector": sector,
        "country": country,
        "name": info.get("longName") or info.get("shortName"),
        "exchange": info.get("exchange"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", action="append", help="symbol to enrich (repeatable); default = all")
    ap.add_argument("--held-only", action="store_true", help="only symbols with open lots")
    ap.add_argument("--missing-only", action="store_true", help="only rows lacking sector or country")
    args = ap.parse_args()

    cfg = load_config()
    with connect(cfg) as conn:
        if args.symbol:
            symbols = [s.strip().upper() for s in args.symbol]
        elif args.held_only:
            rows = fetch_all(
                conn,
                """
                SELECT symbol FROM lots
                GROUP BY symbol
                HAVING SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) > 0
                ORDER BY symbol
                """,
            )
            symbols = [r["symbol"] for r in rows]
        elif args.missing_only:
            rows = fetch_all(
                conn,
                "SELECT symbol FROM instruments WHERE sector IS NULL OR country IS NULL ORDER BY symbol",
            )
            symbols = [r["symbol"] for r in rows]
        else:
            rows = fetch_all(conn, "SELECT symbol FROM instruments ORDER BY symbol")
            symbols = [r["symbol"] for r in rows]

        if not symbols:
            log.info("No symbols to enrich.")
            return

        log.info("Enriching %d symbol(s)…", len(symbols))
        for sym in symbols:
            m = classify(sym)
            execute(
                conn,
                """
                UPDATE instruments
                SET asset_type = %(asset_type)s,
                    sector     = %(sector)s,
                    country    = %(country)s,
                    name       = COALESCE(%(name)s, name),
                    exchange   = COALESCE(%(exchange)s, exchange),
                    updated_at = now()
                WHERE symbol = %(symbol)s
                """,
                {**m, "symbol": sym},
            )
            log.info(
                "  %-6s asset=%-9s sector=%-22s region=%s",
                sym, m["asset_type"], m["sector"], m["country"],
            )

    log.info("Done.")


if __name__ == "__main__":
    main()
