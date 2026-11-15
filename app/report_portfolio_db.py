"""Generate portfolio reports from PortfolioDB (Postgres).

Modes:
- daily: intraday / daily briefing (uses latest snapshot)
- eod: end-of-day briefing (uses snapshot <= 23:05 reporting-local)

Outputs plain text to stdout.

Usage:
  set PORTFOLIODB_PASSWORD=...
  python report_portfolio_db.py --mode daily
  python report_portfolio_db.py --mode eod
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd

import ledger_inputs
import portfolio_kpis as kpis
from db import connect, fetch_all, load_config
from ledger_core import to_decimal as _dec  # the one Decimal helper (audit R-4)
from portfolio import compute_fifo_merged, compute_avg_cost_merged
from reporting_utils import local_tz, money as fmt_money, utf8_stdout

UTC = timezone.utc


@dataclass
class Snapshot:
    ts: datetime
    prices: dict[str, dict]


# Below this many priced symbols the daily briefing treats the snapshot as
# incomplete and tries to collect fresh prices before reporting.
MIN_PRICED_SYMBOLS = 10

# Where the collector lives, relative to this file — never an absolute path.
SNAPSHOT_SCRIPT = Path(__file__).resolve().parent / "snapshot_prices.py"


def collect_fresh_prices() -> None:
    """Best-effort price collection before a daily briefing.

    This was a subprocess call to a hardcoded
    `C:\\Windows\\...\\powershell.exe` running
    `C:\\Install\\PortfolioDB\\run_snapshot.ps1` — an operator-specific path
    pointing at a launcher that is gitignored, so it exists on exactly one
    machine. Everywhere else (a fresh clone, the container, CI) the call failed
    into a bare `except: pass` and the briefing carried on with whatever stale
    snapshot it had, saying nothing.

    The wrapper was redundant besides. It re-implemented two things the Python
    now does for itself: reading PORTFOLIODB_PASSWORD out of .env, which
    db.load_config handles, and a weekday/hours guard, which is
    app/market_window.py's job.

    So: run the collector directly with the interpreter already in hand.
    Deliberately without --ignore-window — the collector refuses outside the
    configured window, and that refusal is meant to be one rule every caller
    obeys, including this one. A briefing run at midnight should report stale
    prices, not manufacture a snapshot.

    Failures are reported rather than swallowed. The briefing still runs: a
    missing refresh is a degraded report, not a reason to produce none.
    """
    # Both elements of the command are fixed by this module, not by anything a
    # caller supplies: sys.executable is the interpreter already running, and
    # SNAPSHOT_SCRIPT is derived from __file__. No shell (shell=False is the
    # default), so nothing is word-split or glob-expanded either. The audit
    # rules below fire on any subprocess call whose argv is not a literal
    # string, which this deliberately is not — a literal would reintroduce the
    # hardcoded path this change exists to remove.
    try:
        proc = subprocess.run(  # nosec B603  # nosemgrep
            [sys.executable, str(SNAPSHOT_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else f"exit {proc.returncode}"
            print(f"⚠️ Price collection did not run ({tail}). Using the existing snapshot.")
    except Exception as exc:
        print(f"⚠️ Price collection could not be started ({exc}). Using the existing snapshot.")


def unrealized_pct(pnl, cost):
    """Unrealized P&L as a percent of cost basis; NaN where there is no cost.

    A fully-closed position has open_cost 0, and "inf%" is not a return — this
    report was printing exactly that for every closed symbol on the runs where
    it did not crash outright.

    The to_numeric calls carry as much weight as the zero guard. `price_of`
    yields None for any symbol missing from the snapshot, and when *no* symbol
    has a price the derived columns become object dtype. Pandas then divides
    element-wise in Python, where x / 0.0 raises ZeroDivisionError rather than
    returning inf the way numpy does — which is how a cosmetic inf became a
    crash. Coercing to numeric first keeps the columns float64 in every case.
    """
    pnl = pd.to_numeric(pnl, errors="coerce")
    cost = pd.to_numeric(cost, errors="coerce")
    return (pnl / cost.where(cost != 0)) * 100


# The newest row in price_snapshots is not necessarily a portfolio snapshot.
# Benchmarks for the Markets strip (index futures, added in 1.1.0) are collected
# around the clock on their own cadence, while the held symbols stop after the
# US close — so for most of the day MAX(ts) names a benchmark-only instant at
# which nothing in the portfolio has a price. Excluding benchmarks was not
# enough either: a watchlist symbol, or one sold last month that is still
# collected, can be quoted after the holdings stopped, and its instant made the
# report's day a Sunday, labelled every holding stale and printed a $0.00 delta
# (QA of PR #123, B1). The instant is now the executive report's rule,
# portfolio_kpis.portfolio_as_of: the newest quote of a HELD symbol, with the
# newest non-benchmark quote only when nothing is held. A zero quote (Yahoo's
# placeholder outside regular hours) is not a price and never decides it.
_LATEST_PER_SYMBOL = """
    SELECT ps.symbol, MAX(ps.ts) AS ts, i.benchmark
    FROM price_snapshots ps
    JOIN instruments i ON i.symbol = ps.symbol
    WHERE ps.last_price > 0{cutoff}
    GROUP BY ps.symbol, i.benchmark
