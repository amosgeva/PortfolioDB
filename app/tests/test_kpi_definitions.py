"""The shared KPI definitions, and every surface that uses them agreeing.

Audit F-12 / Q-2: the dashboard and the Advisor subtracted the cost of an
unpriced position from the market value of the priced ones, so a position with
no quote read as a 100% loss (priced cost 100 → value 120, plus unpriced cost
100, gave −80), while the MCP summed per-row P&L (+20). Audit F-5 / Q-4: "total
return" divided lifetime P&L by the cost still open, so it exploded after a
large sale and read 0% after a full one.

The parity class at the bottom runs the three surfaces this workstream owns —
the dashboard payload (`build_payload_data`), the Advisor snapshot
(`snapshot_portfolio`) and the executive report (`gather`) — through their real
entry points over one ledger with a priced and an unpriced holding and a
closed round trip, with non-flat prices, and asserts they report the same
figures.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import ledger_inputs
import portfolio_kpis as kpis

D = Decimal


def _lot(i, sym, side, qty, price, day, fees="0", account="IBKR"):
    return {"id": i, "symbol": sym, "account": account, "side": side, "trade_date": day,
            "quantity": D(qty), "price": D(price), "fees": D(fees)}


# ─── peak net invested ────────────────────────────────────────


class TestPeakNetInvested:
    def test_buys_add_outlay_including_fees(self):
        lots = [_lot(1, "A", "BUY", "10", "10", date(2026, 1, 2), fees="1")]
        assert kpis.peak_net_invested(lots) == D("101")

    def test_a_sale_does_not_lower_the_peak(self):
        lots = [_lot(1, "A", "BUY", "10", "100", date(2026, 1, 2)),
                _lot(2, "A", "SELL", "9", "190", date(2026, 2, 2))]
        assert kpis.peak_net_invested(lots) == D("1000")

    def test_recycled_proceeds_do_not_inflate_it(self):
        # Buy 100, sell for 120, buy again for 120: never more than 100 of
        # the owner's money was committed.
        lots = [_lot(1, "A", "BUY", "1", "100", date(2026, 1, 2)),
                _lot(2, "A", "SELL", "1", "120", date(2026, 1, 3)),
                _lot(3, "B", "BUY", "1", "120", date(2026, 1, 4))]
        assert kpis.peak_net_invested(lots) == D("100")

    def test_same_day_order_does_not_matter(self):
        day = date(2026, 1, 2)
        a = [_lot(1, "A", "SELL", "1", "120", day), _lot(2, "A", "BUY", "2", "100", day)]
        b = list(reversed(a))
        assert kpis.peak_net_invested(a) == kpis.peak_net_invested(b) == D("80")

    def test_an_empty_ledger_has_none(self):
        assert kpis.peak_net_invested([]) == D("0")
        assert kpis.total_return_pct(D("5"), D("0"), D("0")) is None


class TestTotalReturn:
    """F-5's own numbers: realized 900 on a book that cost 1,000."""

    LOTS = [_lot(1, "A", "BUY", "10", "100", date(2026, 1, 2)),
            _lot(2, "A", "SELL", "9", "200", date(2026, 3, 2))]

    def test_after_selling_most_of_the_book_it_does_not_explode(self):
        rows = [{"symbol": "A", "qty": D("1"), "open_cost": D("100"), "realized_pnl": D("900"),
                 "last_price": D("200")}]
        s = kpis.summary(rows, self.LOTS)
        # Lifetime P&L 900 + 100 unrealized on the 1,000 committed: 100%.
        # The old ratio divided by the 100 still open: 1,000%.
        assert s["total_return_pct"] == pytest.approx(D("100"))
        assert s["invested_capital"] == D("1000")

    def test_after_selling_everything_it_is_not_zero(self):
        lots = self.LOTS + [_lot(3, "A", "SELL", "1", "200", date(2026, 4, 2))]
        rows = [{"symbol": "A", "qty": D("0"), "open_cost": D("0"), "realized_pnl": D("1000"),
                 "last_price": None}]
        s = kpis.summary(rows, lots)
        assert s["total_return_pct"] == pytest.approx(D("100"))     # was 0.0%

    def test_income_is_the_separate_with_income_figure(self):
        rows = [{"symbol": "A", "qty": D("1"), "open_cost": D("100"), "realized_pnl": D("900"),
                 "last_price": D("200")}]
        s = kpis.summary(rows, self.LOTS, income=D("50"))
        assert s["total_return_pct"] == pytest.approx(D("100"))
        assert s["total_return_with_income_pct"] == pytest.approx(D("105"))


