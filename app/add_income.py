"""Add an income event (dividend / interest / cap-gain distribution) into
PortfolioDB, or backfill dividends from yfinance.

Income lives in its own append-only `income` table — it never touches `lots`
or cost basis (see app/mcp/services/income.py).

Manual usage:
  set PORTFOLIODB_PASSWORD=...
  python add_income.py --symbol NVDA --account IBKR --pay-date 2026-03-15 \
      --kind DIVIDEND --amount 4.00 [--ex-date 2026-03-01] \
      [--tax-withheld 0.60] [--per-share 0.04] [--notes "Q1"]

Backfill usage (cash = per-share × shares held on the ex-date, reconstructed
from your lots; merged across accounts, account left NULL):
  python add_income.py --backfill --symbol NVDA [--since 2024-01-01]

Both insert with ON CONFLICT DO NOTHING (dedupe on symbol, account, kind,
pay_date, amount), so re-running is safe.

Correcting earlier estimates:
  python add_income.py --backfill --symbol NVDA --replace-estimates [--since ...]

--replace-estimates deletes the symbol's earlier backfilled rows and rebuilds
them, and the two halves share one scope: with --since, only estimates dated
on or after it are deleted, and only those dates are rebuilt; without it, all
of them. Nothing is deleted unless there is something to rebuild, and the
command is one transaction, so a failed insert restores the deleted rows.
Manual rows are never touched.
"""

from __future__ import annotations

import argparse
from datetime import datetime

import holdings
import ledger_inputs
import ledger_numbers
from db import load_config, run, transaction

KINDS = ["DIVIDEND", "INTEREST", "CAP_GAIN_DIST"]

# What a backfilled row is, spelled out in the row itself: the pay date is the
# ex-date because yfinance publishes no payment dates, and the amount is the
# per-share figure times the shares the ledger says were held — an estimate of
# cash received, not a statement from the broker.
BACKFILL_NOTE = "auto-backfill (estimate: pay_date = ex_date; entitlement from ledger, split-adjusted)"


def _shares_held_on(conn, symbol: str, as_of, *, per_account: bool = False) -> dict[str | None, float]:
    """Shares of ``symbol`` held on ``as_of``, in TODAY's split units.

    The lots are selected by the ex-date; their quantity is stated in current
    units, because that is the unit yfinance states every historical
    per-share dividend in (Apple's $0.82 of August 2020 comes back as $0.205
    after the later 4:1). Ten pre-split shares times a rebased per-share
    figure is half the cash; twenty restated shares times it is the cash. The
    1.7.0 version read the ledger as of the ex-date, which dropped the later
    split and undercounted every dividend paid before one (re-audit F10).

    Returns {account: shares}; with ``per_account`` False the accounts are
    merged under the single key None, which is the shape the income row
    takes.
    """
    ledger = ledger_inputs.load(conn, symbol=symbol, as_of=as_of, units="current")
    sym = symbol.upper()
    if not per_account:
        return {None: holdings.holdings_on(ledger.lots, as_of).get(sym, 0.0)}
    by_account: dict[str | None, float] = {}
    for account in {lot.get("account") for lot in ledger.lots}:
        subset = [lot for lot in ledger.lots if lot.get("account") == account]
        qty = holdings.holdings_on(subset, as_of).get(sym, 0.0)
        if qty:
            by_account[account] = qty
    return by_account


