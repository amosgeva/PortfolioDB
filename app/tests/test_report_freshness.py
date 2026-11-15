"""Stale prices, "as of" and the weekly change, in the three text/HTML reports.

Audit F-8: the daily report valued only the symbols in the one snapshot it
picked, silently dropping a holding that missed that run, while the executive
report valued it at its last quote of any age; and the executive report let a
benchmark's timestamp decide "as of" and the previous session, so a futures
quote on a Sunday made the day change compare Friday's close with itself
(+$0.00). Both reports now follow portfolio_kpis' one rule.

Audit F-9: the weekly change adds manually entered cash to securities; a
purchase made while the cash balance was not re-entered showed up as a gain.
The figure is labelled for what it is and the stale balance is named.

Audit R-5 / R-6: importing reporting_utils opens nothing; the CLIs close their
connections.
"""

from __future__ import annotations

import importlib
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

import ledger_inputs
import portfolio_kpis as kpis
import reporting_tz

D = Decimal
TZ = reporting_tz.tzinfo()

WED, THU, FRI, SAT, SUN = (date(2026, 11, 11) + timedelta(days=i) for i in range(5))


def _close(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 21, 0, tzinfo=timezone.utc)


def _lot(i, sym, side, qty, price, day, account="IBKR"):
    return {"id": i, "symbol": sym, "account": account, "side": side, "trade_date": day,
            "quantity": D(qty), "price": D(price), "fees": D("0")}


LOTS = [_lot(1, "AAA", "BUY", "10", "100", date(2026, 11, 2)),
        _lot(2, "BBB", "BUY", "5", "50", date(2026, 11, 2))]


# ─── the shared rule ──────────────────────────────────────────


class TestFreshnessRule:
    QUOTES = {"AAA": _close(FRI), "BBB": _close(WED), "ES=F": _close(SUN)}

    def test_as_of_is_the_newest_held_quote_not_a_benchmark(self):
        assert kpis.portfolio_as_of(self.QUOTES, ["AAA", "BBB"], {"ES=F"}) == _close(FRI)

    def test_with_nothing_held_benchmarks_still_do_not_decide(self):
        assert kpis.portfolio_as_of(self.QUOTES, [], {"ES=F"}) == _close(FRI)

    def test_a_quote_from_an_earlier_day_is_stale(self):
        stale = kpis.stale_quotes(self.QUOTES, ["AAA", "BBB", "NOQ"], _close(FRI), TZ)
        assert [e["symbol"] for e in stale] == ["BBB"]       # NOQ is unpriced, not stale


# ─── executive report ─────────────────────────────────────────


@pytest.fixture
def exec_report_weekend(monkeypatch):
    """AAA quoted Wed–Fri (101 → 104 → 110), BBB last quoted on Wednesday,
    ES=F (a benchmark) quoted on Sunday — the newest row in the table."""
    import exec_report

    latest = {
        "AAA": {"ts": _close(FRI), "last_price": 110.0},
        "BBB": {"ts": _close(WED), "last_price": 55.0},
        "ES=F": {"ts": _close(SUN), "last_price": 7000.0},
    }
    eod = {
        WED: {"AAA": 101.0, "BBB": 55.0, "ES=F": 6900.0},
        THU: {"AAA": 104.0, "ES=F": 6950.0},
        FRI: {"AAA": 110.0, "ES=F": 6990.0},
        SUN: {"ES=F": 7000.0},
    }
    monkeypatch.setattr(exec_report.ledger_inputs, "load", lambda conn, **kw: ledger_inputs.prepare(LOTS, []))
    monkeypatch.setattr(exec_report, "_fetch_latest_prices", lambda conn: latest)
    monkeypatch.setattr(exec_report, "_fetch_eod_by_day", lambda conn: eod)
    monkeypatch.setattr(exec_report, "_fetch_cash", lambda conn: [])
    monkeypatch.setattr(exec_report, "_fetch_income", lambda conn: [])
    monkeypatch.setattr(exec_report, "_fetch_instruments", lambda conn: [
        {"symbol": "AAA", "currency": "USD", "benchmark": False},
        {"symbol": "BBB", "currency": "USD", "benchmark": False},
        {"symbol": "ES=F", "currency": "USD", "benchmark": True},
    ])
    monkeypatch.setattr(exec_report, "_fetch_latest_brief", lambda conn: None)
    monkeypatch.setattr(exec_report, "_fetch_fd_metrics", lambda conn: {})
    monkeypatch.setattr(exec_report, "_fetch_earnings_window", lambda conn, syms, **kw: ([], []))
    return exec_report.gather(object())


