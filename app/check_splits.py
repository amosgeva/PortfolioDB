"""Scan price history for unrecorded splits, then check each hit against the vendor.

The price-ratio heuristic alone **cannot** tell a split from a one-day crash —
both halve the price overnight. That is not a theoretical caveat: PRIM fell
0.4997 in a day on 2026-05-06 and was recorded as a 2:1 split on that evidence.
It was a real decline, and the wrong adjustment distorted every return spanning
the date until it was corrected.

So every hit is cross-referenced against yfinance, which keeps an
authoritative split register, and labelled SPLIT (vendor confirms), NOT A SPLIT
(vendor contradicts) or UNVERIFIED (vendor unreachable, or --no-vendor-check).
A hit the vendor does not confirm is evidence *against* a split, not merely an
absence of evidence. (Until 1.8.0 this docstring promised the cross-check and
main() never called it; audit F-4.)

It also lists splits the collector recorded that are PENDING REVIEW — they
change no figure until approved — and approves one:

    python check_splits.py --approve NVDA:2024-06-10 --shares-credited
    python check_splits.py --approve NVDA:2024-06-10 --quantities-already-post-split

``--shares-credited`` (the usual case: the broker added the shares, and the
lots entered before the split are in old units) applies the split to prices
and lots together. ``--quantities-already-post-split`` applies it to prices
only, for lots already entered in post-split units. Either way the split then
takes effect everywhere at once. Approval is the only write this script does.

Usage (from app/, with PORTFOLIODB_PASSWORD set):

    python check_splits.py
    python check_splits.py --symbol PRIM --tolerance 0.05
    python check_splits.py --no-vendor-check        # offline: hits are UNVERIFIED

Exit code is 1 when anything is flagged (a suspected split or a pending
review), so a scheduled run can alert; 2 for bad arguments or a failed
approval.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date

import corporate_actions
import reporting_tz
import symbol_paths
from db import connect, load_config


def fetch_daily_prices(conn, symbol: str | None) -> dict[str, list[tuple]]:
    """{symbol: [(day, last_price)]} — one price per reporting-timezone day."""
    tz = reporting_tz.tz_name()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (symbol, date_trunc('day', ts AT TIME ZONE %s))
                   symbol,
                   date_trunc('day', ts AT TIME ZONE %s)::date AS day_local,
                   last_price
            FROM price_snapshots
            WHERE last_price > 0
              AND (%s::text IS NULL OR symbol = %s)
            ORDER BY symbol,
                     date_trunc('day', ts AT TIME ZONE %s),
                     ts DESC
            """,
            (tz, tz, symbol, symbol, tz),
        )
        out: dict[str, list[tuple]] = defaultdict(list)
        for sym, day, price in cur.fetchall():
            out[sym].append((day, float(price)))
    return out


def _symbol_resolves(symbol: str) -> bool:
    """Whether yfinance knows this ticker at all."""
    try:
        import yfinance as yf

        info = yf.Ticker(symbol).info
        return bool(
            info
            and (info.get("regularMarketPrice") is not None
                 or info.get("currentPrice") is not None
                 or info.get("previousClose") is not None)
        )
    except Exception:
        return False


