"""Golden output of the executive report, over the demo ledger.

`exec_report.py` was one 1,055-line module mixing SQL, analytics and HTML
(audit R-4). It is now split into `exec_report_queries`, `exec_report_analytics`
and `exec_report_render`, with `exec_report` keeping only `gather()` and
`build_html()`. A refactor of a report is only safe if the report is the same
afterwards, so this test renders the whole document and compares it with a
committed copy, character for character.

The input is the fictional portfolio `demo_seed.py` writes, generated with its
clock frozen so the random walk, the trade dates and the cash rows are the same
on every run. The database is replaced at the report's own fetchers, which
answer from those rows the way their SQL does. Three things are normalised
because they are not the report's to decide: the inlined plotly.js bundle (a
dependency, megabytes of it), the random div ids plotly gives each chart, and
the `template` block plotly writes into every figure layout (theme defaults
that change with plotly releases). Everything else, including every chart's
data and the rest of its layout, is compared.

The golden file was captured from the pre-split module; the split commit left
it untouched, which is the proof the split changed nothing. Later commits that
change what the report says (1.8.0: F-5, F-8, F-11, F-12) regenerate it on
purpose, so the reviewer sees the whole effect as one diff. To regenerate:

    PDB_UPDATE_GOLDEN=1 pytest tests/test_exec_report_golden.py   (from app/)
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import demo_seed
import exec_report
import ledger_inputs

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "exec_report_demo.golden.html"

# A Friday evening after the demo's 20:00 UTC "close", so the latest session is
# a full one and the previous session is Thursday.
FROZEN_NOW = datetime(2026, 11, 13, 21, 0, tzinfo=timezone.utc)


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW.replace(tzinfo=None)


def demo_rows(monkeypatch) -> dict:
    """What `demo_seed.seed()` would write, at FROZEN_NOW, as plain rows."""
    monkeypatch.setattr(demo_seed, "datetime", _Frozen)
    prices = demo_seed.simulate_prices()
    today = FROZEN_NOW.date()
    lots = []
    for i, (sym, account, side, days_ago, qty, factor, fees) in enumerate(demo_seed.TRADES, start=1):
        trade_date = today - timedelta(days=days_ago)
        price = round(demo_seed.close_on(prices[sym], trade_date) * factor, 4)
        lots.append({
            "id": i, "symbol": sym, "account": account, "side": side,
            "trade_date": trade_date, "quantity": Decimal(str(qty)),
            "price": Decimal(str(price)), "fees": Decimal(str(fees)),
        })
    lots.sort(key=lambda r: (r["symbol"], r["account"] or "", r["trade_date"], r["id"]))
    snapshots = [
        {"ts": ts, "symbol": sym, "last_price": Decimal(str(p))}
        for sym, series in prices.items() for ts, p in series
    ]
    cash = [
        {"account": acct, "cash": Decimal(str(c)), "ts": FROZEN_NOW, "currency": "USD"}
        for acct, c in demo_seed.CASH
    ]
    benchmarks = set(demo_seed.BENCHMARKS)
    return {"lots": lots, "snapshots": snapshots, "cash": cash, "benchmarks": benchmarks}


def install_demo(monkeypatch, rows: dict) -> None:
    """Answer the report's fetchers from `rows`, as their SQL would."""
    tz = exec_report.reporting_tz.tzinfo()
    snaps = rows["snapshots"]

    def load(conn, **_kw):
        return ledger_inputs.prepare(rows["lots"], [])

    def latest_prices(conn):
        out: dict = {}
        for r in snaps:
            cur = out.get(r["symbol"])
            if cur is None or r["ts"] > cur["ts"]:
                out[r["symbol"]] = {"ts": r["ts"], "last_price": float(r["last_price"])}
        return out

    def eod_by_day(conn):
        last: dict = {}
        for r in snaps:
            key = (r["symbol"], r["ts"].astimezone(tz).date())
            if key not in last or r["ts"] > last[key][0]:
                last[key] = (r["ts"], float(r["last_price"]))
        by_day: dict = defaultdict(dict)
        for (sym, d), (_ts, p) in sorted(last.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            by_day[d][sym] = p
        return dict(by_day)

    def cash(conn):
        return [dict(r) for r in rows["cash"]]

    def income(conn):
        return [dict(r) for r in rows.get("income", [])]

    def instruments(conn):
        syms = {r["symbol"] for r in snaps} | {r["symbol"] for r in rows["lots"]}
        return [{"symbol": s, "currency": rows.get("currencies", {}).get(s, "USD"),
                 "benchmark": s in rows["benchmarks"]} for s in sorted(syms)]

    def brief(conn):
        return {"id": 1, "ts": FROZEN_NOW - timedelta(hours=2), "kind": "morning",
                "total_value": Decimal("56000"), "payload": demo_seed.DEMO_BRIEF}

    def fd_metrics(conn):
        return {"AAPL": {"symbol": "AAPL", "pe_ratio": Decimal("31.2"), "revenue_growth": Decimal("0.061"),
                         "market_cap": Decimal("3.1e12"), "net_margin": Decimal("0.24"),
                         "earnings_growth": Decimal("0.09"), "fetched_at": FROZEN_NOW}}

    def earnings(conn, symbols, past_days=60, upcoming_days=45):
        past = [{"symbol": "AAPL", "report_period": date(2026, 9, 27), "filing_date": date(2026, 10, 30),
                 "fiscal_period": "Q4", "eps_actual": Decimal("1.85"), "eps_estimate": Decimal("1.78"),
                 "eps_surprise": "BEAT", "eps_surprise_pct": Decimal("0.039"),
                 "revenue_actual": None, "revenue_estimate": None, "revenue_surprise": None,
                 "revenue_surprise_pct": None}]
        upcoming = [{"symbol": "MSFT", "expected": date(2026, 11, 25), "last_filing": date(2026, 8, 27),
                     "last_period": date(2026, 6, 30), "cadence_days": 90, "overdue": False}]
        return past, upcoming

    monkeypatch.setattr(exec_report.ledger_inputs, "load", load)
    monkeypatch.setattr(exec_report, "_fetch_latest_prices", latest_prices)
    monkeypatch.setattr(exec_report, "_fetch_eod_by_day", eod_by_day)
    monkeypatch.setattr(exec_report, "_fetch_cash", cash)
    if hasattr(exec_report, "_fetch_income"):
        monkeypatch.setattr(exec_report, "_fetch_income", income)
        monkeypatch.setattr(exec_report, "_fetch_instruments", instruments)
    monkeypatch.setattr(exec_report, "_fetch_latest_brief", brief)
    monkeypatch.setattr(exec_report, "_fetch_fd_metrics", fd_metrics)
    monkeypatch.setattr(exec_report, "_fetch_earnings_window", earnings)
    monkeypatch.setattr(exec_report, "get_plotlyjs", lambda: "/* plotly.js elided */")
    # The brief's age badge reads the wall clock; freeze it wherever it lives.
    badge_module = sys.modules[exec_report._brief_age_badge.__module__]
    monkeypatch.setattr(badge_module, "datetime", _Frozen)


_PLOT = re.compile(
    r'<div id="(?P<id>[0-9a-f-]{36})" class="plotly-graph-div"[^>]*></div>\s*'
    r'<script[^>]*>(?P<js>.*?)</script>',
    re.S,
)


def normalise(html: str) -> str:
    """Replace each plotly figure with its data and layout (template removed)."""
    counter = iter(range(1, 1000))

    dec = json.JSONDecoder()

    def next_value(js: str, pos: int):
        """The JSON value after the next comma at or past `pos`."""
        pos = js.index(",", pos) + 1
        while js[pos].isspace():
            pos += 1
        return dec.raw_decode(js, pos)

    def one(m: re.Match) -> str:
        js = m.group("js")
        start = js.index("Plotly.newPlot(") + len("Plotly.newPlot(")
        while js[start].isspace():
            start += 1
        _id, pos = dec.raw_decode(js, start)
        data, pos = next_value(js, pos)
        layout, _ = next_value(js, pos)
        layout.pop("template", None)
        n = next(counter)
        return (f'<figure data-plot="{n}">\n<data>{json.dumps(data, sort_keys=True)}</data>\n'
                f'<layout>{json.dumps(layout, sort_keys=True)}</layout>\n</figure>')

    return _PLOT.sub(one, html)


def render(monkeypatch) -> str:
    install_demo(monkeypatch, demo_rows(monkeypatch))
    return normalise(exec_report.build_html(object()))


def test_the_report_matches_its_golden_copy(monkeypatch):
    html = render(monkeypatch)
    if os.getenv("PDB_UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(html, encoding="utf-8", newline="\n")
    expected = GOLDEN.read_text(encoding="utf-8")
    assert html == expected, (
        "the executive report changed; if that is intended, regenerate with "
        "PDB_UPDATE_GOLDEN=1 and review the diff of the golden file"
    )


def test_the_golden_copy_is_not_trivial():
    """A golden file that lost its sections would pass the comparison forever."""
    text = GOLDEN.read_text(encoding="utf-8")
    for needle in ("Key Metrics", "Holdings", "Allocation", "Holdings Value Over Time",
                   "Top Movers", "Earnings Calendar", "Realized P&amp;L Log", "Cash Positions",
                   "Advisor Brief", "VOO", "NVDA", "Broker A"):
        assert needle in text, needle
    assert text.count("<figure data-plot=") == 3
