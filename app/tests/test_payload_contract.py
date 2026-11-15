"""The 1.8.0 additions to the dashboard payload, through build_payload_data.

* `config` — benchmark, reporting timezone, currency, logo CDN (U-3).
* The benchmark the payload computes against follows the setting: returns
  strip, risk beta and the chart universe (U-3, calculation half).
* `alerts` — persistent price alerts from `price_alerts`, evaluated on read
  against split-restated prices; a missing table is [] and nothing else is
  absorbed (U-1, data half).
* Cash entries carry `as_of` (U-4, data half).
* Money in another currency is not summed as dollars (F-11 / Q-8).
* A 1Y value chart that fell back to the whole history says so (F-14).
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg2.errors
import pytest

import ledger_inputs
from corporate_actions import CorporateAction

D = Decimal
DAYS = [date(2026, 3, 2) + timedelta(days=i) for i in range(30) if (date(2026, 3, 2) + timedelta(days=i)).weekday() < 5]


def _ts(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 20, tzinfo=timezone.utc)


LOTS = [{"id": 1, "symbol": "AAA", "account": "IBKR", "side": "BUY", "trade_date": DAYS[0],
         "quantity": D("10"), "price": D("100"), "fees": D("1")}]

# Non-flat series: AAA drifts up, SPY and QQQ move differently so a benchmark
# column computed from the wrong one is visibly wrong.
PRICES = []
for i, d in enumerate(DAYS):
    PRICES.append({"ts": _ts(d), "symbol": "AAA", "last_price": D(str(100 + i))})
    PRICES.append({"ts": _ts(d), "symbol": "SPY", "last_price": D(str(500 + 2 * i))})
    PRICES.append({"ts": _ts(d), "symbol": "QQQ", "last_price": D(str(400 - 3 * i))})


def _latest():
    out: dict = {}
    for r in PRICES:
        out[r["symbol"]] = dict(r, bid=None, ask=None, source="test")
    return list(out.values())


@pytest.fixture
def build(monkeypatch):
    from dashboard import payload, queries

    state = {"cash": [], "income": [], "instruments": [], "alerts": [], "actions": [], "watch": []}
    for name, fn in inspect.getmembers(queries, inspect.isfunction):
        if fn.__module__ == queries.__name__ and not name.startswith("_"):
            monkeypatch.setattr(queries, name, lambda conn, *a, **k: [])
    monkeypatch.setattr(queries, "first_snapshot_run_ts", lambda conn: None)
    monkeypatch.setattr(queries, "latest_prices", lambda conn: _latest())
    monkeypatch.setattr(queries, "price_history", lambda conn, days=370: [dict(r) for r in PRICES])
    monkeypatch.setattr(queries, "latest_cash_per_account", lambda conn: state["cash"])
    monkeypatch.setattr(queries, "income_rows", lambda conn: state["income"])
    monkeypatch.setattr(queries, "instrument_attrs", lambda conn: state["instruments"])
    monkeypatch.setattr(queries, "price_alerts", lambda conn: state["alerts"])
    monkeypatch.setattr(queries, "watchlist_symbols", lambda conn: [{"symbol": s} for s in state["watch"]])
    monkeypatch.setattr(payload.market_overview, "overview", lambda conn: [])
    monkeypatch.setattr(payload.fd_store, "recent_news", lambda conn, universe, limit=24: [])
    monkeypatch.setattr(payload, "_logo_data_uris", lambda syms: {})
    monkeypatch.setattr(payload.ledger_inputs, "load",
                        lambda conn, **kw: ledger_inputs.prepare(LOTS, state["actions"]))

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            n = _ts(DAYS[-1]) + timedelta(hours=1)
            return n.astimezone(tz) if tz else n.replace(tzinfo=None)

    monkeypatch.setattr(payload, "datetime", _Clock)
    for var in ("PORTFOLIODB_BENCHMARK_SYMBOL", "PORTFOLIODB_BENCHMARK_LABEL", "PORTFOLIODB_LOGO_CDN"):
        monkeypatch.delenv(var, raising=False)

    def run():
        return payload.build_payload_data(object(), lambda syms: {})

    run.state = state
    return run


class TestConfig:
    def test_defaults(self, build):
        cfg = build()["config"]
        assert cfg == {"benchmark_symbol": "SPY", "benchmark_label": "SPY",
                       "reporting_tz": "Asia/Jerusalem", "currency": "USD", "logo_cdn": False}

    @pytest.mark.parametrize("raw, expected", [("1", True), ("TRUE", True), ("yes", True), (" on ", True),
                                               ("0", False), ("", False), ("off", False)])
    def test_logo_cdn(self, build, monkeypatch, raw, expected):
        monkeypatch.setenv("PORTFOLIODB_LOGO_CDN", raw)
        assert build()["config"]["logo_cdn"] is expected

    def test_label_override(self, build, monkeypatch):
        monkeypatch.setenv("PORTFOLIODB_BENCHMARK_SYMBOL", "qqq")
        monkeypatch.setenv("PORTFOLIODB_BENCHMARK_LABEL", "Nasdaq 100")
        cfg = build()["config"]
        assert (cfg["benchmark_symbol"], cfg["benchmark_label"]) == ("QQQ", "Nasdaq 100")


class TestBenchmarkFollowsTheSetting:
    """U-3: the server computed against a hard-coded SPY whatever was set."""

    def _bench_max(self, data):
        return next(p["benchmark"] for p in data["returns"]["periods"] if p["period"] == "MAX")

    def test_default_is_spy(self, build):
        data = build()
        assert data["returns"]["benchmarkSymbol"] == "SPY"
        assert self._bench_max(data) == pytest.approx(((500 + 2 * (len(DAYS) - 1)) / 500 - 1) * 100, abs=0.01)
        assert data["risk"]["benchmark"] == "SPY"

    def test_setting_it_moves_every_calculation(self, build, monkeypatch):
        monkeypatch.setenv("PORTFOLIODB_BENCHMARK_SYMBOL", "QQQ")
        data = build()
        assert data["returns"]["benchmarkSymbol"] == "QQQ"
        assert data["returns"]["benchmark"].startswith("QQQ ")
        assert self._bench_max(data) == pytest.approx(((400 - 3 * (len(DAYS) - 1)) / 400 - 1) * 100, abs=0.01)
        assert data["risk"]["benchmark"] == "QQQ"
        assert "QQQ" in data["priceHist"]

    def test_an_uncollected_benchmark_is_reported_unavailable(self, build, monkeypatch):
        monkeypatch.setenv("PORTFOLIODB_BENCHMARK_SYMBOL", "IWM")
        data = build()
        assert data["returns"]["benchmarkAvailable"] is False
        assert all(p["benchmark"] is None for p in data["returns"]["periods"])

    def test_the_mcp_resolves_the_same_symbol(self, monkeypatch):
        import benchmark

        monkeypatch.setenv("PORTFOLIODB_BENCHMARK_SYMBOL", "  vti ")
        assert benchmark.benchmark_symbol() == "VTI"
        assert benchmark.benchmark_symbol("dia") == "DIA"
        monkeypatch.setenv("PORTFOLIODB_BENCHMARK_SYMBOL", "")
        assert benchmark.benchmark_symbol() == "SPY"


class TestAlerts:
    def _alert(self, i, sym, direction, level, created=None, note=None):
        return {"id": i, "symbol": sym, "direction": direction, "level": D(level), "note": note,
                "created_at": created or _ts(DAYS[0])}

    def test_evaluated_against_the_latest_quote(self, build):
        last_aaa = 100 + len(DAYS) - 1                     # 121
        build.state["alerts"] = [
            self._alert(1, "AAA", "above", "120", note="trim"),
            self._alert(2, "AAA", "above", "130"),
            self._alert(3, "AAA", "below", "125"),
            self._alert(4, "ZZZ", "below", "10"),
        ]
        alerts = build()["alerts"]
        by_id = {a["id"]: a for a in alerts}
        assert by_id[1]["triggered"] is True and by_id[1]["note"] == "trim"
        assert by_id[2]["triggered"] is False
        assert by_id[3]["triggered"] is True
        assert by_id[1]["last_price"] == pytest.approx(last_aaa)
        assert by_id[1]["last_price_ts"] == _ts(DAYS[-1]).isoformat()
        # No quote on file: unknown, not "not triggered".
        assert by_id[4]["triggered"] is None and by_id[4]["last_price"] is None
        assert set(alerts[0]) == {"id", "symbol", "direction", "level", "effective_level", "note",
                                  "created_at", "last_price", "last_price_ts", "triggered"}

    def test_a_split_after_the_alert_restates_its_level(self, build):
        """An alert set at 150 before a 2:1 split is an alert at 75 after it.
        Unrestated, a "below 150" alert fires on every post-split quote."""
        build.state["actions"] = [CorporateAction(symbol="AAA", kind="SPLIT", ex_date=DAYS[-1] + timedelta(days=1),
                                                  ratio=D("2"))]
        build.state["alerts"] = [self._alert(1, "AAA", "below", "150", created=_ts(DAYS[0]))]
        a = build()["alerts"][0]
        assert a["level"] == 150.0
        assert a["effective_level"] == pytest.approx(75.0)
        # Latest quote (121, pre-split) is restated to 60.5: below 75.
        assert a["last_price"] == pytest.approx(60.5)
        assert a["triggered"] is True

    def test_missing_table_reads_as_no_alerts_and_nothing_else_is_absorbed(self, monkeypatch):
        from dashboard import queries

        class Conn:
            rolled_back = 0

            def rollback(self):
                self.rolled_back += 1

        def missing(conn, sql, params=None):
            raise psycopg2.errors.UndefinedTable('relation "price_alerts" does not exist')

        conn = Conn()
        monkeypatch.setattr(queries, "fetch_all", missing)
        assert queries.price_alerts(conn) == []
        assert conn.rolled_back == 1

        def denied(conn, sql, params=None):
            raise psycopg2.errors.InsufficientPrivilege("permission denied for table price_alerts")

        monkeypatch.setattr(queries, "fetch_all", denied)
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            queries.price_alerts(conn)
        assert conn.rolled_back == 2


class TestCash:
    def test_each_entry_says_when_it_was_entered(self, build):
        build.state["cash"] = [{"account": "IBKR", "cash": D("1000"), "ts": _ts(DAYS[3]), "currency": "USD"}]
        k = build()["kpi"]
        assert k["cash"] == 1000.0
        entry = k["cashAccounts"][0]
        assert entry["as_of"] == _ts(DAYS[3]).isoformat()
        assert entry["asOf"] == entry["as_of"]          # pre-1.8 name kept
        assert entry["currency"] == "USD" and entry["inTotal"] is True


class TestOtherCurrencies:
    def test_foreign_income_and_cash_are_excluded_and_listed(self, build):
        build.state["income"] = [
            {"symbol": "AAA", "pay_date": DAYS[5], "amount": D("12"), "currency": "USD"},
            {"symbol": "AAA", "pay_date": DAYS[6], "amount": D("500"), "currency": "ILS"},
        ]
        build.state["cash"] = [
            {"account": "IBKR", "cash": D("1000"), "ts": _ts(DAYS[3]), "currency": "USD"},
            {"account": "Leumi", "cash": D("9000"), "ts": _ts(DAYS[3]), "currency": "ILS"},
        ]
        build.state["instruments"] = [{"symbol": "AAA", "asset_type": "stock", "currency": "CAD",
                                       "country": "CA", "sector": "Tech"}]
        data = build()
        k = data["kpi"]
        assert k["dividends"] == 12.0                   # not 512
        assert k["cash"] == 1000.0                      # not 10,000
        assert data["cash"] == 1000.0
        leumi = next(e for e in k["cashAccounts"] if e["account"] == "Leumi")
        assert leumi["inTotal"] is False and leumi["currency"] == "ILS"
        kinds = {w["kind"]: w for w in data["currencyWarnings"]}
        assert kinds["income"]["currency"] == "ILS" and kinds["income"]["amount"] == 500.0
        assert kinds["cash"]["account"] == "Leumi"
        assert kinds["instrument"]["symbol"] == "AAA" and kinds["instrument"]["held"] is True

    def test_foreign_income_stays_out_of_the_time_weighted_return(self, build):
        base = build()
        build.state["income"] = [{"symbol": "AAA", "pay_date": DAYS[5], "amount": D("500"), "currency": "EUR"}]
        with_eur = build()
        assert with_eur["returns"]["periods"] == base["returns"]["periods"]

    def test_all_usd_has_no_warnings(self, build):
        assert build()["currencyWarnings"] == []


class TestValueChartBasis:
    def test_1y_says_when_it_is_the_whole_history(self, build, monkeypatch):
        data = build()
        assert data["pvBasis"]["1Y"] == "trailing_365d"
        # Every snapshot older than a year: the 1Y window is empty and the
        # chart falls back to everything — labelled, not passed off as a year.
        from dashboard import payload

        class _Later(datetime):
            @classmethod
            def now(cls, tz=None):
                n = _ts(DAYS[-1]) + timedelta(days=500)
                return n.astimezone(tz) if tz else n.replace(tzinfo=None)

        monkeypatch.setattr(payload, "datetime", _Later)
        assert build()["pvBasis"]["1Y"] == "all_history"
