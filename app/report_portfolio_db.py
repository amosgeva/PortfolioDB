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
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal

import pandas as pd

from db import connect, fetch_all, load_config
from portfolio import compute_fifo_merged, compute_avg_cost_merged
from reporting_utils import IL_TZ, money as fmt_money, utf8_stdout

UTC = timezone.utc


def _dec(x) -> Decimal:
    return Decimal(str(x))


@dataclass
class Snapshot:
    ts: datetime
    prices: dict[str, dict]


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
# which nothing in the portfolio has a price. Joining instruments and excluding
# benchmarks asks the question this report actually means: when were the
# *holdings* last priced. The FK on price_snapshots.symbol guarantees the join
# never drops a row.
_LATEST_PORTFOLIO_TS = """
    SELECT MAX(ps.ts) AS ts
    FROM price_snapshots ps
    JOIN instruments i ON i.symbol = ps.symbol
    WHERE NOT i.benchmark
"""


def get_snapshot(conn, *, mode: str) -> Snapshot:
    if mode == "daily":
        # Latest snapshot that priced something the portfolio can hold.
        row = fetch_all(conn, _LATEST_PORTFOLIO_TS)
        ts = row[0]["ts"]
        if ts is None:
            raise RuntimeError("No price snapshots found")
    elif mode == "eod":
        # Snapshot <= today 23:05 IL time
        now_il = datetime.now(IL_TZ)
        target_il = datetime.combine(now_il.date(), time(23, 5)).replace(tzinfo=IL_TZ)
        target_utc = target_il.astimezone(UTC)
        row = fetch_all(conn, _LATEST_PORTFOLIO_TS + " AND ps.ts <= %s", (target_utc,))
        ts = row[0]["ts"]
        if ts is None:
            raise RuntimeError(f"No price snapshots found at or before {target_il.isoformat()}")
    else:
        raise ValueError("mode must be daily|eod")

    rows = fetch_all(
        conn,
        """
        SELECT symbol, ts, last_price, bid, ask, source
        FROM price_snapshots
        WHERE ts=%s
        """,
        (ts,),
    )
    prices = {r["symbol"]: r for r in rows}
    return Snapshot(ts=ts, prices=prices)


def get_prev_snapshot_map(conn, ts: datetime) -> dict[str, float]:
    """Previous snapshot per symbol (2nd newest at/before ts)."""
    rows = fetch_all(
        conn,
        """
        WITH ranked AS (
          SELECT symbol, ts, last_price,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
          FROM price_snapshots
          WHERE ts <= %s
        )
        SELECT symbol, last_price
        FROM ranked
        WHERE rn = 2;
        """,
        (ts,),
    )
    return {r["symbol"]: float(r["last_price"]) for r in rows if r.get("last_price") is not None}


