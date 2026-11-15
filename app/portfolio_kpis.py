"""Portfolio-level KPI definitions — the one place each is defined.

The dashboard payload, the Advisor snapshot and the executive report all roll
their positions up through this module, and the MCP is meant to call the same
functions (see "What the MCP must mirror" in docs/methodology.md). Pure: rows
in, Decimals out, no database and no clock.

Three definitions live here because each was computed differently on different
surfaces before 1.8.0:

**Unpriced positions are not losses** (audit F-12 / Q-2). A held position with
no quote has an unknown market value. The dashboard and the Advisor used to
subtract its whole cost from the market value of the priced positions, so an
unknown price read as a 100% loss, while the MCP summed per-row P&L and agreed
with neither on the percentage. `rollup()` sums market value and unrealized
P&L over *priced* rows only, reports the open cost of unpriced rows separately
(`unpriced_cost_basis`, `unpriced_symbols`), and states unrealized % against
the priced cost it was earned on.

**Total return is over invested capital** (audit F-5 / Q-4). The old figure
was lifetime realized + unrealized P&L divided by the cost of what is *still
open*: after selling most of a profitable book it exploded (900% on a book
that made 90%), and after selling everything it read 0.0%. `total_return_pct()`
divides the same P&L by `peak_net_invested()`, the most capital the ledger
ever had committed at the close of a trade date: running buy outlays (price ×
quantity + fees) minus sale proceeds (price × quantity − fees), maximised over
the ledger's dates. It does not move when a position is sold, it is the same
before and after a full liquidation, and recycling the proceeds of a sale into
the next purchase does not inflate it. It is a money measure, not a TWR; the
time-weighted returns strip remains the performance measure.

**Money in another currency is never summed as dollars silently** (audit F-11
/ Q-8). The ledger is USD-only (docs/methodology.md, "Currency"). Face amounts
recorded in another currency (income, cash) are excluded from dollar totals
and listed; instruments registered in another currency stay in the valuation —
their quotes are whatever the vendor returned, and hiding a holding would be
worse — but are flagged on every surface by `currency_warnings()`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, tzinfo
from decimal import Decimal
from typing import Any, Iterable, Mapping

BASE_CURRENCY = "USD"

ZERO = Decimal("0")
HUNDRED = Decimal("100")

TOTAL_RETURN_BASIS = (
    "(realized + unrealized P&L on priced positions) / peak net invested capital "
    "(the most capital the ledger had committed at any trade date's close)"
)


def to_dec(x: Any) -> Decimal | None:
    """Decimal of a DB / engine value; None for None and NaN."""
    if x is None:
        return None
    if isinstance(x, Decimal):
        return None if x.is_nan() else x
    try:
        d = Decimal(str(x))
    except Exception:
        return None
    return None if d.is_nan() else d


def pct(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    """numerator / denominator × 100, or None when either is missing or the
    denominator is not positive (a ratio over nothing is undefined, not 0%)."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator * HUNDRED


def peak_net_invested(lot_rows: Iterable[Mapping[str, Any]]) -> Decimal:
    """The most capital the ledger had committed at the close of any trade date.

    Net invested after a date = Σ BUY (qty × price + fees) − Σ SELL (qty × price
    − fees) over every lot dated on or before it. Lots are aggregated per date
    first, so the order of a same-day buy and sell does not matter. Units do not
    matter either: qty × price is unchanged by a split restatement. Pass the
    rows the caller valued (restated, account- or cutoff-filtered alike).
    """
    by_day: dict[Any, Decimal] = defaultdict(lambda: ZERO)
    for r in lot_rows:
        qty = to_dec(r.get("quantity")) or ZERO
        price = to_dec(r.get("price")) or ZERO
        fees = to_dec(r.get("fees")) or ZERO
        side = str(r.get("side") or "").upper()
        if side == "BUY":
            by_day[r["trade_date"]] += qty * price + fees
        elif side == "SELL":
            by_day[r["trade_date"]] -= qty * price - fees
    net = peak = ZERO
    for day in sorted(by_day):
        net += by_day[day]
        peak = max(peak, net)
    return peak