"""

_FIRST_TS_FROM = """
    SELECT ps.symbol, MIN(ps.ts) AS ts, i.benchmark
    FROM price_snapshots ps
    JOIN instruments i ON i.symbol = ps.symbol
    WHERE ps.last_price > 0 AND ps.ts >= %s
    GROUP BY ps.symbol, i.benchmark
"""


def held_symbols(conn) -> set[str]:
    """Symbols with an open position, by the engines' rules (prepared ledger)."""
    fifo = compute_fifo_merged(ledger_inputs.load(conn).lots)
    return set(fifo[fifo["qty"] > 0]["symbol"]) if not fifo.empty else set()


def _choose(rows, held: set[str], pick) -> object | None:
    """Apply portfolio_kpis' held-symbol rule to per-symbol timestamps."""
    stamps = {r["symbol"]: r["ts"] for r in rows if r.get("ts") is not None}
    held_stamps = [ts for s, ts in stamps.items() if s in held]
    if held_stamps:
        return pick(held_stamps)
    bench = {r["symbol"] for r in rows if r.get("benchmark")}
    rest = [ts for s, ts in stamps.items() if s not in bench]
    return pick(rest) if rest else None


def get_snapshot(conn, *, mode: str, held: set[str] | None = None) -> Snapshot:
    """The snapshot the report is read at: when the holdings were last priced.

    ``held`` defaults to the ledger's open positions; main() passes the set it
    already computed.
    """
    if held is None:
        held = held_symbols(conn)
    if mode == "daily":
        rows = fetch_all(conn, _LATEST_PER_SYMBOL.format(cutoff=""))
        ts = kpis.portfolio_as_of({r["symbol"]: r["ts"] for r in rows}, held,
                                  {r["symbol"] for r in rows if r.get("benchmark")})
        if ts is None:
            raise RuntimeError("No price snapshots found")
    elif mode == "eod":
        # Snapshot <= today 23:05 IL time
        tz = local_tz()
        now_il = datetime.now(tz)
        target_il = datetime.combine(now_il.date(), time(23, 5)).replace(tzinfo=tz)
        target_utc = target_il.astimezone(UTC)
        rows = fetch_all(conn, _LATEST_PER_SYMBOL.format(cutoff=" AND ps.ts <= %s"), (target_utc,))
        ts = kpis.portfolio_as_of({r["symbol"]: r["ts"] for r in rows}, held,
                                  {r["symbol"] for r in rows if r.get("benchmark")})
        if ts is None:
            raise RuntimeError(f"No price snapshots found at or before {target_il.isoformat()}")
    else:
        raise ValueError("mode must be daily|eod")

    rows = fetch_all(
        conn,
        """
        SELECT symbol, ts, last_price, bid, ask, source
        FROM price_snapshots
        WHERE ts=%s AND last_price > 0
        """,
        (ts,),
    )
    prices = {r["symbol"]: r for r in rows}
    return Snapshot(ts=ts, prices=prices)


def get_prev_snapshot_map(conn, ts: datetime) -> dict[str, tuple[datetime, float]]:
    """Previous snapshot per symbol (2nd newest at/before ts), with its own ts.

    The timestamp travels with the price so main() can restate the quote into
    today's split units before comparing it with the current one — without
    that, a split between the two quotes read as a price move of the whole
    ratio (re-audit F03, case B).
    """
    rows = fetch_all(
        conn,
        """
        WITH ranked AS (
          SELECT symbol, ts, last_price,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
          FROM price_snapshots
          WHERE ts <= %s AND last_price > 0
        )
        SELECT symbol, ts, last_price
        FROM ranked
        WHERE rn = 2;
        """,
        (ts,),
    )
    return {r["symbol"]: (r["ts"], float(r["last_price"])) for r in rows if r.get("last_price") is not None}


