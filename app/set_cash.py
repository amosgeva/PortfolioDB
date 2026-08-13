"""Set a cash snapshot (manual).

Usage:
  set PORTFOLIODB_PASSWORD=...
  python set_cash.py --cash 1025.50 --account IBKR --note "after trades"

If account omitted, uses '(merged)'.
"""

from __future__ import annotations

import argparse

from db import load_config, run, transaction


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cash", type=float, required=True)
    ap.add_argument("--account", default="(merged)")
    ap.add_argument("--note", default=None)
    args = ap.parse_args()

    cfg = load_config()
    with transaction(cfg) as conn:
        run(
            conn,
            "INSERT INTO cash_snapshots(account, cash, note) VALUES (%s,%s,%s)",
            (args.account, args.cash, args.note),
        )

    print("OK", args.account, args.cash)


if __name__ == "__main__":
    main()