def vendor_verdict(symbol: str, day) -> dict:
    """Ask yfinance whether it knows of a split for this symbol near this date.

    A split within a few days either side counts as confirmation — vendor
    ex-dates and the day a price step becomes visible in our snapshots can
    differ by a session.

    Any failure returns 'unknown', never 'contradicted': being unable to reach
    the vendor is not evidence that a split did not happen.
    """
    try:
        import yfinance as yf

        splits = yf.Ticker(symbol).splits
    except Exception as e:
        return {"status": "unknown", "detail": f"vendor lookup failed: {e}"}

    if splits is None or len(splits) == 0:
        # An empty series is ambiguous: a real symbol that never split looks
        # exactly like a ticker yfinance cannot resolve at all. Confirm the
        # symbol resolves before treating the emptiness as evidence — reporting
        # "not a split" because the lookup silently failed would be the same
        # over-confidence this cross-check exists to prevent.
        if not _symbol_resolves(symbol):
            return {
                "status": "unknown",
                "detail": (
                    "yfinance returned no data for this symbol at all — cannot "
                    "tell 'never split' from 'could not look it up'."
                ),
            }
        return {
            "status": "contradicted",
            "detail": (
                "yfinance lists no splits for this symbol, ever — a price step "
                "this size is far more likely a real move."
            ),
        }

    for ts, ratio in splits.items():
        vendor_day = ts.date() if hasattr(ts, "date") else ts
        if abs((vendor_day - day).days) <= 3:
            return {
                "status": "confirmed",
                "detail": f"yfinance reports a {float(ratio):g}:1 split on {vendor_day}",
            }

    latest = max(
        (t.date() if hasattr(t, "date") else t) for t in splits.index
    )
    return {
        "status": "contradicted",
        "detail": (
            f"yfinance knows {len(splits)} split(s) for this symbol but none "
            f"near {day} (most recent {latest})."
        ),
    }


VERDICT_LABEL = {
    "confirmed": "SPLIT (vendor confirms)",
    "contradicted": "NOT A SPLIT (vendor contradicts)",
    "unknown": "UNVERIFIED",
}