def get_day_start_snapshot_map(conn, *, ts: datetime, start_hhmm: tuple[int, int] = (16, 15),
                               held: set[str] | None = None) -> tuple[datetime | None, dict[str, float]]:
    """Return the first snapshot of the IL trading day window and its prices.

    We define "day start" as the earliest snapshot on the same IL calendar day
    with ts >= start_hhmm (default 16:15 IL) that priced a HELD symbol (the
    same rule as the report instant; with nothing held, a non-benchmark one).
    A benchmark-, watchlist- or sold-symbol-only instant quotes no holding, and
    taking it as the baseline left the day's delta empty or measured from the
    wrong run (audit F-8; QA of PR #123, B1).

    Returns (start_ts, {symbol: last_price}). If none found, (None, {}).
    """
    tz = local_tz()
    ts_il = ts.astimezone(tz)
    day = ts_il.date()
    start_il = datetime.combine(day, time(start_hhmm[0], start_hhmm[1])).replace(tzinfo=tz)
    start_utc = start_il.astimezone(UTC)

    if held is None:
        held = held_symbols(conn)
    start_ts = _choose(fetch_all(conn, _FIRST_TS_FROM, (start_utc,)), held, min)
    if start_ts is None:
        return None, {}

    rows = fetch_all(
        conn,
        """
        SELECT symbol, last_price
        FROM price_snapshots
        WHERE ts=%s AND last_price > 0
        """,
        (start_ts,),
    )
    return start_ts, {r["symbol"]: float(r["last_price"]) for r in rows if r.get("last_price") is not None}


def _compute_fifo_merged(lot_rows) -> pd.DataFrame:
    return compute_fifo_merged(lot_rows)


def _compute_avg_cost_merged(lot_rows) -> pd.DataFrame:
    return compute_avg_cost_merged(lot_rows)


def _section(title: str) -> list[str]:
    return [title]


def _fmt_line(label: str, value: str) -> str:
    # Align-ish for monospace Telegram
    return f"{label:<18} {value}"


def get_quotes_at_or_before(conn, ts: datetime) -> dict[str, dict]:
    """Each symbol's latest quote at or before ``ts`` — the price it is valued at.

    The daily report used to value only the symbols present in the one
    snapshot it picked, so a holding that missed that collector run vanished
    from the value without a word, while the executive report valued it at its
    last quote (audit F-8). Both now follow one rule (portfolio_kpis): latest
    quote at or before the report instant, and a quote from an earlier day is
    listed as stale.
    """
    rows = fetch_all(
        conn,
        """
        SELECT DISTINCT ON (symbol) symbol, ts, last_price, bid, ask, source
        FROM price_snapshots
        WHERE ts <= %s AND last_price > 0
        ORDER BY symbol, ts DESC
        """,
        (ts,),
    )
    return {r["symbol"]: r for r in rows}


