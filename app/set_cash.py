"""Set a cash snapshot (manual).

Usage:
  set PORTFOLIODB_PASSWORD=...
  python set_cash.py --cash 1025.50 --account IBKR --note "after trades"

If account omitted, uses '(merged)'.
"""

from __future__ import annotations

import argparse

import ledger_numbers
from db import load_config, run, transaction


def main():
    ap = argparse.ArgumentParser()
    # Finite and non-negative, as Decimal — see app/ledger_numbers.py.
    ap.add_argument("--cash", type=lambda s: ledger_numbers.money_arg(s), required=True,
                    help="balance in the account's currency; must be a finite, non-negative number")
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
