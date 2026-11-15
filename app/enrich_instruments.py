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

A vendor failure never overwrites what is already there (audit E-2). When
yfinance raises, or answers with the near-empty dict it serves when it is
throttling (no quote type, no name), the symbol is skipped and reported; it
used to be written as stock / "Other" / "Unknown" over good data. Those
placeholders are now only ever written into a column that is still empty.

Exit status: 0 when at least one symbol was enriched (or none needed it), 1
when the vendor failed for every symbol, 2 for an invalid --symbol.

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
import ledger_inputs
import symbol_paths
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


def _usable(info: dict) -> bool:
    """Whether yfinance actually answered. Throttled or failed lookups come
    back as {} or a handful of unrelated keys, with no quote type and no name."""
    return bool(info) and bool(info.get("quoteType") or info.get("longName") or info.get("shortName"))


def classify(symbol: str) -> dict[str, str | None] | None:
    """Return {asset_type, sector, country, name, exchange, placeholders} for
    one symbol, or None when there is nothing trustworthy to write.

    Equities resolve from yfinance; funds prefer the curated ETF_META overrides
    (with yfinance as a fallback for whatever it does expose). ``placeholders``
    names the fields filled with a stand-in ("Other", "Unknown", "ETF",
    "Global") rather than a real value; the caller writes those only into an
    empty column.
    """
    info: dict = {}
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:  # yfinance is flaky; degrade gracefully per symbol
        log.warning("yfinance info failed for %s: %s", symbol, exc)
        info = {}

    meta = ETF_META.get(symbol)
    if not _usable(info) and meta is None:
        return None   # a failed lookup is not evidence about the instrument

    quote_type = (info.get("quoteType") or "").upper()
    is_etf = quote_type in {"ETF", "MUTUALFUND"} or symbol in fd_store.ETF_SYMBOLS or meta is not None

    if is_etf:
        asset_type = (meta or {}).get("asset_class") or "ETF"
        sector = (meta or {}).get("sector") or info.get("sector")
        country = (meta or {}).get("region") or info.get("country")
        stand_in = {"sector": "ETF", "country": "Global"}
    else:
        asset_type = "stock"
        sector = info.get("sector")
        country = info.get("country")
        stand_in = {"sector": "Other", "country": "Unknown"}

    placeholders = {field for field, value in (("sector", sector), ("country", country)) if not value}
    sector = sector or stand_in["sector"]
    country = country or stand_in["country"]

    return {
        "asset_type": asset_type,
        "sector": sector,
        "country": country,
        "name": info.get("longName") or info.get("shortName"),
        "exchange": info.get("exchange"),
        "placeholders": placeholders,
    }


# A placeholder only fills an empty column; a real value always wins.
UPDATE_SQL = """
    UPDATE instruments
    SET asset_type = %(asset_type)s,
        sector     = CASE WHEN %(sector_placeholder)s THEN COALESCE(sector, %(sector)s) ELSE %(sector)s END,
        country    = CASE WHEN %(country_placeholder)s THEN COALESCE(country, %(country)s) ELSE %(country)s END,
        name       = COALESCE(%(name)s, name),
        exchange   = COALESCE(%(exchange)s, exchange),
        updated_at = now()
    WHERE symbol = %(symbol)s
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", action="append", type=symbol_paths.symbol_arg,
                    help="symbol to enrich (repeatable); default = all")
    ap.add_argument("--held-only", action="store_true", help="only symbols with open lots")
    ap.add_argument("--missing-only", action="store_true", help="only rows lacking sector or country")
    args = ap.parse_args(argv)

    cfg = load_config()
    conn = connect(cfg)
    try:
        return _enrich(conn, args)
    finally:
        conn.close()


def _symbols(conn, args) -> list[str]:
    if args.symbol:
        return list(dict.fromkeys(args.symbol))
    if args.held_only:
        # Open by the engines' rules (holdings.open_quantities), not a plain
        # signed sum (audit R-1).
        return sorted(ledger_inputs.open_symbols(conn))
    if args.missing_only:
        sql_text = "SELECT symbol FROM instruments WHERE sector IS NULL OR country IS NULL ORDER BY symbol"
    else:
        sql_text = "SELECT symbol FROM instruments ORDER BY symbol"
    return [r["symbol"] for r in fetch_all(conn, sql_text)]


def _enrich(conn, args) -> int:
    symbols = _symbols(conn, args)
    if not symbols:
        log.info("No symbols to enrich.")
        return 0

    log.info("Enriching %d symbol(s)…", len(symbols))
    failed: list[str] = []
    for sym in symbols:
        m = classify(sym)
        if m is None:
            failed.append(sym)
            log.warning("  %-6s skipped: yfinance returned nothing usable; existing metadata kept", sym)
            continue
        placeholders = m.pop("placeholders")
        execute(
            conn,
            UPDATE_SQL,
            {**m, "symbol": sym,
             "sector_placeholder": "sector" in placeholders,
             "country_placeholder": "country" in placeholders},
        )
        log.info(
            "  %-6s asset=%-9s sector=%-22s region=%s",
            sym, m["asset_type"], m["sector"], m["country"],
        )

    if failed:
        log.warning("Vendor failed for %d of %d symbol(s): %s", len(failed), len(symbols), ", ".join(failed))
    log.info("Done.")
    return 1 if failed and len(failed) == len(symbols) else 0


if __name__ == "__main__":
    raise SystemExit(main())