class TestRollup:
    ROWS = [
        {"symbol": "AAA", "qty": 10.0, "open_cost": 100.0, "realized_pnl": 0.0, "last_price": 12.0},
        {"symbol": "BBB", "qty": 10.0, "open_cost": 100.0, "realized_pnl": 0.0, "last_price": None},
        {"symbol": "CCC", "qty": 0.0, "open_cost": 0.0, "realized_pnl": 90.0, "last_price": None},
    ]

    def test_an_unpriced_position_is_reported_not_lost(self):
        r = kpis.rollup(self.ROWS)
        assert r["market_value"] == D("120")
        assert r["unrealized_pnl"] == D("20")          # not −80
        assert r["priced_cost_basis"] == D("100")
        assert r["unpriced_cost_basis"] == D("100")
        assert r["cost_basis"] == D("200")
        assert r["unpriced_symbols"] == ["BBB"]
        assert r["unrealized_pct"] == pytest.approx(D("20"))   # over the priced cost
        assert r["realized_pnl"] == D("90")

    def test_nan_prices_are_unpriced(self):
        rows = [dict(self.ROWS[0], last_price=float("nan"))]
        assert kpis.rollup(rows)["unpriced_symbols"] == ["AAA"]


class TestCurrencyWarnings:
    def test_each_kind_says_what_was_done(self):
        w = kpis.currency_warnings(
            instruments=[{"symbol": "SAP", "currency": "eur"}, {"symbol": "AAPL", "currency": "USD"},
                         {"symbol": "WATCH", "currency": "GBP"}],
            traded_symbols={"SAP", "AAPL"}, held_symbols={"SAP"},
            income=[{"symbol": "SAP", "amount": D("10"), "currency": "EUR"},
                    {"symbol": "AAPL", "amount": D("5"), "currency": "USD"}],
            cash=[{"account": "IBKR", "cash": D("100"), "currency": "ILS"},
                  {"account": "Schwab", "cash": D("50"), "currency": None}],
        )
        kinds = {(x["kind"], x.get("symbol") or x.get("account") or x.get("currency")): x for x in w}
        assert set(kinds) == {("instrument", "SAP"), ("income", "EUR"), ("cash", "IBKR")}
        assert kinds[("instrument", "SAP")]["treatment"] == "included_unconverted"
        assert kinds[("income", "EUR")]["treatment"] == "excluded"
        assert kinds[("cash", "IBKR")]["treatment"] == "excluded"
        # A watchlist symbol touches no total and is not reported.
        assert all(x.get("symbol") != "WATCH" for x in w)


# ─── three surfaces, one answer ───────────────────────────────

D1, D2 = date(2026, 3, 2), date(2026, 3, 5)
T1 = datetime(2026, 3, 2, 20, tzinfo=timezone.utc)
T_PREV = datetime(2026, 3, 4, 20, tzinfo=timezone.utc)
T_NOW = datetime(2026, 3, 5, 20, tzinfo=timezone.utc)

PARITY_LOTS = [
    _lot(1, "AAA", "BUY", "10", "10", D1),      # priced: 10 × 12 = 120 on a cost of 100
    _lot(2, "BBB", "BUY", "10", "10", D1),      # unpriced: cost 100, no quote ever
    _lot(3, "CCC", "BUY", "10", "10", D1),      # round trip: +90 realized
    _lot(4, "CCC", "SELL", "10", "19", D2),
]
# Non-flat: AAA 10 → 11.5 → 12.
PRICE_ROWS = [
    {"ts": T1, "symbol": "AAA", "last_price": D("10")},
    {"ts": T1, "symbol": "CCC", "last_price": D("10")},
    {"ts": T_PREV, "symbol": "AAA", "last_price": D("11.5")},
    {"ts": T_PREV, "symbol": "CCC", "last_price": D("18")},
    {"ts": T_NOW, "symbol": "AAA", "last_price": D("12")},
]
CASH = [{"account": "IBKR", "cash": D("500"), "ts": T_PREV, "currency": "USD", "note": None}]

EXPECTED = {
    "market_value": 120.0,
    "priced_cost": 100.0,
    "unpriced_cost": 100.0,
    "cost": 200.0,
    "unrealized": 20.0,
    "unrealized_pct": 20.0,
    "realized": 90.0,
    "invested": 300.0,
    "total_return_pct": 110.0 / 300.0 * 100.0,
    "cash": 500.0,
}


def _latest_rows():
    out: dict = {}
    for r in PRICE_ROWS:
        cur = out.get(r["symbol"])
        if cur is None or r["ts"] > cur["ts"]:
            out[r["symbol"]] = dict(r, bid=None, ask=None, source="test")
    return out


def _load(conn, **_kw):
    return ledger_inputs.prepare(PARITY_LOTS, [])


@pytest.fixture
def payload_kpi(monkeypatch):
    import inspect

    from dashboard import payload, queries

    for name, fn in inspect.getmembers(queries, inspect.isfunction):
        if fn.__module__ == queries.__name__ and not name.startswith("_"):
            monkeypatch.setattr(queries, name, lambda conn, *a, **k: [])
    monkeypatch.setattr(queries, "first_snapshot_run_ts", lambda conn: None)
    monkeypatch.setattr(queries, "latest_prices", lambda conn: list(_latest_rows().values()))
    monkeypatch.setattr(queries, "price_history", lambda conn, days=370: [dict(r) for r in PRICE_ROWS])
    monkeypatch.setattr(queries, "prev_close", lambda conn: [
        {"symbol": "AAA", "ts": T_PREV, "last_price": D("11.5")}])
    monkeypatch.setattr(queries, "latest_cash_per_account", lambda conn: [dict(r) for r in CASH])
    monkeypatch.setattr(payload.market_overview, "overview", lambda conn: [])
    monkeypatch.setattr(payload.fd_store, "recent_news", lambda conn, universe, limit=24: [])
    monkeypatch.setattr(payload, "_logo_data_uris", lambda syms: {})
    monkeypatch.setattr(payload.ledger_inputs, "load", _load)
    return payload.build_payload_data(object(), lambda syms: {})["kpi"]