def get_day_start_snapshot_map(conn, *, ts: datetime, start_hhmm: tuple[int, int] = (16, 15)) -> tuple[datetime | None, dict[str, float]]:
    """Return the first snapshot of the IL trading day window and its prices.

    We define "day start" as the earliest snapshot on the same IL calendar day
    with ts >= start_hhmm (default 16:15 IL).

    Returns (start_ts, {symbol: last_price}). If none found, (None, {}).
    """
    ts_il = ts.astimezone(IL_TZ)
    day = ts_il.date()
    start_il = datetime.combine(day, time(start_hhmm[0], start_hhmm[1])).replace(tzinfo=IL_TZ)
    start_utc = start_il.astimezone(UTC)

    row = fetch_all(conn, "SELECT MIN(ts) AS ts FROM price_snapshots WHERE ts >= %s", (start_utc,))
    start_ts = row[0]["ts"] if row else None
    if start_ts is None:
        return None, {}

    rows = fetch_all(
        conn,
        """
        SELECT symbol, last_price
        FROM price_snapshots
        WHERE ts=%s
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


def main():
    utf8_stdout()

    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "eod"], required=True)
    args = ap.parse_args()

    cfg = load_config()

    with connect(cfg) as conn:
        # Guard: if we have too few symbols in the latest snapshot, run a fresh snapshot collection first.
        snap = get_snapshot(conn, mode=args.mode)
        if args.mode == 'daily':
            if len(snap.prices) < 10:
                # Run snapshot collection (best-effort) and reload snapshot
                import subprocess
                try:
                    subprocess.run([
                        'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe',
                        '-NoProfile', '-ExecutionPolicy', 'Bypass',
                        '-File', 'C:\\Install\\PortfolioDB\\run_snapshot.ps1'
                    ], check=False, capture_output=True, text=True)
                except Exception:
                    pass
                snap = get_snapshot(conn, mode=args.mode)

        prev_map = get_prev_snapshot_map(conn, snap.ts)
        day_start_ts, day_start_map = get_day_start_snapshot_map(conn, ts=snap.ts)

        lot_rows = fetch_all(
            conn,
            """
            SELECT id, symbol, account, side, trade_date, quantity, price, fees
            FROM lots
            ORDER BY symbol, COALESCE(account,''), trade_date, id
            """,
        )

        fifo_all = _compute_fifo_merged(lot_rows)
        avg_all = _compute_avg_cost_merged(lot_rows)

        # Realized P&L must include closed symbols even if we don't have a current price snapshot for them.
        realized_fifo = float(fifo_all["realized_pnl"].sum()) if not fifo_all.empty else 0.0
        realized_avg = float(avg_all["realized_pnl"].sum()) if not avg_all.empty else 0.0

        # Fees paid (already in basis/proceeds — reporting only) + income.
        from datetime import timedelta
        total_fees = sum(float(r["fees"]) for r in lot_rows if r.get("fees") is not None)
        income_rows = fetch_all(conn, "SELECT pay_date, amount FROM income")
        total_income = sum(float(r["amount"]) for r in income_rows if r.get("amount") is not None)
        ttm_cut = snap.ts.astimezone(IL_TZ).date() - timedelta(days=365)
        ttm_income = sum(
            float(r["amount"]) for r in income_rows
            if r.get("amount") is not None and r.get("pay_date") and r["pay_date"] >= ttm_cut
        )

        # Attach prices (for market value / unrealized only)
        def price_of(sym: str):
            r = snap.prices.get(sym)
            return float(r["last_price"]) if r and r.get("last_price") is not None else None

        fifo = fifo_all.copy()
        fifo["last_price"] = fifo["symbol"].map(price_of)
        fifo["market_value"] = fifo["qty"] * fifo["last_price"]
        fifo["unrealized_pnl"] = fifo["market_value"] - fifo["open_cost"]
        fifo["unrealized_pct"] = unrealized_pct(fifo["unrealized_pnl"], fifo["open_cost"])

        avg = avg_all.copy()
        avg["last_price"] = avg["symbol"].map(price_of)
        avg["market_value"] = avg["qty"] * avg["last_price"]
        avg["unrealized_pnl"] = avg["market_value"] - avg["open_cost"]
        avg["unrealized_pct"] = unrealized_pct(avg["unrealized_pnl"], avg["open_cost"])

        # Only rows with prices contribute to market value/unrealized
        fifo = fifo.dropna(subset=["last_price"]).copy()
        avg = avg.dropna(subset=["last_price"]).copy()

        total_value = float(fifo["market_value"].sum())
        total_cost_fifo = float(fifo["open_cost"].sum())
        total_cost_avg = float(avg["open_cost"].sum())
        unrl_fifo = float(fifo["unrealized_pnl"].sum())
        unrl_avg = float(avg["unrealized_pnl"].sum())
        unrl_pct_fifo = (unrl_fifo / total_cost_fifo * 100) if total_cost_fifo else 0.0
        unrl_pct_avg = (unrl_avg / total_cost_avg * 100) if total_cost_avg else 0.0
        active = int((fifo["qty"] > 0).sum())

        def compute_deltas(price_map: dict[str, float]) -> tuple[float, list[tuple[str, float]]]:
            deltas: list[tuple[str, float]] = []
            total = 0.0
            for _, r in fifo.iterrows():
                sym = r["symbol"]
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

        # Delta vs prev snapshot (10-minute tape)
        tape_delta, tape_sorted = compute_deltas(prev_map)

        # Delta vs start-of-day window (16:15 IL)
        sod_delta, sod_sorted = compute_deltas(day_start_map) if day_start_map else (0.0, [])

        tape_top = tape_sorted[:5]
        tape_bot = list(reversed(tape_sorted[-5:]))
        sod_top = sod_sorted[:5]
        sod_bot = list(reversed(sod_sorted[-5:]))

        ts_il = snap.ts.astimezone(IL_TZ)

        title = "📊 DAILY PORTFOLIO BRIEFING (DB)" if args.mode == "daily" else "🌙 END-OF-DAY PORTFOLIO REPORT (DB)"

        lines: list[str] = []
        lines += _section(title)
        lines.append(f"🕒 Data: {ts_il.strftime('%Y-%m-%d %H:%M')} IL | Source: PortfolioDB")
        lines.append("")

        lines.append("💰 Summary")
        lines.append(_fmt_line("Portfolio Value:", fmt_money(total_value)))
        lines.append(_fmt_line("Active Positions:", str(active)))
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
            sod_il = day_start_ts.astimezone(IL_TZ)
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

        print("\n".join(lines))


if __name__ == "__main__":
    main()
