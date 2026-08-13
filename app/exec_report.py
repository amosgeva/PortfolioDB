"""Executive HTML report generator for PortfolioDB.

Builds a self-contained HTML document with KPIs, holdings, allocation,
equity curve, top movers, realized P&L log, and cash positions.

Entry point: build_html(conn) -> str
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
from plotly.offline import get_plotlyjs

import reporting_tz
from db import fetch_all
from fifo import Lot, run_fifo
from portfolio import compute_fifo_merged, to_decimal


TZ_NAME = reporting_tz.tz_name()
JER = ZoneInfo(TZ_NAME)


# ─── Data gathering ──────────────────────────────────────────


@dataclass
class ReportData:
    as_of: datetime
    positions: pd.DataFrame
    totals: dict
    cash_by_account: dict[str, float]
    account_allocation: pd.DataFrame
    equity_curve: pd.DataFrame
    movers: dict = field(default_factory=dict)
    realized_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    advisor_brief: dict | None = None
    fd_metrics: dict[str, dict] = field(default_factory=dict)
    earnings_past: list[dict] = field(default_factory=list)
    earnings_upcoming: list[dict] = field(default_factory=list)


def _fetch_lots(conn) -> list[dict]:
    return fetch_all(
        conn,
        """
        SELECT id, symbol, account, side, trade_date, quantity, price, fees
        FROM lots
        ORDER BY symbol, COALESCE(account,''), trade_date, id
        """,
    )


def _fetch_latest_prices(conn) -> dict[str, dict]:
    rows = fetch_all(
        conn,
        """
        SELECT ps.symbol, ps.ts, ps.last_price
        FROM price_snapshots ps
        JOIN (
          SELECT symbol, MAX(ts) AS ts
          FROM price_snapshots
          GROUP BY symbol
        ) m ON m.symbol = ps.symbol AND m.ts = ps.ts
        """,
    )
    return {
        r["symbol"]: {
            "ts": r["ts"],
            "last_price": float(r["last_price"]) if r["last_price"] is not None else None,
        }
        for r in rows
    }


def _fetch_eod_by_day(conn) -> dict[date, dict[str, float]]:
    rows = fetch_all(
        conn,
        """
        WITH daily AS (
          SELECT symbol,
                 (ts AT TIME ZONE %s)::date AS d,
                 last_price,
                 ROW_NUMBER() OVER (
                   PARTITION BY symbol, (ts AT TIME ZONE %s)::date
                   ORDER BY ts DESC
                 ) AS rn
          FROM price_snapshots
        )
        SELECT symbol, d, last_price
        FROM daily
        WHERE rn = 1
        ORDER BY d, symbol
        """,
        (TZ_NAME, TZ_NAME),
    )
    by_day: dict[date, dict[str, float]] = defaultdict(dict)
    for r in rows:
        if r["last_price"] is not None:
            by_day[r["d"]][r["symbol"]] = float(r["last_price"])
    return dict(by_day)


def _fetch_latest_brief(conn) -> dict | None:
    rows = fetch_all(
        conn,
        """
        SELECT id, ts, kind, total_value, payload
        FROM advisor_briefs
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not rows:
        return None
    r = rows[0]
    payload = r["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    r["payload"] = payload or {}
    return r


def _fetch_fd_metrics(conn) -> dict[str, dict]:
    rows = fetch_all(
        conn,
        """
        SELECT symbol, pe_ratio, revenue_growth, market_cap, net_margin,
               earnings_growth, fetched_at
        FROM fd_financial_metrics
        """,
    )
    return {r["symbol"]: r for r in rows}


def _fetch_earnings_window(conn, symbols: list[str], past_days: int = 60, upcoming_days: int = 45) -> tuple[list[dict], list[dict]]:
    """Past actual earnings + computed next-expected (filing cadence ~90d)."""
    if not symbols:
        return [], []

    today = date.today()
    past_cutoff = today - timedelta(days=past_days)

    past = fetch_all(
        conn,
        """
        SELECT DISTINCT ON (symbol, report_period)
               symbol, report_period, filing_date, fiscal_period,
               eps_actual, eps_estimate, eps_surprise, eps_surprise_pct,
               revenue_actual, revenue_estimate, revenue_surprise, revenue_surprise_pct
        FROM fd_earnings
        WHERE symbol = ANY(%s)
          AND filing_date IS NOT NULL
          AND filing_date >= %s
        ORDER BY symbol, report_period DESC, filing_date DESC
        """,
        (symbols, past_cutoff),
    )
    past.sort(key=lambda r: r["filing_date"] or date.min, reverse=True)

    # For upcoming: take latest filing per symbol and project +90d.
    latest_per = fetch_all(
        conn,
        """
        SELECT DISTINCT ON (symbol) symbol, report_period, filing_date, fiscal_period
        FROM fd_earnings
        WHERE symbol = ANY(%s) AND filing_date IS NOT NULL
        ORDER BY symbol, filing_date DESC, report_period DESC
        """,
        (symbols,),
    )

    upcoming_cutoff = today + timedelta(days=upcoming_days)
    upcoming = []
    for r in latest_per:
        cadence_days = 90 if (r["fiscal_period"] or "").upper().startswith("Q") else 365
        expected = r["filing_date"] + timedelta(days=cadence_days)
        if today - timedelta(days=14) <= expected <= upcoming_cutoff:
            upcoming.append({
                "symbol": r["symbol"],
                "expected": expected,
                "last_filing": r["filing_date"],
                "last_period": r["report_period"],
                "cadence_days": cadence_days,
                "overdue": expected < today,
            })
    upcoming.sort(key=lambda r: r["expected"])
    return past, upcoming


def _fetch_cash(conn) -> dict[str, float]:
    rows = fetch_all(
        conn,
        """
        SELECT DISTINCT ON (account) account, cash
        FROM cash_snapshots
        ORDER BY account, ts DESC
        """,
    )
    return {(r["account"] or "(merged)"): float(r["cash"]) for r in rows}


def _holdings_at_date(lot_rows: list[dict], as_of: date) -> dict[str, float]:
    qty: dict[str, float] = defaultdict(float)
    for r in lot_rows:
        if r["trade_date"] > as_of:
            continue
        sign = 1 if r["side"].upper() == "BUY" else -1
        qty[r["symbol"]] += sign * float(r["quantity"])
    return {s: q for s, q in qty.items() if q > 1e-9}


def _build_equity_curve(lot_rows, eod_by_day) -> pd.DataFrame:
    """Daily holdings market value, carrying forward last-known prices."""
    if not eod_by_day or not lot_rows:
        return pd.DataFrame(columns=["date", "value"])

    sorted_days = sorted(eod_by_day.keys())
    last_known: dict[str, float] = {}
    pts = []
    for d in sorted_days:
        last_known.update(eod_by_day[d])
        held = _holdings_at_date(lot_rows, d)
        if not held:
            continue
        v = sum(q * last_known[s] for s, q in held.items() if s in last_known)
        if v > 0:
            pts.append({"date": d, "value": v})
    return pd.DataFrame(pts)


def _carry_forward_prices_at(eod_by_day, target_day) -> dict[str, float]:
    """Last-known price per symbol as of end-of-target_day."""
    out: dict[str, float] = {}
    for d in sorted(eod_by_day.keys()):
        if d > target_day:
            break
        out.update(eod_by_day[d])
    return out


def _movers(positions: pd.DataFrame, eod_by_day, today: date) -> dict:
    if positions.empty or not eod_by_day:
        return {}
    sorted_days = sorted(eod_by_day.keys())
    prior_day = next((d for d in reversed(sorted_days) if d < today), None)
    week_day = next((d for d in reversed(sorted_days) if d <= today - timedelta(days=7)), None)

    prior_prices = _carry_forward_prices_at(eod_by_day, prior_day) if prior_day else {}
    week_prices = _carry_forward_prices_at(eod_by_day, week_day) if week_day else {}

    held = positions[positions["qty"] > 0]
    rows_day, rows_week, rows_all = [], [], []
    for _, row in held.iterrows():
        sym = row["symbol"]
        qty = float(row["qty"])
        last = float(row["last_price"]) if pd.notna(row["last_price"]) else None
        if last is None:
            continue
        pp = prior_prices.get(sym)
        if pp:
            rows_day.append({"symbol": sym, "delta": qty * (last - pp), "pct": (last / pp - 1) * 100})
        wp = week_prices.get(sym)
        if wp:
            rows_week.append({"symbol": sym, "delta": qty * (last - wp), "pct": (last / wp - 1) * 100})
        rows_all.append({"symbol": sym, "delta": float(row["unrealized_pnl"]), "pct": float(row["unrealized_pct"])})

    def split(rs):
        s = sorted(rs, key=lambda x: x["delta"], reverse=True)
        return s[:5], list(reversed(s[-5:]))

    d_top, d_bot = split(rows_day)
    w_top, w_bot = split(rows_week)
    a_top, a_bot = split(rows_all)
    return {
        "day_top": d_top, "day_bot": d_bot,
        "week_top": w_top, "week_bot": w_bot,
        "all_top": a_top, "all_bot": a_bot,
    }


def _realized_log(lot_rows: list[dict]) -> pd.DataFrame:
    grouped: dict[tuple[str, str | None], list[Lot]] = defaultdict(list)
    for r in lot_rows:
        grouped[(r["symbol"], r["account"])].append(
            Lot(
                id=int(r["id"]),
                symbol=r["symbol"],
                account=r["account"],
                side=r["side"],
                trade_date=r["trade_date"],
                quantity=to_decimal(r["quantity"]),
                price=to_decimal(r["price"]),
                fees=to_decimal(r["fees"]),
            )
        )
    sell_dates = {int(r["id"]): r["trade_date"] for r in lot_rows if r["side"].upper() == "SELL"}

    out = []
    for _, lots in grouped.items():
        res = run_fifo(lots)
        for m in res.matches:
            out.append({
                "sell_date": sell_dates.get(m.sell_lot_id),
                "symbol": m.symbol,
                "account": m.account or "",
                "qty": float(m.qty),
                "buy_cost_ps": float(m.buy_cost_ps),
                "sell_proceeds_ps": float(m.sell_proceeds_ps),
                "realized_pnl": float(m.realized_pnl),
            })
    if not out:
        return pd.DataFrame(columns=[
            "sell_date", "symbol", "account", "qty",
            "buy_cost_ps", "sell_proceeds_ps", "realized_pnl",
        ])
    return pd.DataFrame(out).sort_values(["sell_date", "symbol"], ascending=[False, True]).reset_index(drop=True)


def gather(conn) -> ReportData:
    lot_rows = _fetch_lots(conn)
    fifo_pos = compute_fifo_merged(lot_rows)
    latest = _fetch_latest_prices(conn)
    cash_by_account = _fetch_cash(conn)
    eod_by_day = _fetch_eod_by_day(conn)

    if fifo_pos.empty:
        positions = pd.DataFrame(columns=[
            "symbol", "qty", "open_cost", "avg_cost", "realized_pnl",
            "last_price", "market_value", "unrealized_pnl", "unrealized_pct", "weight",
        ])
    else:
        positions = fifo_pos.copy()
        positions["last_price"] = positions["symbol"].map(lambda s: latest.get(s, {}).get("last_price"))
        positions["market_value"] = positions["qty"] * positions["last_price"]
        positions["unrealized_pnl"] = positions["market_value"] - positions["open_cost"]
        oc = positions["open_cost"].replace(0, pd.NA)
        positions["unrealized_pct"] = (positions["unrealized_pnl"] / oc * 100).fillna(0.0).astype(float)

    if latest:
        max_ts = max((v["ts"] for v in latest.values() if v.get("ts")), default=None)
        as_of = max_ts.astimezone(JER) if max_ts else datetime.now(JER)
    else:
        as_of = datetime.now(JER)
    today = as_of.date()

    held = positions[positions["qty"] > 0]
    total_value = float(held["market_value"].sum(skipna=True)) if not held.empty else 0.0
    total_cost = float(held["open_cost"].sum()) if not held.empty else 0.0
    total_unrl = float(held["unrealized_pnl"].sum(skipna=True)) if not held.empty else 0.0
    total_unrl_pct = (total_unrl / total_cost * 100) if total_cost else 0.0
    realized = float(positions["realized_pnl"].sum()) if not positions.empty else 0.0
    cash_total = sum(cash_by_account.values())

    if not positions.empty:
        positions["weight"] = (positions["market_value"] / total_value * 100).fillna(0.0) if total_value else 0.0

    prior_day = next((d for d in reversed(sorted(eod_by_day.keys())) if d < today), None)
    week_day = next((d for d in reversed(sorted(eod_by_day.keys())) if d <= today - timedelta(days=7)), None)
    prior_prices = _carry_forward_prices_at(eod_by_day, prior_day) if prior_day else {}
    week_prices = _carry_forward_prices_at(eod_by_day, week_day) if week_day else {}

    def _portfolio_delta(prices: dict) -> tuple[float, float]:
        if not prices or held.empty:
            return 0.0, 0.0
        d_abs, prev_v = 0.0, 0.0
        for _, r in held.iterrows():
            pp = prices.get(r["symbol"])
            last = float(r["last_price"]) if pd.notna(r["last_price"]) else None
            if pp is not None and last is not None:
                qty = float(r["qty"])
                d_abs += qty * (last - pp)
                prev_v += qty * pp
        return d_abs, (d_abs / prev_v * 100) if prev_v else 0.0

    day_chg, day_pct = _portfolio_delta(prior_prices)
    week_chg, week_pct = _portfolio_delta(week_prices)

    total_return_pct = ((realized + total_unrl) / total_cost * 100) if total_cost else 0.0
    totals = {
        "value": total_value, "cost": total_cost,
        "unrl": total_unrl, "unrl_pct": total_unrl_pct,
        "realized": realized, "total_return_pct": total_return_pct,
        "cash": cash_total, "aum": total_value + cash_total,
        "day_chg": day_chg, "day_pct": day_pct,
        "week_chg": week_chg, "week_pct": week_pct,
    }

    # Account allocation: holdings value + cash, per account
    acct_alloc = pd.DataFrame()
    if lot_rows:
        per_acct: dict[tuple[str, str], float] = defaultdict(float)
        for r in lot_rows:
            sign = 1 if r["side"].upper() == "BUY" else -1
            acct = r["account"] or "(none)"
            per_acct[(acct, r["symbol"])] += sign * float(r["quantity"])
        acct_value: dict[str, float] = defaultdict(float)
        for (acct, sym), q in per_acct.items():
            if q <= 1e-9:
                continue
            p = latest.get(sym, {}).get("last_price")
            if p is not None:
                acct_value[acct] += q * p
        for acct, c in cash_by_account.items():
            acct_value[acct] += c
        if acct_value:
            acct_alloc = pd.DataFrame(
                [{"account": a, "value": v} for a, v in acct_value.items() if v > 0]
            ).sort_values("value", ascending=False).reset_index(drop=True)
            tot = acct_alloc["value"].sum()
            acct_alloc["weight"] = (acct_alloc["value"] / tot * 100) if tot else 0.0

    held_symbols = [s for s in positions[positions["qty"] > 0]["symbol"].tolist()] if not positions.empty else []
    fd_metrics = _fetch_fd_metrics(conn)
    earnings_past, earnings_upcoming = _fetch_earnings_window(conn, held_symbols)

    return ReportData(
        as_of=as_of,
        positions=positions,
        totals=totals,
        cash_by_account=cash_by_account,
        account_allocation=acct_alloc,
        equity_curve=_build_equity_curve(lot_rows, eod_by_day),
        movers=_movers(positions, eod_by_day, today),
        realized_log=_realized_log(lot_rows),
        advisor_brief=_fetch_latest_brief(conn),
        fd_metrics=fd_metrics,
        earnings_past=earnings_past,
        earnings_upcoming=earnings_upcoming,
    )


# ─── Rendering ───────────────────────────────────────────────


def _fmt_money(v, signed: bool = False) -> str:
    if v is None or pd.isna(v):
        return "—"
    if signed:
        return f"{'+' if v > 0 else ''}${v:,.2f}"
    return f"${v:,.2f}"


def _fmt_pct(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{v:+.2f}%"


def _fmt_mcap(v) -> str:
    """Compact market cap: $2.8T / $89B / $450M."""
    if v is None or pd.isna(v):
        return "—"
    v = float(v)
    if v >= 1e12:
        return f"${v / 1e12:.2f}T"
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def _fmt_ratio(v, decimals: int = 1) -> str:
    if v is None or pd.isna(v):
        return "—"
    v = float(v)
    if v < 0:
        return "neg"
    return f"{v:.{decimals}f}"


def _fmt_growth_pct(v) -> str:
    """Revenue/earnings growth stored as fraction (0.15 = 15%)."""
    if v is None or pd.isna(v):
        return "—"
    return f"{float(v) * 100:+.1f}%"


def _color(v) -> str:
    if v is None or pd.isna(v):
        return "neu"
    if v > 0:
        return "pos"
    if v < 0:
        return "neg"
    return "neu"


def _kpi(label: str, value: str, delta: str | None = None, cls: str = "neu") -> str:
    delta_html = f'<div class="kpi-delta {cls}">{escape(delta)}</div>' if delta else ""
    return (
        f'<div class="kpi"><div class="kpi-label">{escape(label)}</div>'
        f'<div class="kpi-value">{escape(value)}</div>{delta_html}</div>'
    )


def _section_header(d: ReportData) -> str:
    aum = d.totals["aum"]
    return f"""
    <header class="hdr">
      <div>
        <h1>Portfolio Executive Report</h1>
        <div class="hdr-sub">As of {escape(d.as_of.strftime('%A, %B %d, %Y · %H:%M'))} · {escape(TZ_NAME)}</div>
      </div>
      <div class="hdr-aum">
        <div class="aum-label">Total AUM</div>
        <div class="aum-value">{_fmt_money(aum)}</div>
        <div class="aum-delta {_color(d.totals['day_chg'])}">
          {_fmt_money(d.totals['day_chg'], signed=True)} ({_fmt_pct(d.totals['day_pct'])}) today
        </div>
      </div>
    </header>"""


def _section_kpis(d: ReportData) -> str:
    t = d.totals
    cards = [
        _kpi("Market Value", _fmt_money(t["value"])),
        _kpi("Cost Basis", _fmt_money(t["cost"])),
        _kpi("Unrealized P&L", _fmt_money(t["unrl"], signed=True), _fmt_pct(t["unrl_pct"]), _color(t["unrl"])),
        _kpi("Realized P&L", _fmt_money(t["realized"], signed=True), None, _color(t["realized"])),
        _kpi("Total Return %", _fmt_pct(t["total_return_pct"]), None, _color(t["total_return_pct"])),
        _kpi("Cash", _fmt_money(t["cash"])),
        _kpi("Day Δ", _fmt_money(t["day_chg"], signed=True), _fmt_pct(t["day_pct"]), _color(t["day_chg"])),
        _kpi("Week Δ", _fmt_money(t["week_chg"], signed=True), _fmt_pct(t["week_pct"]), _color(t["week_chg"])),
    ]
    return f'<section class="section"><h2>Key Metrics</h2><div class="kpi-grid">{"".join(cards)}</div></section>'


def _section_holdings(d: ReportData) -> str:
    df = d.positions[d.positions["qty"] > 0].copy()
    if df.empty:
        return '<section class="section"><h2>Holdings</h2><p class="empty">No active holdings.</p></section>'
    df = df.sort_values("market_value", ascending=False, na_position="last")
    rows = []
    for _, r in df.iterrows():
        unr = r["unrealized_pnl"]
        unr_pct = r["unrealized_pct"]
        fd = d.fd_metrics.get(r["symbol"], {})
        rows.append(
            f'<tr><td class="sym">{escape(str(r["symbol"]))}</td>'
            f'<td class="num">{r["qty"]:,.4f}</td>'
            f'<td class="num">{_fmt_money(r["avg_cost"])}</td>'
            f'<td class="num">{_fmt_money(r["last_price"])}</td>'
            f'<td class="num">{_fmt_money(r["market_value"])}</td>'
            f'<td class="num">{_fmt_money(r["open_cost"])}</td>'
            f'<td class="num {_color(unr)}">{_fmt_money(unr, signed=True)}</td>'
            f'<td class="num {_color(unr_pct)}">{_fmt_pct(unr_pct)}</td>'
            f'<td class="num">{r["weight"]:.2f}%</td>'
            f'<td class="num fd">{_fmt_ratio(fd.get("pe_ratio"))}</td>'
            f'<td class="num fd">{_fmt_growth_pct(fd.get("revenue_growth"))}</td>'
            f'<td class="num fd">{_fmt_mcap(fd.get("market_cap"))}</td></tr>'
        )
    enriched = sum(1 for sym in df["symbol"] if sym in d.fd_metrics)
    footnote = (
        f'<p class="small-note">Fundamentals shown for {enriched}/{len(df)} positions '
        f'(ETFs and unenriched symbols show "—").</p>'
    )
    return f"""
    <section class="section">
      <h2>Holdings</h2>
      <table class="data">
        <thead><tr>
          <th>Symbol</th><th class="num">Qty</th><th class="num">Avg Cost</th>
          <th class="num">Last</th><th class="num">Market Value</th><th class="num">Cost Basis</th>
          <th class="num">Unrl P&amp;L</th><th class="num">Unrl %</th><th class="num">Weight</th>
          <th class="num fd">P/E</th><th class="num fd">Rev YoY</th><th class="num fd">Mkt Cap</th>
        </tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
      {footnote}
    </section>"""


def _plotly_div(fig: go.Figure) -> str:
    return pio.to_html(
        fig,
        full_html=False,
        include_plotlyjs=False,
        config={"displayModeBar": False, "responsive": True},
    )


def _section_allocation(d: ReportData) -> str:
    held = d.positions[d.positions["qty"] > 0]
    if held.empty:
        return ""
    pie = px.pie(
        held, values="market_value", names="symbol", hole=0.5,
        color_discrete_sequence=px.colors.qualitative.Pastel,
    )
    pie.update_traces(textposition="inside", textinfo="percent+label")
    pie.update_layout(
        height=420, margin=dict(l=20, r=20, t=20, b=20),
        paper_bgcolor="white", plot_bgcolor="white", showlegend=False,
        font=dict(family="Inter, system-ui, sans-serif"),
    )
    pie_html = _plotly_div(pie)

    bar_html = '<p class="empty">No account-level data.</p>'
    if not d.account_allocation.empty:
        bar = px.bar(
            d.account_allocation, x="account", y="value",
            color_discrete_sequence=["#2c5282"], text="weight",
        )
        bar.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
        bar.update_layout(
            height=320, margin=dict(l=20, r=20, t=20, b=40),
            paper_bgcolor="white", plot_bgcolor="white",
            yaxis=dict(title="Value ($)", gridcolor="#eee", tickprefix="$", tickformat=","),
            xaxis=dict(title=""),
            font=dict(family="Inter, system-ui, sans-serif"),
        )
        bar_html = _plotly_div(bar)

    return f"""
    <section class="section">
      <h2>Allocation</h2>
      <div class="grid-2">
        <div><h3>By Symbol</h3>{pie_html}</div>
        <div><h3>By Account</h3>{bar_html}</div>
      </div>
    </section>"""


def _section_equity_curve(d: ReportData) -> str:
    if d.equity_curve.empty:
        return ""
    fig = go.Figure(go.Scatter(
        x=d.equity_curve["date"],
        y=d.equity_curve["value"],
        mode="lines",
        line=dict(color="#2c5282", width=2),
        fill="tozeroy",
        fillcolor="rgba(44,82,130,0.08)",
        hovertemplate="%{x|%Y-%m-%d}<br>$%{y:,.2f}<extra></extra>",
    ))
    fig.update_layout(
        height=380, margin=dict(l=60, r=20, t=20, b=40),
        paper_bgcolor="white", plot_bgcolor="white",
        xaxis=dict(title="", gridcolor="#eee"),
        yaxis=dict(title="Holdings Value ($)", gridcolor="#eee", tickprefix="$", tickformat=","),
        font=dict(family="Inter, system-ui, sans-serif"),
    )
    return f'<section class="section"><h2>Holdings Value Over Time</h2>{_plotly_div(fig)}</section>'


def _movers_table(rows) -> str:
    if not rows:
        return '<p class="empty">No data.</p>'
    body = "".join(
        f'<tr><td class="sym">{escape(r["symbol"])}</td>'
        f'<td class="num {_color(r["delta"])}">{_fmt_money(r["delta"], signed=True)}</td>'
        f'<td class="num {_color(r["pct"])}">{_fmt_pct(r["pct"])}</td></tr>'
        for r in rows
    )
    return (
        '<table class="data compact">'
        '<thead><tr><th>Symbol</th><th class="num">Δ ($)</th><th class="num">%</th></tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )


def _section_movers(d: ReportData) -> str:
    m = d.movers
    if not m:
        return ""
    return f"""
    <section class="section">
      <h2>Top Movers</h2>
      <div class="grid-3">
        <div>
          <h3>Today</h3>
          <h4 class="up">Gainers</h4>{_movers_table(m.get("day_top", []))}
          <h4 class="down">Losers</h4>{_movers_table(m.get("day_bot", []))}
        </div>
        <div>
          <h3>Past 7 Days</h3>
          <h4 class="up">Gainers</h4>{_movers_table(m.get("week_top", []))}
          <h4 class="down">Losers</h4>{_movers_table(m.get("week_bot", []))}
        </div>
        <div>
          <h3>All-Time Unrealized</h3>
          <h4 class="up">Winners</h4>{_movers_table(m.get("all_top", []))}
          <h4 class="down">Underperformers</h4>{_movers_table(m.get("all_bot", []))}
        </div>
      </div>
    </section>"""


def _brief_age_badge(brief_ts: datetime) -> tuple[str, str]:
    """Return (label, cls) describing how fresh the brief is vs wall-clock now."""
    if brief_ts is None:
        return ("no timestamp", "neu")
    now = datetime.now(JER)
    delta = now - brief_ts.astimezone(JER)
    hours = max(delta.total_seconds() / 3600, 0)
    minutes = max(int(delta.total_seconds() / 60), 0)
    if hours < 1:
        return (f"generated {minutes} min ago", "pos")
    if hours < 12:
        return (f"generated {int(hours)}h ago", "pos")
    if hours < 36:
        return (f"generated {int(hours)}h ago", "neu")
    days = int(hours / 24)
    return (f"⚠ STALE — generated {days}d ago", "neg")


def _section_advisor_brief(d: ReportData) -> str:
    brief = d.advisor_brief
    if not brief:
        return (
            '<section class="section"><h2>Advisor Brief</h2>'
            '<p class="empty">No briefs generated yet. Run <code>.\\run_brief.ps1</code> '
            'or click <em>Generate Brief</em> in the dashboard\'s Advisor tab.</p></section>'
        )
    payload = brief.get("payload") or {}
    age_label, age_cls = _brief_age_badge(brief["ts"])
    summary = payload.get("summary") or ""
    insights = payload.get("insights") or []
    suggestions = payload.get("suggestions") or []

    ins_html = '<p class="empty">No insights.</p>'
    if insights:
        items = []
        for i in insights:
            tag = i.get("tag", "")
            tag_html = f'<span class="brief-tag">{escape(tag)}</span>' if tag else ""
            items.append(
                f'<li><div class="brief-title">{escape(i.get("title","(untitled)"))} {tag_html}</div>'
                f'<div class="brief-body">{escape(i.get("body",""))}</div></li>'
            )
        ins_html = f'<ul class="brief-list">{"".join(items)}</ul>'

    sug_html = '<p class="empty">No suggestions today — boredom is alpha.</p>'
    if suggestions:
        items = []
        for s in suggestions:
            rule = s.get("rule_invoked") or "—"
            items.append(
                f'<li><div class="brief-title">{escape(s.get("action","(no action)"))}</div>'
                f'<div class="brief-body">{escape(s.get("rationale",""))}</div>'
                f'<div class="brief-rule">Rule: {escape(rule)}</div></li>'
            )
        sug_html = f'<ul class="brief-list">{"".join(items)}</ul>'

    return f"""
    <section class="section">
      <h2>Advisor Brief <span class="badge {age_cls}">{escape(age_label)}</span></h2>
      <p class="lead">{escape(summary)}</p>
      <div class="grid-2">
        <div><h3>Insights ({len(insights)})</h3>{ins_html}</div>
        <div><h3>Suggestions ({len(suggestions)})</h3>{sug_html}</div>
      </div>
    </section>"""


def _fmt_surprise_cell(label: str | None, pct) -> str:
    if label is None and (pct is None or pd.isna(pct)):
        return "—"
    if pct is None or pd.isna(pct):
        return escape(label or "—")
    pct_val = float(pct) * 100
    pct_str = f"{pct_val:+.1f}%"
    label_str = label or ("BEAT" if pct_val > 0 else "MISS" if pct_val < 0 else "INLINE")
    cls = "pos" if pct_val > 0 else ("neg" if pct_val < 0 else "neu")
    return f'<span class="{cls}">{escape(label_str)} {pct_str}</span>'


def _section_earnings_calendar(d: ReportData) -> str:
    past = d.earnings_past
    upcoming = d.earnings_upcoming
    if not past and not upcoming:
        return ""

    past_html = '<p class="empty">No earnings in the trailing 60 days.</p>'
    if past:
        rows = []
        for r in past:
            rows.append(
                f'<tr><td>{r["filing_date"]}</td>'
                f'<td class="sym">{escape(r["symbol"])}</td>'
                f'<td>{escape(r["fiscal_period"] or "")}</td>'
                f'<td class="num">{_fmt_ratio(r["eps_actual"], 2)}</td>'
                f'<td class="num">{_fmt_ratio(r["eps_estimate"], 2)}</td>'
                f'<td>{_fmt_surprise_cell(r["eps_surprise"], r["eps_surprise_pct"])}</td>'
                f'<td>{_fmt_surprise_cell(r["revenue_surprise"], r["revenue_surprise_pct"])}</td></tr>'
            )
        past_html = (
            '<table class="data compact">'
            '<thead><tr>'
            '<th>Filed</th><th>Symbol</th><th>Period</th>'
            '<th class="num">EPS Act</th><th class="num">EPS Est</th>'
            '<th>EPS Surprise</th><th>Rev Surprise</th>'
            '</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
        )

    upcoming_html = '<p class="empty">No upcoming earnings in the next 45 days (based on filing cadence).</p>'
    if upcoming:
        rows = []
        for r in upcoming:
            badge = '<span class="badge neg">overdue</span> ' if r["overdue"] else ""
            rows.append(
                f'<tr><td>{badge}{r["expected"]}</td>'
                f'<td class="sym">{escape(r["symbol"])}</td>'
                f'<td>{r["last_filing"]}</td>'
                f'<td>{r["last_period"]}</td></tr>'
            )
        upcoming_html = (
            '<table class="data compact">'
            '<thead><tr>'
            '<th>Expected ~</th><th>Symbol</th><th>Last Filed</th><th>Last Period</th>'
            '</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
            '<p class="small-note">Expected dates are estimates: last filing + 90d (quarterly) or +365d (annual). Treat as guidance, not confirmation.</p>'
        )

    return f"""
    <section class="section">
      <h2>Earnings Calendar</h2>
      <div class="grid-2">
        <div><h3>Past 60 days</h3>{past_html}</div>
        <div><h3>Next 45 days (estimated)</h3>{upcoming_html}</div>
      </div>
    </section>"""


def _section_realized(d: ReportData) -> str:
    df = d.realized_log
    if df.empty:
        return '<section class="section"><h2>Realized P&amp;L Log</h2><p class="empty">No closed positions.</p></section>'
    summary = (
        df.groupby("symbol")
        .agg(matches=("qty", "count"), realized=("realized_pnl", "sum"))
        .sort_values("realized", ascending=False)
        .reset_index()
    )
    sum_rows = "".join(
        f'<tr><td class="sym">{escape(r["symbol"])}</td>'
        f'<td class="num">{int(r["matches"])}</td>'
        f'<td class="num {_color(r["realized"])}">{_fmt_money(r["realized"], signed=True)}</td></tr>'
        for _, r in summary.iterrows()
    )
    detail = df.head(30)
    det_rows = "".join(
        f'<tr><td>{r["sell_date"]}</td><td class="sym">{escape(r["symbol"])}</td>'
        f'<td>{escape(r["account"] or "")}</td>'
        f'<td class="num">{r["qty"]:,.4f}</td>'
        f'<td class="num">{_fmt_money(r["buy_cost_ps"])}</td>'
        f'<td class="num">{_fmt_money(r["sell_proceeds_ps"])}</td>'
        f'<td class="num {_color(r["realized_pnl"])}">{_fmt_money(r["realized_pnl"], signed=True)}</td></tr>'
        for _, r in detail.iterrows()
    )
    detail_label = f"Recent {len(detail)} closed matches" if len(df) > 30 else "Closed matches"
    return f"""
    <section class="section">
      <h2>Realized P&amp;L Log</h2>
      <div class="grid-2">
        <div>
          <h3>Summary by Symbol</h3>
          <table class="data compact">
            <thead><tr><th>Symbol</th><th class="num">Matches</th><th class="num">Realized P&amp;L</th></tr></thead>
            <tbody>{sum_rows}</tbody>
          </table>
        </div>
        <div>
          <h3>{detail_label}</h3>
          <table class="data compact small">
            <thead><tr><th>Date</th><th>Symbol</th><th>Account</th><th class="num">Qty</th>
              <th class="num">Buy/sh</th><th class="num">Sell/sh</th><th class="num">P&amp;L</th></tr></thead>
            <tbody>{det_rows}</tbody>
          </table>
        </div>
      </div>
    </section>"""


def _section_cash(d: ReportData) -> str:
    if not d.cash_by_account:
        return ""
    rows = "".join(
        f'<tr><td>{escape(a)}</td><td class="num">{_fmt_money(v)}</td></tr>'
        for a, v in sorted(d.cash_by_account.items(), key=lambda kv: -kv[1])
    )
    total = sum(d.cash_by_account.values())
    return f"""
    <section class="section">
      <h2>Cash Positions</h2>
      <table class="data compact">
        <thead><tr><th>Account</th><th class="num">Cash</th></tr></thead>
        <tbody>{rows}
          <tr class="total"><td><strong>Total</strong></td>
            <td class="num"><strong>{_fmt_money(total)}</strong></td></tr>
        </tbody>
      </table>
    </section>"""


_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  background: #f6f7fb; color: #1a202c; margin: 0; padding: 0;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
.report {
  max-width: 1180px; margin: 0 auto; padding: 32px;
  background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
.hdr {
  display: flex; justify-content: space-between; align-items: flex-start;
  border-bottom: 3px solid #2c5282; padding-bottom: 16px; margin-bottom: 28px;
}
.hdr h1 { margin: 0 0 4px 0; font-size: 26px; font-weight: 700; color: #1a365d; letter-spacing: -0.5px; }
.hdr-sub { color: #718096; font-size: 13px; }
.hdr-aum { text-align: right; }
.aum-label { color: #718096; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; }
.aum-value { font-size: 26px; font-weight: 700; color: #1a365d; }
.aum-delta { font-size: 13px; font-weight: 600; }
.section { margin-bottom: 32px; page-break-inside: avoid; }
.section h2 {
  font-size: 16px; font-weight: 700; color: #2d3748;
  border-left: 4px solid #2c5282; padding-left: 12px; margin: 0 0 14px 0;
}
.section h3 { font-size: 13px; font-weight: 600; color: #4a5568; margin: 0 0 8px 0; }
.section h4 { font-size: 11px; font-weight: 600; margin: 12px 0 4px 0; text-transform: uppercase; letter-spacing: 0.5px; }
h4.up { color: #2f855a; }
h4.down { color: #c53030; }
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.kpi {
  border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px 14px;
  background: linear-gradient(180deg, #ffffff, #f7fafc);
}
.kpi-label { color: #718096; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; }
.kpi-value { font-size: 19px; font-weight: 700; color: #1a365d; margin-top: 4px; }
.kpi-delta { font-size: 11px; font-weight: 600; margin-top: 2px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; }
table.data { width: 100%; border-collapse: collapse; font-size: 12px; }
table.data thead th {
  background: #edf2f7; color: #2d3748; font-weight: 600;
  text-align: left; padding: 7px 10px; border-bottom: 2px solid #cbd5e0;
}
table.data tbody td { padding: 6px 10px; border-bottom: 1px solid #edf2f7; }
table.data tbody tr:hover { background: #f7fafc; }
table.data .num { text-align: right; font-variant-numeric: tabular-nums; }
table.data .sym { font-weight: 600; color: #1a365d; }
table.data tr.total td { border-top: 2px solid #cbd5e0; background: #f7fafc; }
table.data.compact thead th, table.data.compact tbody td { padding: 5px 8px; font-size: 11px; }
table.data.small { font-size: 10.5px; }
.pos { color: #2f855a; }
.neg { color: #c53030; }
.neu { color: #718096; }
.empty { color: #a0aec0; font-style: italic; font-size: 12px; }
.footer { margin-top: 28px; padding-top: 14px; border-top: 1px solid #e2e8f0;
  color: #a0aec0; font-size: 10px; text-align: center; }
.small-note { color: #a0aec0; font-size: 11px; margin: 6px 0 0; font-style: italic; }
.lead { font-size: 14px; color: #2d3748; margin: 0 0 14px 0; line-height: 1.5; }
.badge {
  display: inline-block; padding: 2px 8px; font-size: 10px; font-weight: 700;
  border-radius: 999px; vertical-align: middle; margin-left: 6px;
  background: #edf2f7; color: #4a5568; text-transform: uppercase; letter-spacing: 0.4px;
}
.badge.pos { background: #f0fff4; color: #2f855a; }
.badge.neu { background: #edf2f7; color: #718096; }
.badge.neg { background: #fff5f5; color: #c53030; }
.brief-list { margin: 0; padding-left: 18px; }
.brief-list li { margin-bottom: 12px; }
.brief-title { font-weight: 600; font-size: 13px; color: #1a365d; margin-bottom: 2px; }
.brief-body { font-size: 12px; color: #2d3748; line-height: 1.45; }
.brief-rule { font-size: 11px; color: #718096; margin-top: 4px; font-style: italic; }
.brief-tag {
  display: inline-block; padding: 1px 6px; font-size: 9px; font-weight: 600;
  background: #ebf4ff; color: #2c5282; border-radius: 4px;
  text-transform: uppercase; letter-spacing: 0.4px; margin-left: 6px;
  vertical-align: middle;
}
table.data .fd { color: #4a5568; font-size: 11px; }
@media print {
  body { background: white; }
  .report { box-shadow: none; max-width: 100%; padding: 12px; }
  .section { page-break-inside: avoid; }
  .hdr { page-break-after: avoid; }
}
@media (max-width: 800px) {
  .grid-2, .grid-3 { grid-template-columns: 1fr; }
  .hdr { flex-direction: column; }
  .hdr-aum { text-align: left; margin-top: 12px; }
}
"""


def build_html(conn) -> str:
    d = gather(conn)
    plotly_js = get_plotlyjs()
    sections = [
        _section_header(d),
        _section_kpis(d),
        _section_advisor_brief(d),
        _section_holdings(d),
        _section_allocation(d),
        _section_equity_curve(d),
        _section_movers(d),
        _section_earnings_calendar(d),
        _section_realized(d),
        _section_cash(d),
    ]
    body = "\n".join(s for s in sections if s)
    title = f"Portfolio Executive Report — {d.as_of.strftime('%Y-%m-%d')}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{_CSS}</style>
<script>{plotly_js}</script>
</head>
<body>
<div class="report">
{body}
<div class="footer">Generated by PortfolioDB · {escape(d.as_of.strftime('%Y-%m-%d %H:%M %Z'))}</div>
</div>
</body>
</html>
"""
