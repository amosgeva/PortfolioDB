"""Set or clear the watchlist flag for one or more symbols.

Usage:
  set PORTFOLIODB_PASSWORD=...
  python set_watchlist.py NVDA AMD              # watch (what `make watchlist ARGS="NVDA AMD"` runs)
  python set_watchlist.py --symbol APP --on
  python set_watchlist.py --symbol APP --off

Every symbol is validated (symbol_paths.normalize_symbol) before anything is
written: a watchlisted symbol is collected and rendered on the dashboard. One
invalid symbol refuses the whole command, so nothing is half-applied.
"""

from __future__ import annotations

import argparse

import ledger_writes
import symbol_paths
from db import load_config, run, transaction


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", type=symbol_paths.symbol_arg,
                    help="symbols to watch (or un-watch with --off)")
    ap.add_argument("--symbol", action="append", default=[], type=symbol_paths.symbol_arg)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true", help="watch (the default)")
    g.add_argument("--off", action="store_true", help="stop watching")
    args = ap.parse_args(argv)

    symbols = list(dict.fromkeys(args.symbols + args.symbol))
    if not symbols:
        ap.error("give at least one symbol")
    val = not args.off

    cfg = load_config()
    with transaction(cfg) as conn:
        for sym in symbols:
            ledger_writes.ensure_instrument(conn, sym)
            run(conn, "UPDATE instruments SET watchlist=%s, updated_at=now() WHERE symbol=%s", (val, sym))

    for sym in symbols:
        print("OK", sym, "watchlist=", val)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