def _insert_income(conn, **f) -> int:
    """Insert one income row; returns 1 when written, 0 when the dedupe key
    (symbol, account, kind, pay_date, amount) already existed."""
    return run(
        conn,
        """
        INSERT INTO income(symbol, account, kind, ex_date, pay_date, amount,
                           currency, tax_withheld, per_share, source, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            f["symbol"], f["account"], f["kind"], f["ex_date"], f["pay_date"],
            f["amount"], f["currency"], f["tax_withheld"], f["per_share"],
            f["source"], f["notes"],
        ),
    )


def _replace_estimates(conn, symbol: str, since=None) -> int:
    """Delete the symbol's earlier backfilled estimates; returns how many.

    Only rows the backfill itself wrote (``source = 'yfinance'``). Manual
    entries are never touched. Needed because the dedupe key includes the
    amount: a corrected estimate would otherwise be inserted *beside* the old
    one rather than replace it.

    ``since`` bounds the deletion to the same range the backfill rebuilds. The
    1.7.2 version deleted every estimate for the symbol and then let the
    insert loop skip the dates before ``--since``, so a bounded rerun removed
    older history and did not put it back (1.7.2 re-audit, N04). Backfilled
    rows carry ``ex_date`` = ``pay_date``; the bound reads whichever is set.
    """
    if since is None:
        return run(
            conn,
            "DELETE FROM income WHERE symbol = %s AND source = 'yfinance'",
            (symbol,),
        )
    return run(
        conn,
        "DELETE FROM income WHERE symbol = %s AND source = 'yfinance' "
        "AND COALESCE(ex_date, pay_date) >= %s",
        (symbol, since),
    )


def _backfill(
    conn, symbol: str, since, *, per_account: bool = False, replace_estimates: bool = False
) -> tuple[int, int]:
    """Insert one estimated DIVIDEND row per ex-date the ledger held shares on.

    Returns (inserted, duplicates): the dedupe key is (symbol, account, kind,
    pay_date, amount), so a rerun that inserts nothing reports every row as a
    duplicate instead of a silent zero. ``per_account`` writes one row per
    account; the default merges them under a NULL account, which is the shape
    every row backfilled before 1.7.0 has, so reruns keep deduplicating.
    ``replace_estimates`` deletes the symbol's earlier yfinance rows first —
    the way to correct amounts written by a version that counted them wrong.
    The deletion and the rebuild share one scope (``since``), the deletion
    happens only once the vendor series shows there is something to rebuild,
    and the caller's transaction makes the two halves atomic.
    """
    import yfinance as yf

    # yfinance states per-share dividends in today's (split-adjusted) units,
    # which is why the entitlement is counted in the same units
    # (_shares_held_on loads with units="current").
    divs = yf.Ticker(symbol).dividends   # per-share Series indexed by ex-date
    if divs is None or len(divs) == 0:
        print(f"No dividend history for {symbol}")
        return 0, 0

    to_rebuild = [(ts.date(), per_share) for ts, per_share in divs.items()
                  if since is None or ts.date() >= since]
    if not to_rebuild:
        print(f"No dividends for {symbol} on or after {since}; nothing rebuilt and no estimate removed")
        return 0, 0

    if replace_estimates:
        removed = _replace_estimates(conn, symbol, since)
        scope = f"dated on or after {since}" if since else "for all dates"
        print(f"replaced {removed} earlier estimate(s) for {symbol} {scope}")

    inserted = duplicates = 0
    for ex_d, per_share in to_rebuild:
        for account, shares in _shares_held_on(conn, symbol, ex_d, per_account=per_account).items():
            if shares <= 0:
                continue
            amount = round(float(per_share) * shares, 8)
            if amount <= 0:
                continue
            wrote = _insert_income(
                conn, symbol=symbol, account=account, kind="DIVIDEND", ex_date=ex_d,
                pay_date=ex_d, amount=amount, currency="USD", tax_withheld=0,
                per_share=float(per_share), source="yfinance", notes=BACKFILL_NOTE,
            )
            if wrote:
                inserted += 1
            else:
                duplicates += 1
    return inserted, duplicates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--backfill", action="store_true",
                    help="Pull dividend history from yfinance instead of manual entry")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD (backfill lower bound)")
    ap.add_argument("--per-account", action="store_true",
                    help="Backfill one row per account instead of one merged row (account NULL). "
                         "Rows written before 1.7.0 are merged; mixing the two double-counts.")
    ap.add_argument("--replace-estimates", action="store_true",
                    help="Delete this symbol's earlier backfilled (source=yfinance) rows before "
                         "inserting, so corrected amounts replace old estimates instead of "
                         "landing beside them. With --since, only estimates dated on or after "
                         "it are deleted — the same dates that are rebuilt. Manual rows are "
                         "never touched.")
    ap.add_argument("--account", default=None)
    ap.add_argument("--kind", choices=KINDS, default="DIVIDEND")
    ap.add_argument("--ex-date", default=None, help="YYYY-MM-DD")
    ap.add_argument("--pay-date", default=None, help="YYYY-MM-DD")
    # Finite, non-negative, exact Decimal — see app/ledger_numbers.py.
    ap.add_argument("--amount", type=ledger_numbers.money_arg, default=None, help="gross cash received")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--tax-withheld", type=ledger_numbers.money_arg, default=ledger_numbers.parse_money("0"))
    ap.add_argument("--per-share", type=ledger_numbers.money_arg, default=None)
    ap.add_argument("--notes", default=None)
    args = ap.parse_args()

    symbol = args.symbol.strip().upper()

    cfg = load_config()
    # One transaction for the whole command: instrument upsert + income
    # insert(s) commit together, and the connection is closed on exit.
    with transaction(cfg) as conn:
        run(
            conn,
            "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING",
            (symbol,),
        )

        if args.backfill:
            since = (
                datetime.strptime(args.since, "%Y-%m-%d").date() if args.since else None
            )
            inserted, duplicates = _backfill(
                conn, symbol, since,
                per_account=args.per_account, replace_estimates=args.replace_estimates,
            )
            print(
                f"OK backfilled {inserted} dividend row(s) for {symbol}"
                f" ({duplicates} already present). Rows are estimates: pay date = ex-date,"
                " entitlement from the ledger in today's split-adjusted shares."
            )
            return

        if args.pay_date is None or args.amount is None:
            ap.error("--pay-date and --amount are required for manual entry")

        _insert_income(
            conn, symbol=symbol, account=args.account, kind=args.kind,
            ex_date=args.ex_date, pay_date=args.pay_date, amount=args.amount,
            currency=args.currency, tax_withheld=args.tax_withheld,
            per_share=args.per_share, source="manual", notes=args.notes,
        )
        print(
            f"OK added {args.kind} {args.amount} {args.currency} for {symbol} "
            f"pay_date={args.pay_date}"
        )


if __name__ == "__main__":
    main()