def total_return_pct(
    realized: Decimal, unrealized: Decimal, invested: Decimal, income: Decimal = ZERO
) -> Decimal | None:
    """(realized + unrealized [+ income]) / peak net invested × 100."""
    return pct(realized + unrealized + income, invested)


def rollup(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Priced / unpriced roll-up of position rows.

    Each row needs `symbol`, `qty`, `open_cost`, `realized_pnl` and
    `last_price` (None, or absent, when the position has no quote). Rows with
    no open quantity contribute realized P&L only.
    """
    market_value = priced_cost = unpriced_cost = unrealized = realized = ZERO
    priced: list[str] = []
    unpriced: list[str] = []
    for r in rows:
        realized += to_dec(r.get("realized_pnl")) or ZERO
        qty = to_dec(r.get("qty")) or ZERO
        if qty <= 0:
            continue
        cost = to_dec(r.get("open_cost")) or ZERO
        last = to_dec(r.get("last_price"))
        if last is None:
            unpriced_cost += cost
            unpriced.append(str(r["symbol"]))
            continue
        mv = qty * last
        market_value += mv
        priced_cost += cost
        unrealized += mv - cost
        priced.append(str(r["symbol"]))
    return {
        "market_value": market_value,
        "priced_cost_basis": priced_cost,
        "unpriced_cost_basis": unpriced_cost,
        "cost_basis": priced_cost + unpriced_cost,
        "unrealized_pnl": unrealized,
        "unrealized_pct": pct(unrealized, priced_cost),
        "realized_pnl": realized,
        "priced_symbols": sorted(priced),
        "unpriced_symbols": sorted(unpriced),
    }


def summary(
    rows: Iterable[Mapping[str, Any]],
    lot_rows: Iterable[Mapping[str, Any]],
    income: Decimal = ZERO,
) -> dict[str, Any]:
    """`rollup()` plus invested capital and both total-return figures."""
    out = rollup(rows)
    invested = peak_net_invested(lot_rows)
    out["invested_capital"] = invested
    out["total_return_pct"] = total_return_pct(out["realized_pnl"], out["unrealized_pnl"], invested)
    out["total_return_with_income_pct"] = total_return_pct(
        out["realized_pnl"], out["unrealized_pnl"], invested, income
    )
    # The numerator leaves out what an unpriced position has gained or lost,
    # because nobody knows; the figure is partial until it is priced.
    out["total_return_partial"] = bool(out["unpriced_symbols"])
    return out


def as_float(x: Decimal | None, places: int | None = None) -> float | None:
    if x is None:
        return None
    f = float(x)
    return round(f, places) if places is not None else f


# ─── Quote freshness (the reports' one rule, audit F-8) ──────


def portfolio_as_of(
    quote_ts: Mapping[str, datetime | None],
    held_symbols: Iterable[str],
    benchmark_symbols: Iterable[str] = (),
) -> datetime | None:
    """When the portfolio was last priced: the newest quote of a HELD symbol.

    Benchmarks (index futures for the Markets strip) are quoted around the
    clock, so the newest row in price_snapshots is usually not a portfolio
    quote. Letting it decide "as of" moved the report's day onto a weekend and
    made the day change compare Friday's close with itself — a false $0.00
    (audit F-8). With nothing held, the newest non-benchmark quote; None when
    there is none.
    """
    held = set(held_symbols)
    stamps = [ts for s, ts in quote_ts.items() if s in held and ts is not None]
    if not stamps:
        bench = set(benchmark_symbols)
        stamps = [ts for s, ts in quote_ts.items() if s not in bench and ts is not None]
    return max(stamps) if stamps else None


def stale_quotes(
    quote_ts: Mapping[str, datetime | None],
    symbols: Iterable[str],
    as_of: datetime | None,
    tz: tzinfo,
) -> list[dict[str, Any]]:
    """Symbols valued at a quote from an earlier reporting-local day than `as_of`.

    The executive and the daily report both value a held symbol at its latest
    quote at or before the report instant and both list it here when that
    quote is from an earlier day: the figure is carried forward, said so, and
    left out of the day's change (it has no move today that anyone observed).
    A symbol with no quote at all is unpriced, not stale (see rollup()).
    """
    if as_of is None:
        return []
    day = as_of.astimezone(tz).date()
    out = []
    for s in sorted(set(symbols)):
        ts = quote_ts.get(s)
        if ts is not None and ts.astimezone(tz).date() < day:
            out.append({"symbol": s, "ts": ts})
    return out


# ─── Currency ────────────────────────────────────────────────


def currency_of(row: Mapping[str, Any]) -> str:
    """A row's currency, upper-cased; a missing value is the base currency."""
    c = row.get("currency")
    return str(c).strip().upper() if c is not None and str(c).strip() else BASE_CURRENCY


def split_by_currency(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict], list[dict]]:
    """(base-currency rows, other-currency rows)."""
    base: list[dict] = []
    other: list[dict] = []
    for r in rows:
        (base if currency_of(r) == BASE_CURRENCY else other).append(dict(r))
    return base, other