class TestExecutiveReport:
    def test_as_of_is_friday_not_the_sunday_futures_quote(self, exec_report_weekend):
        assert exec_report_weekend.as_of.date() == FRI

    def test_the_day_change_is_fridays_move_not_zero(self, exec_report_weekend):
        t = exec_report_weekend.totals
        # AAA 104 → 110 on 10 shares. Before the fix: today = Sunday, the
        # previous day = Friday, and Friday's close against itself = $0.00.
        assert t["day_chg"] == pytest.approx(60.0)
        assert t["prior_day"] == THU

    def test_a_stale_symbol_is_valued_listed_and_kept_out_of_the_day(self, exec_report_weekend):
        d = exec_report_weekend
        assert [e["symbol"] for e in d.stale] == ["BBB"]
        assert d.totals["value"] == pytest.approx(10 * 110 + 5 * 55)
        assert all(r["symbol"] != "BBB" for r in d.movers["day_top"] + d.movers["day_bot"])

    def test_the_rendered_report_says_which_session_and_which_prices(self, exec_report_weekend):
        import exec_report_render as render

        header = render._section_header(exec_report_weekend)
        assert "on Fri, Nov 13" in header and "today" not in header
        notes = render._section_notices(exec_report_weekend)
        assert "Stale prices:</strong> BBB (2026-11-11)" in notes


# ─── daily report ─────────────────────────────────────────────


@pytest.fixture
def daily(monkeypatch):
    import report_portfolio_db as rep

    class _Conn:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            self.closed = True

    conn = _Conn()
    snap_ts = _close(FRI)
    monkeypatch.setattr(rep, "utf8_stdout", lambda: None)
    monkeypatch.setattr(rep, "load_config", lambda: {})
    monkeypatch.setattr(rep, "connect", lambda cfg: conn)
    monkeypatch.setattr(rep, "collect_fresh_prices", lambda: None)
    monkeypatch.setattr(rep, "fetch_all", lambda conn, q, p=None: [])
    # Friday's snapshot quotes AAA only; BBB missed it and was last quoted on
    # Wednesday; CCC is held and was never quoted.
    monkeypatch.setattr(rep, "get_snapshot", lambda conn, mode: rep.Snapshot(
        ts=snap_ts, prices={"AAA": {"symbol": "AAA", "ts": snap_ts, "last_price": D("110")}}))
    monkeypatch.setattr(rep, "get_quotes_at_or_before", lambda conn, ts: {
        "AAA": {"symbol": "AAA", "ts": _close(THU), "last_price": D("104")},
        "BBB": {"symbol": "BBB", "ts": _close(WED), "last_price": D("55")},
    })
    monkeypatch.setattr(rep, "get_prev_snapshot_map", lambda conn, ts: {
        "AAA": (_close(THU), 104.0), "BBB": (_close(WED) - timedelta(days=1), 50.0)})
    monkeypatch.setattr(rep, "get_day_start_snapshot_map", lambda conn, ts: (
        datetime.combine(FRI, time(16, 15), tzinfo=TZ), {"AAA": 105.0, "BBB": 52.0}))
    lots = LOTS + [_lot(3, "CCC", "BUY", "2", "30", date(2026, 11, 2))]
    monkeypatch.setattr(rep.ledger_inputs, "load", lambda conn, **kw: ledger_inputs.prepare(lots, []))
    monkeypatch.setattr(sys, "argv", ["report_portfolio_db.py", "--mode", "daily"])

    def run():
        rep.main()
        return conn

    return run


class TestDailyReport:
    def test_a_holding_missing_from_the_snapshot_is_valued_not_dropped(self, daily, capsys):
        daily()
        out = capsys.readouterr().out
        # 10 × 110 + 5 × 55. Before the fix BBB vanished: $1,100.00.
        assert "Portfolio Value:   $1,375.00" in out
        assert "Stale prices:      BBB (2026-11-11)" in out

    def test_an_unpriced_holding_is_reported_with_its_cost(self, daily, capsys):
        daily()
        out = capsys.readouterr().out
        assert "Unpriced:          CCC — cost $60.00" in out
        assert "Cost Basis FIFO:   $1,250.00" in out          # priced cost only

    def test_stale_symbols_are_kept_out_of_the_deltas(self, daily, capsys):
        daily()
        out = capsys.readouterr().out
        deltas = [ln for ln in out.splitlines() if ln.startswith("Delta:")]
        assert deltas == ["Delta:             $60.00", "Delta:             $50.00"]   # AAA only
        assert "BBB" not in "".join(ln for ln in out.splitlines() if ln.startswith("  BBB"))

    def test_the_connection_is_closed(self, daily, capsys):
        assert daily().closed