def _parse_target(text: str) -> tuple[str, date]:
    """'NVDA:2024-06-10' -> ('NVDA', date)."""
    sym, sep, day = text.partition(":")
    if not sep:
        raise argparse.ArgumentTypeError("use SYMBOL:YYYY-MM-DD, e.g. NVDA:2024-06-10")
    try:
        return symbol_paths.normalize_symbol(sym), date.fromisoformat(day.strip())
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def approve(conn, symbol: str, ex_date: date, *, shares_credited: bool) -> int:
    """Mark one pending action reviewed and set adjust_lots; returns rows changed.

    Refuses to approve a row that would duplicate an action already in effect
    (the database trigger would also refuse it on update).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE corporate_actions
            SET reviewed = TRUE,
                adjust_prices = TRUE,
                adjust_lots = %s,
                notes = COALESCE(notes || ' ', '') || %s
            WHERE symbol = %s AND ex_date = %s AND kind IN ('SPLIT', 'REVERSE_SPLIT')
            RETURNING id
            """,
            (
                shares_credited,
                f"[approved {date.today().isoformat()}: "
                + ("shares credited — prices and lots adjusted]" if shares_credited
                   else "quantities already post-split — prices adjusted, lots not]"),
                symbol, ex_date,
            ),
        )
        changed = len(cur.fetchall())
    conn.commit()
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", type=symbol_paths.symbol_arg, help="Restrict the scan to one symbol.")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=corporate_actions.DETECT_TOLERANCE,
        help="Max fractional distance from a common split ratio (default %(default)s).",
    )
    parser.add_argument(
        "--no-vendor-check",
        action="store_true",
        help="Skip the yfinance cross-reference (offline use); every hit is reported UNVERIFIED.",
    )
    parser.add_argument("--approve", type=_parse_target, metavar="SYMBOL:YYYY-MM-DD",
                        help="Approve a pending split (needs one of the two flags below).")
    credited = parser.add_mutually_exclusive_group()
    credited.add_argument("--shares-credited", action="store_true",
                          help="The broker credited the new shares: adjust prices AND lots.")
    credited.add_argument("--quantities-already-post-split", action="store_true",
                          help="Lots before the split were entered in post-split units: adjust prices only.")
    args = parser.parse_args(argv)

    if args.approve and not (args.shares_credited or args.quantities_already_post_split):
        parser.error("--approve needs --shares-credited or --quantities-already-post-split: "
                     "whether the lots must be restated is the one thing the vendor cannot tell you")

    cfg = load_config()
    conn = connect(cfg)
    try:
        if args.approve:
            sym, day = args.approve
            try:
                n = approve(conn, sym, day, shares_credited=args.shares_credited)
            except Exception as e:
                conn.rollback()
                print(f"Approval refused: {str(e).strip().splitlines()[0]}")
                return 2
            if not n:
                print(f"No split recorded for {sym} on {day}. Nothing approved.")
                return 2
            print(f"Approved {sym} {day}: it now applies to prices"
                  + (" and lots." if args.shares_credited else " (lots unchanged)."))
            return 0
        recorded = corporate_actions.fetch_recorded_actions(conn)
        prices = fetch_daily_prices(conn, args.symbol)
    finally:
        conn.close()

    effective = corporate_actions.effective_actions(recorded)
    pending = [a for a in recorded if a.pending_review]
    ignored = corporate_actions.near_duplicates(recorded)
    # Every recorded row counts as "known" for the scan: a pending split is
    # reported below as pending, not again as a suspected one.
    found = corporate_actions.detect_suspected_splits(
        prices, known=recorded, tolerance=args.tolerance
    )

    if effective:
        print(f"{len(effective)} action(s) in effect:")
        for a in effective:
            flags = []
            if not a.adjust_prices:
                flags.append("prices NOT adjusted")
            if not a.adjust_lots:
                flags.append("lots NOT adjusted")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  {a.symbol:8} {a.ex_date}  ratio {a.ratio}{suffix}")
        print()

    for dup, kept in ignored:
        print(f"DUPLICATE: {dup.symbol} {dup.kind} {dup.ratio} on {dup.ex_date} repeats the one on "
              f"{kept.ex_date} and is ignored. Delete the duplicate row.")
    if ignored:
        print()

    if pending:
        print(f"{len(pending)} split(s) PENDING REVIEW — recorded from the vendor, changing no figure yet:")
        for a in pending:
            print(f"  {a.symbol:8} {a.ex_date}  ratio {a.ratio}  (source {a.source})")
        print("Confirm each against the broker, then:\n"
              "  python check_splits.py --approve SYMBOL:YYYY-MM-DD --shares-credited\n"
              "  (or --quantities-already-post-split if those lots were entered in new units)\n")

    if not found:
        print("No unrecorded splits detected.")
        return 1 if pending or ignored else 0

    print(f"{len(found)} suspected unrecorded split(s):\n")
    for f in found:
        if args.no_vendor_check:
            verdict = {"status": "unknown", "detail": "vendor check skipped (--no-vendor-check)"}
        else:
            verdict = vendor_verdict(f["symbol"], f["day"])
        f["verdict"] = verdict
        print(
            f"  {f['symbol']:8} {f['day']}  "
            f"{f['prev_price']:.2f} -> {f['price']:.2f}  "
            f"implies {f['observed_ratio']:.4f}:1  "
            f"(nearest {f['nearest_ratio']:g}:1, off by {f['deviation'] * 100:.2f}%)\n"
            f"           {VERDICT_LABEL[verdict['status']]}: {verdict['detail']}"
        )
    print(
        "\nRecord only what the vendor confirms (or you have verified elsewhere). A NOT A\n"
        "SPLIT hit is most likely a real price move: record it as kind 'NONE', ratio 1, so\n"
        "the scan stops reporting it.\n"
        "  INSERT INTO corporate_actions\n"
        "    (symbol, kind, ex_date, ex_ts, ratio, adjust_prices, adjust_lots, reviewed, notes)\n"
        "  VALUES ('SYM', 'SPLIT', DATE 'YYYY-MM-DD', TIMESTAMPTZ '... 16:30:00+03',\n"
        "          2, TRUE, <TRUE if the shares were actually credited>, TRUE, 'why');\n"
        "\nex_ts should be the ex-date's regular-session open — raw-timestamp\n"
        "readers need it to classify that day's pre-open snapshots correctly."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
