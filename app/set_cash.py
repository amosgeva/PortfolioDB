"""Set a cash snapshot (manual).

Usage:
  set PORTFOLIODB_PASSWORD=...
  python set_cash.py --cash 1025.50 --account IBKR --note "after trades"

If account omitted (or blank), uses '(merged)'. '(none)' is refused: it is the
label for lots recorded without an account (app/account_names.py).
"""

from __future__ import annotations

import argparse

import account_names
import ledger_numbers
from db import load_config, run, transaction


DEFAULT_ACCOUNT = "(merged)"


def main(argv=None):
    ap = argparse.ArgumentParser()
    # Finite and non-negative, as Decimal — see app/ledger_numbers.py.
    ap.add_argument("--cash", type=lambda s: ledger_numbers.money_arg(s), required=True,
                    help="balance in the account's currency; must be a finite, non-negative number")
    # A cash balance always names an account (cash_snapshots.account is NOT
    # NULL); '(none)' is refused like everywhere else (exit 2).
    ap.add_argument("--account", default=DEFAULT_ACCOUNT, type=account_names.account_arg)
    ap.add_argument("--note", default=None)
    args = ap.parse_args(argv)
    account = args.account or DEFAULT_ACCOUNT

    cfg = load_config()
    with transaction(cfg) as conn:
        run(
            conn,
            "INSERT INTO cash_snapshots(account, cash, note) VALUES (%s,%s,%s)",
            (account, args.cash, args.note),
        )

    print("OK", account, args.cash)


if __name__ == "__main__":
    main()