class TestDailyDayStartIgnoresBenchmarks:
    def test_a_benchmark_only_instant_is_not_the_baseline(self, monkeypatch):
        import sqlite3

        import report_portfolio_db as rep

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE instruments (symbol TEXT PRIMARY KEY, benchmark INT NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE price_snapshots (symbol TEXT, ts TEXT, last_price REAL)")
        conn.executemany("INSERT INTO instruments VALUES (?, ?)", [("AAA", 0), ("ES=F", 1)])
        start = datetime.combine(FRI, time(16, 15), tzinfo=TZ).astimezone(timezone.utc)
        rows = [("ES=F", (start + timedelta(minutes=5)).isoformat(), 7000.0),
                ("AAA", (start + timedelta(minutes=20)).isoformat(), 105.0)]
        conn.executemany("INSERT INTO price_snapshots VALUES (?, ?, ?)", rows)

        def fetch_all(_c, sql, params=None):
            args = tuple(p.isoformat() if isinstance(p, datetime) else p for p in (params or ()))
            cur = conn.execute(sql.replace("%s", "?"), args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

        monkeypatch.setattr(rep, "fetch_all", fetch_all)
        ts, prices = rep.get_day_start_snapshot_map(conn, ts=_close(FRI))
        assert ts == rows[1][1]
        assert prices == {"AAA": 105.0}


# ─── weekly report ────────────────────────────────────────────


class TestWeeklyChange:
    def _run(self, monkeypatch, capsys, cash):
        from test_report_weekly_split import BEFORE, END_TS, MON, START_TS, TUE, _lot as wlot, _run

        lots = [wlot(1, "DDD", "BUY", "10", "100", BEFORE), wlot(2, "DDD", "BUY", "5", "105", TUE)]
        prices = {START_TS: {"DDD": D("100")}, END_TS: {"DDD": D("110")}}
        return _run(monkeypatch, capsys, lots, cash=cash, prices=prices), START_TS, MON

    def test_it_is_labelled_a_value_change_with_the_market_pnl_beside_it(self, monkeypatch, capsys):
        from test_report_weekly_split import START_TS

        out, *_ = self._run(monkeypatch, capsys,
                            [{"account": "IBKR", "cash": D("1000"), "ts": START_TS - timedelta(days=1)}])
        line = next(ln for ln in out.splitlines() if ln.startswith("Weekly change:"))
        assert "not a return" in line
        # Securities 1,000 → 1,650 with cash unchanged at 1,000: +650, of
        # which 525 is the Tuesday purchase. Market P&L is 10×10 + 5×5.
        assert "Weekly change:    $650.00" in line
        assert "Market P&L:       $125.00" in out
        assert "Net trade outlay: $525.00" in out

    def test_a_cash_balance_not_reentered_this_week_is_named(self, monkeypatch, capsys):
        from test_report_weekly_split import START_TS

        out, *_ = self._run(monkeypatch, capsys,
                            [{"account": "IBKR", "cash": D("1000"), "ts": START_TS - timedelta(days=9)}])
        warn = next(ln for ln in out.splitlines() if ln.startswith("⚠️ Cash for IBKR"))
        assert "before this week began" in warn and "1 trade(s) this week" in warn

    def test_a_balance_entered_during_the_week_is_not_flagged(self, monkeypatch, capsys):
        from test_report_weekly_split import END_TS, START_TS

        out, *_ = self._run(monkeypatch, capsys, [
            {"account": "IBKR", "cash": D("1000"), "ts": START_TS - timedelta(days=1)},
            {"account": "IBKR", "cash": D("475"), "ts": END_TS - timedelta(hours=2)},
        ])
        assert "⚠️ Cash for" not in out
        assert "Weekly change:    $125.00" in out          # cash now mirrors the purchase

    def test_cash_in_another_currency_is_not_added(self, monkeypatch, capsys):
        from test_report_weekly_split import START_TS

        out, *_ = self._run(monkeypatch, capsys, [
            {"account": "IBKR", "cash": D("1000"), "ts": START_TS - timedelta(days=1), "currency": "USD"},
            {"account": "Leumi", "cash": D("9000"), "ts": START_TS - timedelta(days=1), "currency": "ILS"},
        ])
        assert "End securities:   $1,650.00 | cash: $1,000.00" in out
        assert "Cash in Leumi is entered in ILS" in out


# ─── R-5: nothing at import ───────────────────────────────────


class TestNoDatabaseAtImport:
    def test_reporting_utils_and_the_reports_import_without_resolving_the_zone(self, monkeypatch):
        calls = []

        def boom():
            calls.append(1)
            raise AssertionError("resolved the reporting zone at import")

        monkeypatch.setattr(reporting_tz, "tzinfo", boom)
        monkeypatch.setattr(reporting_tz, "tz_name", boom)
        for name in ("reporting_utils", "report_weekly_db", "report_portfolio_db",
                     "exec_report_queries", "exec_report_analytics", "exec_report_render", "exec_report"):
            importlib.reload(importlib.import_module(name))
        assert calls == []

    def test_the_historical_name_still_resolves(self):
        import reporting_utils

        importlib.reload(reporting_utils)
        assert reporting_utils.IL_TZ == reporting_tz.tzinfo()
        with pytest.raises(AttributeError):
            reporting_utils.NOT_A_THING  # noqa: B018
