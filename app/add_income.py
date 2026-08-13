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
"""

from __future__ import annotations

import argparse
from datetime import datetime

from db import fetch_all, load_config, run, transaction

KINDS = ["DIVIDEND", "INTEREST", "CAP_GAIN_DIST"]


def _shares_held_on(conn, symbol: str, as_of) -> float:
    """Net BUY−SELL quantity for a symbol on/before a date (all accounts)."""
    rows = fetch_all(
        conn,
        """
        SELECT COALESCE(
                 SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END), 0
               ) AS qty
        FROM lots
        WHERE symbol = %s AND trade_date <= %s
        """,
        (symbol, as_of),
    )
    return float(rows[0]["qty"]) if rows else 0.0


def _insert_income(conn, **f) -> None:
    run(
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


def _backfill(conn, symbol: str, since) -> int:
    """Insert one DIVIDEND row per ex-date the user held shares on."""
    import yfinance as yf

    divs = yf.Ticker(symbol).dividends   # per-share Series indexed by ex-date
    if divs is None or len(divs) == 0:
        print(f"No dividend history for {symbol}")
        return 0

    inserted = 0
    for ts, per_share in divs.items():
        ex_d = ts.date()
        if since and ex_d < since:
            continue
        shares = _shares_held_on(conn, symbol, ex_d)
        if shares <= 0:
            continue
        amount = round(float(per_share) * shares, 8)
        if amount <= 0:
            continue
        _insert_income(
            conn, symbol=symbol, account=None, kind="DIVIDEND", ex_date=ex_d,
            pay_date=ex_d, amount=amount, currency="USD", tax_withheld=0,
            per_share=float(per_share), source="yfinance", notes="auto-backfill",
        )
        inserted += 1
    return inserted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--backfill", action="store_true",
                    help="Pull dividend history from yfinance instead of manual entry")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD (backfill lower bound)")
    ap.add_argument("--account", default=None)
    ap.add_argument("--kind", choices=KINDS, default="DIVIDEND")
    ap.add_argument("--ex-date", default=None, help="YYYY-MM-DD")
    ap.add_argument("--pay-date", default=None, help="YYYY-MM-DD")
    ap.add_argument("--amount", type=float, default=None, help="gross cash received")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--tax-withheld", type=float, default=0.0)
    ap.add_argument("--per-share", type=float, default=None)
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
            n = _backfill(conn, symbol, since)
            print(f"OK backfilled {n} dividend row(s) for {symbol}")
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