def main():
    utf8_stdout()

    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "eod"], required=True)
    args = ap.parse_args()

    cfg = load_config()
    tz = local_tz()

    # closing(): psycopg2's own `with conn` leaves the connection open (db.py);
    # this report only reads, so there is nothing to commit (audit R-6).
    with closing(connect(cfg)) as conn:
        # The holdings decide the report instant and the day-start baseline.
        held_now = held_symbols(conn)
        # Guard: if too few symbols are priced, try to collect fresh ones first.
        snap = get_snapshot(conn, mode=args.mode, held=held_now)
        if args.mode == "daily" and len(snap.prices) < MIN_PRICED_SYMBOLS:
            collect_fresh_prices()
            snap = get_snapshot(conn, mode=args.mode, held=held_now)

        prev_raw = get_prev_snapshot_map(conn, snap.ts)
        day_start_ts, day_start_raw = get_day_start_snapshot_map(conn, ts=snap.ts, held=held_now)
        # The snapshot's own rows win over an older quote of the same symbol.
        quotes = {**get_quotes_at_or_before(conn, snap.ts), **snap.prices}

        # Restated through the prepared ledger, like every other reader, so a
        # recorded split shows up here as a share count and not as a loss —
        # and the comparison quotes are restated with the same actions, so a
        # split between the previous quote and the current one is not a
        # delta of the whole ratio (re-audit F03, case B). Each quote keeps
        # its own timestamp for that; the ledger decides which actions apply.
        ledger = ledger_inputs.load(conn)
        lot_rows = ledger.lots
        snap = Snapshot(ts=snap.ts, prices={
            r["symbol"]: r for r in ledger.price_rows(list(snap.prices.values()))
        })
        quotes = {r["symbol"]: r for r in ledger.price_rows(list(quotes.values()))}
        prev_map = {
            sym: price for _ts, sym, price in ledger.price_points(
                [(ts, sym, price) for sym, (ts, price) in prev_raw.items()]
            )
        }
        day_start_map = {
            sym: price for _ts, sym, price in ledger.price_points(
                [(day_start_ts, sym, price) for sym, price in day_start_raw.items()]
            )
        } if day_start_ts is not None else {}

        fifo_all = _compute_fifo_merged(lot_rows)
        avg_all = _compute_avg_cost_merged(lot_rows)

        # Fees paid (already in basis/proceeds — reporting only) + income.
        total_fees = sum(float(r["fees"]) for r in lot_rows if r.get("fees") is not None)
        income_rows = fetch_all(conn, "SELECT symbol, pay_date, amount, currency FROM income")
        instruments = fetch_all(conn, "SELECT symbol, currency FROM instruments")
        cash_rows = fetch_all(
            conn,
            "SELECT DISTINCT ON (account) account, cash, ts, currency "
            "FROM cash_snapshots ORDER BY account, ts DESC",
        )
        # Income in another currency is not dollars (audit Q-8).
        usd_income, _foreign = kpis.split_by_currency(income_rows)
        total_income = sum(float(r["amount"]) for r in usd_income if r.get("amount") is not None)
        ttm_cut = snap.ts.astimezone(tz).date() - timedelta(days=365)
        ttm_income = sum(
            float(r["amount"]) for r in usd_income
            if r.get("amount") is not None and r.get("pay_date") and r["pay_date"] >= ttm_cut
        )

        def price_of(sym: str):
            r = quotes.get(sym)
            return float(r["last_price"]) if r and r.get("last_price") is not None else None

        def priced(frame: pd.DataFrame) -> pd.DataFrame:
            out = frame.copy()
            out["last_price"] = out["symbol"].map(price_of)
            out["market_value"] = out["qty"] * out["last_price"]
            out["unrealized_pnl"] = out["market_value"] - out["open_cost"]
            out["unrealized_pct"] = unrealized_pct(out["unrealized_pnl"], out["open_cost"])
            return out

        fifo_full = priced(fifo_all)
        avg_full = priced(avg_all)

        # The roll-up every surface uses (portfolio_kpis): value and P&L over
        # priced rows, the cost of an unpriced holding reported, not lost.
        def rollup(frame: pd.DataFrame) -> dict:
            return kpis.rollup(
                {"symbol": r["symbol"], "qty": r["qty"], "open_cost": r["open_cost"],
                 "realized_pnl": r["realized_pnl"],
                 "last_price": r["last_price"] if pd.notna(r["last_price"]) else None}
                for _, r in frame.iterrows()
            )

        r_fifo = rollup(fifo_full)
        r_avg = rollup(avg_full)
        realized_fifo = float(r_fifo["realized_pnl"])
        realized_avg = float(r_avg["realized_pnl"])

        # Only rows with prices contribute to market value/unrealized
        fifo = fifo_full.dropna(subset=["last_price"]).copy()

        total_value = float(r_fifo["market_value"])
        total_cost_fifo = float(r_fifo["priced_cost_basis"])
        total_cost_avg = float(r_avg["priced_cost_basis"])
        unrl_fifo = float(r_fifo["unrealized_pnl"])
        unrl_avg = float(r_avg["unrealized_pnl"])
        unrl_pct_fifo = kpis.as_float(r_fifo["unrealized_pct"]) or 0.0
        unrl_pct_avg = kpis.as_float(r_avg["unrealized_pct"]) or 0.0
        active = int((fifo["qty"] > 0).sum())

        held = fifo_full[fifo_full["qty"] > 0]["symbol"].tolist()
        stale = kpis.stale_quotes({s: q.get("ts") for s, q in quotes.items()}, held, snap.ts, tz)
        stale_syms = {e["symbol"] for e in stale}
        warnings = kpis.currency_warnings(
            instruments=instruments,
            traded_symbols={r["symbol"] for r in lot_rows},
            held_symbols=held,
            income=income_rows,
            cash=cash_rows,
        )

        def compute_deltas(price_map: dict[str, float], only: set[str] | None = None) -> tuple[float, list[tuple[str, float]]]:
            deltas: list[tuple[str, float]] = []
            total = 0.0
            for _, r in fifo.iterrows():
                sym = r["symbol"]
                if only is not None and sym not in only:
                    continue
                qty = float(r["qty"])
                lastp = float(r["last_price"])
                basep = price_map.get(sym)
                if basep is None:
                    continue
                d = qty * (lastp - basep)
                deltas.append((sym, d))
                total += d
            deltas_sorted = sorted(deltas, key=lambda x: x[1], reverse=True)
            return total, deltas_sorted

        # Delta vs prev snapshot (10-minute tape): only symbols quoted in THIS
        # snapshot — a carried-forward quote did not move since the last one.
        tape_delta, tape_sorted = compute_deltas(prev_map, set(snap.prices))

        # Delta vs start-of-day window (16:15 IL); a stale symbol has no move
        # today that anyone observed.
        fresh = {s for s in held if s not in stale_syms}
        sod_delta, sod_sorted = compute_deltas(day_start_map, fresh) if day_start_map else (0.0, [])

        tape_top = tape_sorted[:5]
        tape_bot = list(reversed(tape_sorted[-5:]))
        sod_top = sod_sorted[:5]
        sod_bot = list(reversed(sod_sorted[-5:]))

        ts_il = snap.ts.astimezone(tz)

        title = "📊 DAILY PORTFOLIO BRIEFING (DB)" if args.mode == "daily" else "🌙 END-OF-DAY PORTFOLIO REPORT (DB)"

        lines: list[str] = []
        lines += _section(title)
        lines.append(f"🕒 Data: {ts_il.strftime('%Y-%m-%d %H:%M')} IL | Source: PortfolioDB")
        lines.append("")

        lines.append("💰 Summary")
        lines.append(_fmt_line("Portfolio Value:", fmt_money(total_value)))
        lines.append(_fmt_line("Active Positions:", str(active)))
        if r_fifo["unpriced_symbols"]:
            lines.append(_fmt_line(
                "Unpriced:",
                f"{', '.join(r_fifo['unpriced_symbols'])} — cost {fmt_money(r_fifo['unpriced_cost_basis'])}, "
                "no quote on file; not in value or P&L",
            ))
        if stale:
            lines.append(_fmt_line(
                "Stale prices:",
                ", ".join(f"{e['symbol']} ({e['ts'].astimezone(tz).strftime('%Y-%m-%d')})" for e in stale)
                + " — valued at last quote; not in the day's deltas",
            ))
        lines.append("")

        lines.append("🧾 P&L (FIFO vs AVG)")
        lines.append(_fmt_line("Cost Basis FIFO:", fmt_money(total_cost_fifo)))
        lines.append(_fmt_line("Unrealized FIFO:", f"{fmt_money(unrl_fifo)} ({unrl_pct_fifo:.2f}%)"))
        lines.append(_fmt_line("Realized FIFO:", fmt_money(realized_fifo)))
        lines.append("")
        lines.append(_fmt_line("Cost Basis AVG:", fmt_money(total_cost_avg)))
        lines.append(_fmt_line("Unrealized AVG:", f"{fmt_money(unrl_avg)} ({unrl_pct_avg:.2f}%)"))
        lines.append(_fmt_line("Realized AVG:", fmt_money(realized_avg)))
        lines.append("")

        lines.append("💵 Income & Costs")
        lines.append(_fmt_line("Dividends (all):", fmt_money(total_income)))
        lines.append(_fmt_line("Dividends (TTM):", fmt_money(ttm_income)))
        lines.append(_fmt_line("Total Fees Paid:", fmt_money(total_fees)))
        lines.append("")

        # Movers: tape
        lines.append("⏱️ Since last snapshot")
        lines.append(_fmt_line("Delta:", fmt_money(tape_delta)))
        if tape_top:
            lines.append("🟢 Top:")
            for sym, d in tape_top:
                lines.append(f"  {sym:<5} {fmt_money(d)}")
        if tape_bot:
            lines.append("🔴 Bottom:")
            for sym, d in tape_bot:
                lines.append(f"  {sym:<5} {fmt_money(d)}")
        lines.append("")

        # Movers: since day start
        if day_start_ts is not None and sod_sorted:
            sod_il = day_start_ts.astimezone(tz)
            lines.append(f"📅 Since day start (from {sod_il.strftime('%H:%M')} IL)")
            lines.append(_fmt_line("Delta:", fmt_money(sod_delta)))
            if sod_top:
                lines.append("🟢 Top contributors:")
                for sym, d in sod_top:
                    lines.append(f"  {sym:<5} {fmt_money(d)}")
            if sod_bot:
                lines.append("🔴 Bottom contributors:")
                for sym, d in sod_bot:
                    lines.append(f"  {sym:<5} {fmt_money(d)}")
        else:
            lines.append("📅 Since day start: N/A (no baseline snapshot found for 16:15 IL window)")

        if warnings:
            lines.append("")
            lines.append("💱 Currency")
            for w in warnings:
                lines.append(f"  ⚠️ {w['message']}")

        print("\n".join(lines))


if __name__ == "__main__":
    main()
