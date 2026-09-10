"""Generate a deterministic weekly PortfolioDB report from Postgres.

Purpose: provide the numeric source of truth for the Saturday weekly deep dive.
- Uses PortfolioDB snapshots, lots, and cash snapshots only.
- No hardcoded dates.
- No legacy SQLite fallback.
- Handles trades during the week by valuing start/end holdings as-of each snapshot date.
- States every quantity and price of the week in one unit basis (the end
  snapshot's), so a split inside the week is a share count, not a move.

Usage:
  python C:\\Install\\PortfolioDB\\app\\report_weekly_db.py
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import fd_store
import ledger_inputs
from db import connect, fetch_all, load_config
from portfolio import compute_fifo_merged
from reporting_utils import IL_TZ, money, pct, utf8_stdout

UTC = timezone.utc


def D(x) -> Decimal:
    if x is None:
        return Decimal("0")
    return Decimal(str(x))


def compact_money(x) -> str:
    if x is None:
        return "n/a"
    try:
        n = float(x)
    except (TypeError, ValueError):
        return "n/a"
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000_000:
        return f"{sign}${n / 1_000_000_000_000:.2f}T"
    if n >= 1_000_000_000:
        return f"{sign}${n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{sign}${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{sign}${n / 1_000:.2f}K"
    return f"{sign}${n:.2f}"


def fd_pct(x, digits: int = 1, signed: bool = False) -> str:
    """Format a fractional value as a percent. `signed=True` adds `+` for growth-style metrics."""
    if x is None:
        return "—"
    try:
        v = float(x) * 100
    except (TypeError, ValueError):
        return "—"
    fmt = f"{{:+.{digits}f}}%" if signed else f"{{:.{digits}f}}%"
    return fmt.format(v)


def fd_ratio(x, digits: int = 2) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def snapshot_at_or_after(conn, dt_utc: datetime):
    rows = fetch_all(conn, "SELECT MIN(ts) AS ts FROM price_snapshots WHERE ts >= %s", (dt_utc,))
    return rows[0]["ts"] if rows and rows[0]["ts"] else None


def latest_snapshot(conn):
    rows = fetch_all(conn, "SELECT MAX(ts) AS ts FROM price_snapshots")
    return rows[0]["ts"] if rows and rows[0]["ts"] else None


def price_map(conn, ts: datetime, ledger: ledger_inputs.PreparedLedger | None = None) -> dict[str, Decimal]:
    """Quotes stamped at one snapshot, restated into ``ledger``'s units.

    The positions these are multiplied by come out of the prepared ledger in
    the end-of-week units, so a quote observed before a split that the ledger
    has applied must be restated too — otherwise the start-of-week quote is
    twice the end-of-week one for the same holding and the report prints a
    pure split as a 50% loss (1.7.2 re-audit, F03 case A). Without a ledger the
    raw quotes are returned.
    """
    rows = fetch_all(conn, "SELECT symbol, last_price FROM price_snapshots WHERE ts=%s", (ts,))
    points = [(ts, r["symbol"], float(r["last_price"])) for r in rows if r.get("last_price") is not None]
    if ledger is not None:
        points = ledger.price_points(points)
    return {sym: D(px) for _, sym, px in points}


def cash_as_of(conn, ts: datetime) -> dict[str, Decimal]:
    rows = fetch_all(
        conn,
        """
        SELECT DISTINCT ON (account) account, cash, ts
        FROM cash_snapshots
        WHERE ts <= %s
        ORDER BY account, ts DESC
        """,
        (ts,),
    )
    return {r["account"]: D(r["cash"]) for r in rows}


def positions_as_of(conn, asof_date) -> dict[tuple[str, str], Decimal]:
    """Return net qty by (account, symbol) for lots with trade_date <= asof_date.

    Quantities are engine-independent (net BUY−SELL == FIFO open qty), so this
    intentionally does NOT go through the FIFO engine — only cost attribution
    differs between engines, and cost comes from current_cost_by_symbol. It does
    go through the prepared ledger, so the quantities are in the units the
    prices they are multiplied by are quoted in: a raw pre-split share count
    against a post-split quote halved (or quartered) the position's value.
    """
    return positions_from(ledger_inputs.load(conn, as_of=asof_date).lots, asof_date)


def positions_from(lots, asof_date) -> dict[tuple[str, str], Decimal]:
    """positions_as_of for lots already prepared — the caller picks the units.

    main() prepares one ledger for the whole week and derives both the start
    and the end positions from it, so the two share counts it subtracts and
    the quotes it multiplies them by are all in the same units.
    """
    out: dict[tuple[str | None, str], Decimal] = defaultdict(Decimal)
    for lot in lots:
        if lot["trade_date"] > asof_date:
            continue
        qty = D(lot["quantity"])
        out[(lot["account"], lot["symbol"])] += qty if lot["side"] == "BUY" else -qty
    # The account may be NULL (the schema allows it, the CLIs allow omitting
    # it), and Python will not order None against a string — the 1.7.0 version
    # crashed the whole report the moment an unnamed account met a named one
    # (re-audit N01). Sort on a key that places unnamed accounts first; the
    # returned keys keep the account exactly as stored, so None and "" stay
    # distinct rather than being folded together.
    ordered = sorted(out.items(), key=lambda kv: (kv[0][0] is not None, kv[0][0] or "", kv[0][1]))
    return {k: q for k, q in ordered if abs(q) > Decimal("0.0000001")}


def value_positions(positions: dict[tuple[str, str], Decimal], prices: dict[str, Decimal]):
    by_symbol = defaultdict(Decimal)
    by_account = defaultdict(Decimal)
    missing = []
    for (acct, sym), qty in positions.items():
        px = prices.get(sym)
        if px is None:
            missing.append(sym)
            continue
        val = qty * px
        by_symbol[sym] += val
        by_account[acct] += val
    return by_symbol, by_account, sorted(set(missing))


def current_cost_by_symbol(conn) -> dict[str, Decimal]:
    """FIFO open cost per symbol with an open position.

    Routed through portfolio.compute_fifo_merged — the same engine the daily
    report and dashboard use — replacing the old buys-minus-sell-PROCEEDS
    approximation, which folded realized P&L into 'cost' and drifted from
    every other surface after any partial sell.
    """
    df = compute_fifo_merged(ledger_inputs.load(conn).lots)
    out: dict[str, Decimal] = {}
    if not df.empty:
        for _, r in df.iterrows():
            if r["qty"] > 0:
                out[r["symbol"]] = D(r["open_cost"])
    return out


def trades_between(conn, start_date, end_date):
    return fetch_all(
        conn,
        """
        SELECT trade_date, account, side, symbol, quantity, price, fees, notes
        FROM lots
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date, id
        """,
        (start_date, end_date),
    )


def qty_by_symbol(positions: dict[tuple[str, str], Decimal]) -> dict[str, Decimal]:
    out = defaultdict(Decimal)
    for (_acct, sym), qty in positions.items():
        out[sym] += qty
    return dict(out)


def main():
    utf8_stdout()

    now_il = datetime.now(IL_TZ)
    week_start_date = now_il.date() - timedelta(days=now_il.weekday())
    start_il = datetime.combine(week_start_date, time(16, 15)).replace(tzinfo=IL_TZ)
    start_utc = start_il.astimezone(UTC)

    cfg = load_config()
    with connect(cfg) as conn:
        start_ts = snapshot_at_or_after(conn, start_utc)
        end_ts = latest_snapshot(conn)
        if not start_ts or not end_ts:
            raise RuntimeError("Missing start or end price snapshot")

        start_date = start_ts.astimezone(IL_TZ).date()
        end_date = end_ts.astimezone(IL_TZ).date()

        # One prepared ledger for the week, in the end snapshot's units. Both
        # position snapshots, both quote maps and the week's trades are read
        # through it, so a split inside the week changes no value and
        # contributes nothing — it used to enter as a raw pre-split share
        # count against a post-split quote (F03 case A).
        ledger = ledger_inputs.load(conn, as_of=end_date)
        start_prices = price_map(conn, start_ts, ledger)
        end_prices = price_map(conn, end_ts, ledger)
        start_pos = positions_from(ledger.lots, start_date)
        end_pos = positions_from(ledger.lots, end_date)
        start_sym_val, start_acct_val, start_missing = value_positions(start_pos, start_prices)
        end_sym_val, end_acct_val, end_missing = value_positions(end_pos, end_prices)
        start_qty = qty_by_symbol(start_pos)
        end_qty = qty_by_symbol(end_pos)
        start_cash = cash_as_of(conn, start_ts)
        end_cash = cash_as_of(conn, end_ts)

        start_sec = sum(start_sym_val.values(), Decimal("0"))
        end_sec = sum(end_sym_val.values(), Decimal("0"))
        start_cash_total = sum(start_cash.values(), Decimal("0"))
        end_cash_total = sum(end_cash.values(), Decimal("0"))
        start_total = start_sec + start_cash_total
        end_total = end_sec + end_cash_total
        delta = end_total - start_total
        delta_pct = (delta / start_total * Decimal("100")) if start_total else Decimal("0")

        cost = current_cost_by_symbol(conn)
        current_rows = []
        for sym, val in sorted(end_sym_val.items(), key=lambda x: x[1], reverse=True):
            c = cost.get(sym, Decimal("0"))
            pnl = val - c if c else Decimal("0")
            pnl_pct = (pnl / c * Decimal("100")) if c else Decimal("0")
            current_rows.append((sym, val, c, pnl, pnl_pct))

        # What was entered, for the listing; the P&L arithmetic below uses the
        # prepared rows of the same trades, in the week's units.
        trades = trades_between(conn, week_start_date, end_date)
        week_lots = [lot for lot in ledger.lots if week_start_date <= lot["trade_date"] <= end_date]
        in_week = set(start_qty) | set(end_qty) | {lot["symbol"] for lot in week_lots}
        week_actions = [
            a for a in ledger.actions
            if a.symbol in in_week and start_date < a.ex_date <= end_date and a.ratio != 1
        ]

        # Contribution should measure market P&L, not capital added/removed by trades.
        # For existing shares: start_qty * (end_price - start_price).
        # For buys during the week: current value minus trade cost/fees.
        # This avoids making a new buy look like a huge "winner" just because capital moved from cash to securities.
        trade_pnl_by_symbol = defaultdict(Decimal)
        for t in week_lots:
            sym = t["symbol"]
            end_px = end_prices.get(sym)
            if end_px is None:
                continue
            qty = D(t["quantity"])
            trade_px = D(t["price"])
            fees = D(t.get("fees"))
            if t["side"] == "BUY":
                trade_pnl_by_symbol[sym] += qty * (end_px - trade_px) - fees
            elif t["side"] == "SELL":
                start_px = start_prices.get(sym)
                if start_px is not None:
                    trade_pnl_by_symbol[sym] += qty * (trade_px - start_px) - fees

        all_symbols = sorted(set(start_sym_val) | set(end_sym_val) | set(trade_pnl_by_symbol))
        contribs = []
        for sym in all_symbols:
            sp = start_prices.get(sym)
            ep = end_prices.get(sym)
            held_qty = start_qty.get(sym, Decimal("0"))
            price_move = held_qty * (ep - sp) if sp is not None and ep is not None else Decimal("0")
            trade_pnl = trade_pnl_by_symbol.get(sym, Decimal("0"))
            d = price_move + trade_pnl
            base = start_sym_val.get(sym, Decimal("0"))
            p = (d / base * Decimal("100")) if base else Decimal("0")
            qty_delta = end_qty.get(sym, Decimal("0")) - start_qty.get(sym, Decimal("0"))
            contribs.append((sym, d, p, price_move, trade_pnl, qty_delta, start_sym_val.get(sym, Decimal("0")), end_sym_val.get(sym, Decimal("0"))))
        contribs.sort(key=lambda x: x[1], reverse=True)

        print("📊 WEEKLY PORTFOLIO NUMBERS — PortfolioDB Source of Truth")
        print(f"Week: {week_start_date} → {end_date}")
        print(f"Start snapshot: {start_ts.astimezone(IL_TZ).strftime('%Y-%m-%d %H:%M IL')}")
        print(f"End snapshot:   {end_ts.astimezone(IL_TZ).strftime('%Y-%m-%d %H:%M IL')}")
        print()
        print("💰 WEEK IN NUMBERS")
        print(f"Start securities: {money(start_sec)} | cash: {money(start_cash_total)} | total: {money(start_total)}")
        print(f"End securities:   {money(end_sec)} | cash: {money(end_cash_total)} | total: {money(end_total)}")
        print(f"Weekly change:    {money(delta)} ({pct(delta_pct)})")
        print()
        print("🏦 ACCOUNT TOTALS")
        for acct in sorted(set(start_acct_val) | set(end_acct_val) | set(start_cash) | set(end_cash)):
            s = start_acct_val.get(acct, Decimal("0")) + start_cash.get(acct, Decimal("0"))
            e = end_acct_val.get(acct, Decimal("0")) + end_cash.get(acct, Decimal("0"))
            d = e - s
            print(f"{acct}: {money(s)} → {money(e)} ({money(d)})")
        print()
        print("🟢 TOP CONTRIBUTORS")
        for sym, d, p, price_move, trade_pnl, qty_delta, sv, ev in contribs[:5]:
            trade_note = f" | trade P&L {money(trade_pnl)}" if trade_pnl else ""
            qty_note = f" | qty Δ {float(qty_delta):+.4f}" if qty_delta else ""
            print(f"{sym}: {money(d)} ({pct(p)}) | {money(sv)} → {money(ev)}{trade_note}{qty_note}")
        print()
        print("🔴 BOTTOM CONTRIBUTORS")
        for sym, d, p, price_move, trade_pnl, qty_delta, sv, ev in sorted(contribs, key=lambda x: x[1])[:5]:
            trade_note = f" | trade P&L {money(trade_pnl)}" if trade_pnl else ""
            qty_note = f" | qty Δ {float(qty_delta):+.4f}" if qty_delta else ""
            print(f"{sym}: {money(d)} ({pct(p)}) | {money(sv)} → {money(ev)}{trade_note}{qty_note}")
        print()
        print("📌 CURRENT POSITIONS")
        for sym, val, c, pnl, pnl_pct in current_rows:
            print(f"{sym}: value {money(val)} | cost {money(c)} | P&L {money(pnl)} ({pct(pnl_pct)})")
        print()
        if week_actions:
            print("🔀 CORPORATE ACTIONS THIS WEEK (every figure above is in post-action units)")
            for a in week_actions:
                print(f"{a.symbol}: {a.kind} ×{a.ratio.normalize():f} on {a.ex_date}")
            print()
        print("🧾 TRADES THIS WEEK")
        if trades:
            for t in trades:
                fees = D(t.get("fees"))
                note = f" — {t.get('notes')}" if t.get("notes") else ""
                print(f"{t['trade_date']} {t['account']} {t['side']} {t['symbol']} {float(t['quantity']):.4f} @ ${float(t['price']):.2f} fees {money(fees)}{note}")
        else:
            print("No trades recorded this week.")
        print()
        _print_fundamental_context(conn, current_rows, week_start_date, end_date)
        if start_missing or end_missing:
            print()
            print(f"⚠️ Missing price data: start={start_missing}, end={end_missing}")


def _print_fundamental_context(conn, current_rows, week_start_date, end_date) -> None:
    """Per-symbol enrichment block, reading fd_store. No API calls."""
    held_equities = [
        sym for sym, *_ in current_rows
        if sym not in fd_store.ETF_SYMBOLS
    ]
    if not held_equities:
        return

    print("🧠 FUNDAMENTAL CONTEXT")
    missing: list[str] = []
    for sym in held_equities:
        facts = fd_store.latest_facts(conn, sym)
        metrics = fd_store.latest_metrics(conn, sym)
        income = fd_store.recent_financials(conn, sym, "income_statement", limit=1)
        cashflow = fd_store.recent_financials(conn, sym, "cash_flow_statement", limit=1)
        earnings = fd_store.recent_earnings(conn, sym, limit=1)
        filings = fd_store.recent_filings(conn, sym, limit=20)
        news = fd_store.recent_news(conn, sym, limit=10)

        if not (facts or metrics or income or earnings or filings or news):
            missing.append(sym)
            continue

        sector = (facts or {}).get("sector") or "—"
        print(f"{sym} ({sector})")

        if metrics:
            parts = [
                f"P/E {fd_ratio(metrics.get('pe_ratio'))}",
                f"EV/EBITDA {fd_ratio(metrics.get('ev_ebitda'))}",
                f"ROE {fd_pct(metrics.get('return_on_equity'))}",
                f"rev growth {fd_pct(metrics.get('revenue_growth'), signed=True)}",
                f"op margin {fd_pct(metrics.get('operating_margin'))}",
            ]
            print(f"  {' | '.join(parts)}")

        if income:
            inc = income[0]
            cf = (cashflow or [{}])[0]
            line = (
                f"  Latest {inc.get('fiscal_period') or inc.get('report_period')}: "
                f"rev {compact_money(inc.get('revenue'))} | "
                f"NI {compact_money(inc.get('net_income'))} | "
                f"FCF {compact_money(cf.get('free_cash_flow'))}"
            )
            print(line)

        if earnings:
            e = earnings[0]
            actual = e.get("eps_actual")
            estimate = e.get("eps_estimate")
            surprise = e.get("eps_surprise") or "—"
            eps_line = (
                f"  Earnings {e.get('fiscal_period') or e.get('report_period')}: "
                f"EPS {fd_ratio(actual)} vs est {fd_ratio(estimate)} ({surprise})"
            )
            rev_surprise = e.get("revenue_surprise")
            if rev_surprise:
                eps_line += f" | revenue {rev_surprise}"
            print(eps_line)

        week_filings = [
            f for f in filings
            if f.get("filing_date") and week_start_date <= f["filing_date"] <= end_date
        ]
        if week_filings:
            types = ", ".join(sorted({f.get("filing_type") or "?" for f in week_filings}))
            print(f"  Filings this week: {len(week_filings)} ({types})")

        # Most recent 2 news items, prefer those from this week.
        from datetime import datetime as _dt, time as _time
        week_start_dt = _dt.combine(week_start_date, _time.min)
        week_news = [
            n for n in news
            if n.get("published_at") and n["published_at"].replace(tzinfo=None) >= week_start_dt
        ]
        show_news = (week_news or news)[:2]
        for n in show_news:
            pub = n.get("published_at")
            when = pub.strftime("%Y-%m-%d") if pub else "—"
            title = (n.get("title") or "").strip()
            if len(title) > 110:
                title = title[:107] + "…"
            source = f" [{n['source']}]" if n.get("source") else ""
            print(f"  📰 {when}{source} {title}")

    if missing:
        print()
        print(f"⚠️ No FD data on file for: {', '.join(missing)}")
        print(f"   Run: python app/fd_weekly_enrichment.py --symbols {','.join(missing)} --force-refresh")


if __name__ == "__main__":
    main()
