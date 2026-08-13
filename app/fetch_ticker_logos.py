"""Cache ticker logos locally so the dashboard doesn't hot-link a third-party
CDN at runtime.

Downloads parqet logo PNGs for every held + watchlist symbol into
app/dashboard/static/logos/, skipping files fresher than --max-age-days.
The dashboard payload embeds them as data URIs (see dashboard/payload.py);
symbols without a cached file fall back to the CDN in the browser.

Usage (from app/, credentials via .env):
  python fetch_ticker_logos.py [--max-age-days 30] [--symbols NVDA VOO ...]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from db import connect, fetch_all, load_config

LOGO_DIR = Path(__file__).resolve().parent / "dashboard" / "static" / "logos"
LOGO_URL = "https://assets.parqet.com/logos/symbol/{sym}?format=png&size=64"


def active_symbols(conn) -> list[str]:
    rows = fetch_all(
        conn,
        """
        WITH pos AS (
          SELECT symbol,
                 SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) AS qty
          FROM lots GROUP BY symbol
        )
        SELECT i.symbol FROM instruments i
        LEFT JOIN pos p ON p.symbol = i.symbol
        WHERE COALESCE(p.qty, 0) > 0 OR i.watchlist = TRUE
        ORDER BY i.symbol
        """,
    )
    return [r["symbol"] for r in rows]


def fetch_logo(sym: str) -> bytes | None:
    url = LOGO_URL.format(sym=quote(sym))
    if not url.startswith("https://"):  # constant template — belt and braces
        return None
    req = Request(url, headers={"User-Agent": "PortfolioDB-logo-cache/1.0"})
    try:
        with urlopen(req, timeout=15) as resp:  # nosec B310 — https enforced above
            data = resp.read()
            return data if data else None
    except (HTTPError, URLError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-days", type=int, default=30,
                    help="skip files newer than this")
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="override the held+watchlist universe")
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        cfg = load_config()
        with connect(cfg) as conn:
            symbols = active_symbols(conn)

    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    max_age_s = args.max_age_days * 86400
    now = time.time()

    fetched = skipped = failed = 0
    for sym in symbols:
        path = LOGO_DIR / f"{sym}.png"
        if path.exists() and now - path.stat().st_mtime < max_age_s:
            skipped += 1
            continue
        data = fetch_logo(sym)
        if data:
            path.write_bytes(data)
            fetched += 1
        else:
            failed += 1
            print(f"no logo for {sym}")
        time.sleep(0.3)  # be polite to the CDN

    print(f"logos: fetched={fetched} cached={skipped} failed={failed} -> {LOGO_DIR}")


if __name__ == "__main__":
    main()
