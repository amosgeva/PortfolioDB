"""Executive HTML report generator for PortfolioDB.

Builds a self-contained HTML document with KPIs, holdings, allocation,
equity curve, top movers, realized P&L log, and cash positions.

Entry point: build_html(conn) -> str

The report is three modules behind this one (audit R-4):

* `exec_report_queries` — every SQL read;
* `exec_report_analytics` — the arithmetic, pure functions over prepared rows;
* `exec_report_render` — formatters, sections and the HTML document.

This module only orchestrates: `gather()` fetches, restates through the
prepared ledger and computes; `build_html()` renders. The old underscore names
are kept as aliases because tests and callers patch them here, and `gather()`
looks them up in this module's namespace so a patch takes effect.
"""

from __future__ import annotations

from plotly.offline import get_plotlyjs

import exec_report_analytics as analytics
import exec_report_queries as queries
import exec_report_render as render
import ledger_inputs
import portfolio_kpis as kpis
import reporting_tz
from exec_report_analytics import ReportData
from portfolio import compute_fifo_merged

__all__ = ["ReportData", "build_html", "gather", "get_plotlyjs"]

# ─── Back-compatible names (see the module docstring) ───────

_fetch_latest_prices = queries.fetch_latest_prices
_fetch_eod_by_day = queries.fetch_eod_by_day
_fetch_latest_brief = queries.fetch_latest_brief
_fetch_fd_metrics = queries.fetch_fd_metrics
_fetch_earnings_window = queries.fetch_earnings_window
_fetch_cash = queries.fetch_cash
_fetch_income = queries.fetch_income
_fetch_instruments = queries.fetch_instruments

_restate_latest = analytics.restate_latest
_holdings_at_date = analytics.holdings_at_date
_build_equity_curve = analytics.build_equity_curve
_carry_forward_prices_at = analytics.carry_forward_prices_at
_movers = analytics.movers
_realized_log = analytics.realized_log

_section_header = render._section_header
_section_kpis = render._section_kpis
_section_holdings = render._section_holdings
_section_allocation = render._section_allocation
_section_equity_curve = render._section_equity_curve
_section_movers = render._section_movers
_section_advisor_brief = render._section_advisor_brief
_section_earnings_calendar = render._section_earnings_calendar
_section_realized = render._section_realized
_section_cash = render._section_cash
_brief_age_badge = render._brief_age_badge


# ─── Data gathering ──────────────────────────────────────────


def gather(conn) -> ReportData:
    tz_name = reporting_tz.tz_name()
    tz = reporting_tz.tzinfo()
    # Lots and the EOD price series come restated through the same prepared
    # ledger the dashboard and the MCP tools use, so a recorded split reads as
    # a share count here and not as a cliff in the value chart.
    ledger = ledger_inputs.load(conn)
    lot_rows = ledger.lots
    fifo_pos = compute_fifo_merged(lot_rows)
    latest = _restate_latest(ledger, _fetch_latest_prices(conn))
    eod_by_day = ledger.price_by_day(_fetch_eod_by_day(conn))
    instruments = list(_fetch_instruments(conn))
    benchmarks = {r["symbol"] for r in instruments if r.get("benchmark")}
    # Cash and income in another currency are not dollars: they are left out
    # of every total and listed instead (audit F-11 / Q-8).
    cash_rows = [dict(r) for r in _fetch_cash(conn)]
    cash_by_account = {
        (r["account"] or "(merged)"): float(r["cash"])
        for r in cash_rows if kpis.currency_of(r) == kpis.BASE_CURRENCY
    }
    income_rows = list(_fetch_income(conn))
    usd_income, _foreign_income = kpis.split_by_currency(income_rows)
    income_total = sum((kpis.to_dec(r["amount"]) or kpis.ZERO for r in usd_income), kpis.ZERO)

    positions = analytics.price_positions(fifo_pos, latest)
    held_symbols = analytics.held_symbols(positions)
    # "As of" is when the HOLDINGS were last priced, never a benchmark's quote
    # (audit F-8); a held symbol quoted on an earlier day is stale: valued at
    # that quote, listed, and left out of the day's change.
    as_of = analytics.report_as_of(latest, tz, held_symbols, benchmarks)
    today = as_of.date()
    stale = kpis.stale_quotes({s: v.get("ts") for s, v in latest.items()}, held_symbols, as_of, tz)
    stale_syms = {e["symbol"] for e in stale}
    totals = analytics.compute_totals(
        positions, cash_by_account, eod_by_day, today, lot_rows, income_total, stale_syms
    )
    acct_alloc = analytics.account_allocation(lot_rows, latest, cash_by_account)

    fd_metrics = _fetch_fd_metrics(conn)
    earnings_past, earnings_upcoming = _fetch_earnings_window(conn, held_symbols)

    return ReportData(
        as_of=as_of,
        positions=positions,
        totals=totals,
        cash_by_account=cash_by_account,
        account_allocation=acct_alloc,
        equity_curve=_build_equity_curve(lot_rows, eod_by_day),
        movers=_movers(positions, eod_by_day, today, stale_syms),
        realized_log=_realized_log(lot_rows),
        advisor_brief=_fetch_latest_brief(conn),
        fd_metrics=fd_metrics,
        earnings_past=earnings_past,
        earnings_upcoming=earnings_upcoming,
        tz_name=tz_name,
        stale=stale,
        currency_warnings=kpis.currency_warnings(
            instruments=instruments,
            traded_symbols={r["symbol"] for r in lot_rows},
            held_symbols=held_symbols,
            income=income_rows,
            cash=cash_rows,
        ),
        cash_rows=cash_rows,
    )


def build_html(conn) -> str:
    return render.render_document(gather(conn), get_plotlyjs())