@pytest.fixture
def advisor_snapshot(monkeypatch):
    import advisor

    monkeypatch.setattr(advisor.ledger_inputs, "load", _load)
    monkeypatch.setattr(advisor, "_latest_prices", lambda conn: _latest_rows())
    monkeypatch.setattr(advisor, "_latest_cash", lambda conn: [dict(r) for r in CASH])
    monkeypatch.setattr(advisor, "_income_rows", lambda conn: [])
    monkeypatch.setattr(advisor, "_instrument_currencies", lambda conn: [])
    return advisor.snapshot_portfolio(object())


@pytest.fixture
def exec_totals(monkeypatch):
    import exec_report

    latest = {s: {"ts": r["ts"], "last_price": float(r["last_price"])} for s, r in _latest_rows().items()}
    eod = {T1.date(): {"AAA": 10.0, "CCC": 10.0}, T_PREV.date(): {"AAA": 11.5, "CCC": 18.0},
           T_NOW.date(): {"AAA": 12.0}}
    monkeypatch.setattr(exec_report.ledger_inputs, "load", _load)
    monkeypatch.setattr(exec_report, "_fetch_latest_prices", lambda conn: latest)
    monkeypatch.setattr(exec_report, "_fetch_eod_by_day", lambda conn: eod)
    monkeypatch.setattr(exec_report, "_fetch_cash", lambda conn: [dict(r) for r in CASH])
    monkeypatch.setattr(exec_report, "_fetch_income", lambda conn: [])
    monkeypatch.setattr(exec_report, "_fetch_instruments", lambda conn: [])
    monkeypatch.setattr(exec_report, "_fetch_latest_brief", lambda conn: None)
    monkeypatch.setattr(exec_report, "_fetch_fd_metrics", lambda conn: {})
    monkeypatch.setattr(exec_report, "_fetch_earnings_window", lambda conn, syms, **kw: ([], []))
    return exec_report.gather(object())


class TestPricedAndUnpricedParity:
    def test_dashboard_payload(self, payload_kpi):
        k = payload_kpi
        got = {
            "market_value": k["marketValue"], "priced_cost": k["pricedCostBasis"],
            "unpriced_cost": k["unpricedCostBasis"], "cost": k["costBasis"],
            "unrealized": k["unrealized"], "unrealized_pct": k["unrealizedPct"],
            "realized": k["realized"], "invested": k["investedCapital"],
            "total_return_pct": k["totalReturnPct"], "cash": k["cash"],
        }
        assert got == pytest.approx(EXPECTED, abs=0.01)
        assert k["unpricedSymbols"] == ["BBB"]
        assert k["totalReturnPartial"] is True

    def test_advisor_snapshot(self, advisor_snapshot):
        s = advisor_snapshot
        got = {
            "market_value": s["total_market_value"], "priced_cost": s["priced_cost_basis"],
            "unpriced_cost": s["unpriced_cost_basis"], "cost": s["total_cost"],
            "unrealized": s["total_unrealized"], "unrealized_pct": s["total_unrealized_pct"],
            "realized": s["total_realized"], "invested": s["invested_capital"],
            "total_return_pct": s["total_return_pct"], "cash": s["cash_total"],
        }
        assert got == pytest.approx(EXPECTED, abs=0.01)
        assert s["unpriced_symbols"] == ["BBB"]
        assert s["nav"] == pytest.approx(620.0)
        bbb = next(p for p in s["positions"] if p["symbol"] == "BBB")
        assert bbb["market_value"] is None and bbb["unrealized"] is None

    def test_executive_report(self, exec_totals):
        t = exec_totals.totals
        got = {
            "market_value": t["value"], "priced_cost": t["priced_cost"],
            "unpriced_cost": t["unpriced_cost"], "cost": t["cost"],
            "unrealized": t["unrl"], "unrealized_pct": t["unrl_pct"],
            "realized": t["realized"], "invested": t["invested"],
            "total_return_pct": t["total_return_pct"], "cash": t["cash"],
        }
        assert got == pytest.approx(EXPECTED, abs=0.01)
        assert t["unpriced_symbols"] == ["BBB"]
        bbb = exec_totals.positions.set_index("symbol").loc["BBB"]
        assert bbb["unrealized_pct"] != bbb["unrealized_pct"], "unknown, not +0.00%"

    def test_the_rendered_report_names_the_unpriced_position(self, exec_totals):
        import exec_report_render as render

        html = render._section_notices(exec_totals) + render._section_kpis(exec_totals)
        assert "Unpriced:</strong> BBB" in html
        assert "incl. $100.00 unpriced" in html
        assert "on $300.00 invested" in html
