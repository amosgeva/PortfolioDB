"""Set or clear watchlist flag for a symbol.

Usage:
  set PORTFOLIODB_PASSWORD=...
  python set_watchlist.py --symbol APP --on
  python set_watchlist.py --symbol APP --off
"""

from __future__ import annotations

import argparse

from db import load_config, run, transaction


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    args = ap.parse_args()

    sym = args.symbol.strip().upper()
    val = True if args.on else False

    cfg = load_config()
    with transaction(cfg) as conn:
        run(conn, "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING", (sym,))
        run(conn, "UPDATE instruments SET watchlist=%s, updated_at=now() WHERE symbol=%s", (val, sym))

    print("OK", sym, "watchlist=", val)


if __name__ == "__main__":
    main()