def currency_warnings(
    *,
    instruments: Iterable[Mapping[str, Any]] = (),
    traded_symbols: Iterable[str] = (),
    held_symbols: Iterable[str] = (),
    income: Iterable[Mapping[str, Any]] = (),
    cash: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Everything in the ledger recorded in a currency other than USD.

    `instruments` are {symbol, currency} rows; only symbols in `traded_symbols`
    (ever bought or sold) are reported, since a watchlist symbol touches no
    total. `income` rows need `currency`, `amount` and optionally `symbol`;
    `cash` rows need `account`, `currency`, `cash`. Each warning says what was
    done with the amount: `included_unconverted` (instruments) or `excluded`.
    """
    traded = {s.upper() for s in traded_symbols}
    held = {s.upper() for s in held_symbols}
    out: list[dict[str, Any]] = []
    for r in instruments:
        sym = str(r.get("symbol") or "").upper()
        cur = currency_of(r)
        if sym in traded and cur != BASE_CURRENCY:
            out.append({
                "kind": "instrument", "symbol": sym, "currency": cur, "held": sym in held,
                "treatment": "included_unconverted",
                "message": (
                    f"{sym} is registered in {cur}; its quotes and trades are added "
                    f"to {BASE_CURRENCY} totals without conversion."
                ),
            })
    by_cur: dict[str, dict[str, Any]] = {}
    for r in income:
        cur = currency_of(r)
        if cur == BASE_CURRENCY:
            continue
        e = by_cur.setdefault(cur, {"count": 0, "amount": ZERO, "symbols": set()})
        e["count"] += 1
        e["amount"] += to_dec(r.get("amount")) or ZERO
        if r.get("symbol"):
            e["symbols"].add(str(r["symbol"]).upper())
    for cur, e in sorted(by_cur.items()):
        out.append({
            "kind": "income", "currency": cur, "count": e["count"],
            "amount": float(e["amount"]), "symbols": sorted(e["symbols"]),
            "treatment": "excluded",
            "message": (
                f"{e['count']} income record(s) in {cur} ({float(e['amount']):,.2f} {cur}) "
                f"are left out of the {BASE_CURRENCY} income totals."
            ),
        })
    for r in cash:
        cur = currency_of(r)
        if cur == BASE_CURRENCY:
            continue
        amount = to_dec(r.get("cash")) or ZERO
        acct = r.get("account")
        out.append({
            "kind": "cash", "account": acct, "currency": cur, "amount": float(amount),
            "treatment": "excluded",
            "message": (
                f"Cash in {acct or '(no account)'} is entered in {cur} "
                f"({float(amount):,.2f} {cur}) and is left out of the {BASE_CURRENCY} cash total."
            ),
        })
    return out
