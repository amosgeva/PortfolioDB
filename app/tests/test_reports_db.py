"""The daily, weekly and executive reports against a real Postgres.

QA of PR #123 found two gaps the fakes could not see, both reproduced here
through the reports' own entry points with real SQL:

* B1 (F-8): the daily report chose its instant and its day-start baseline
  from every non-benchmark quote, so an unheld watchlist symbol (or one sold
  last month that is still collected) quoted on a Sunday made the report's
  day Sunday, labelled the holding stale and printed a $0.00 delta, while the
  executive report correctly said Friday, +$60. Both now take the instant from
  the HELD symbols (portfolio_kpis.portfolio_as_of).
* B2 (F-11): the weekly report checked only cash for another currency; a
  security registered in CAD was summed as dollars without a word.

Each test shadows the ledger tables with session TEMP tables (`LIKE public.x`),
so the database is never written; the connection is rolled back and closed.
Skips without a database unless PORTFOLIODB_TESTS_REQUIRE_DB is set.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

import exec_report
import report_portfolio_db as daily
import report_weekly_db as weekly
from db import connect_for_tests

TABLES = ("instruments", "lots", "price_snapshots", "income", "cash_snapshots", "corporate_actions")
FRI_CLOSE = datetime(2026, 11, 13, 21, 0, tzinfo=timezone.utc)


class _Borrowed:
    """The test's connection, handed to a report's main() without letting it
    close it (the test rolls back and closes)."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def _clock(when: datetime):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when.replace(tzinfo=None)
    return Clock


@pytest.fixture
def ledger():
    """Ten AAA (registered in CAD) quoted 100 → 104 → 110 Mon/Thu/Fri; WATCH
    (watchlist, never held) quoted on Sunday; SOLD bought and sold, still
    quoted on Sunday; a zero AAA quote on Saturday (Yahoo's off-hours 0.0)."""
    c = connect_for_tests(skip=pytest.skip, fail=pytest.fail)
    try:
        with c.cursor() as cur:
            for t in TABLES:
                cur.execute(f"CREATE TEMP TABLE {t} (LIKE public.{t} INCLUDING DEFAULTS)")
            cur.execute(
                "INSERT INTO instruments(symbol, currency, benchmark, watchlist) VALUES "
                "('AAA','CAD',false,false),('WATCH','USD',false,true),"
                "('SOLD','USD',false,false),('ES=F','USD',true,false)"
            )
            cur.execute(
                "INSERT INTO lots(id, symbol, account, side, trade_date, quantity, price, fees) VALUES "
                "(10001,'AAA','QA','BUY','2026-11-02',10,100,0),"
                "(10002,'SOLD','QA','BUY','2026-10-01',5,20,0),"
                "(10003,'SOLD','QA','SELL','2026-10-20',5,25,0)"
            )
            cur.execute(
                "INSERT INTO price_snapshots(symbol, ts, last_price) VALUES "
                "('AAA','2026-11-09 21:00+00',100),"
                "('AAA','2026-11-12 21:00+00',104),"
                "('AAA','2026-11-13 21:00+00',110),"
                "('AAA','2026-11-14 12:00+00',0),"
                "('WATCH','2026-11-15 08:00+00',200),"
                "('SOLD','2026-11-15 09:00+00',30),"
                "('ES=F','2026-11-15 23:00+00',7000)"
            )
        yield c
    finally:
        c.rollback()
        c.close()


def _run_daily(conn, monkeypatch, capsys, mode="daily"):
    monkeypatch.setattr(daily, "connect", lambda cfg: _Borrowed(conn))
    monkeypatch.setattr(daily, "load_config", lambda: None)
    monkeypatch.setattr(daily, "utf8_stdout", lambda: None)
    monkeypatch.setattr(daily, "collect_fresh_prices", lambda: None)   # never start the collector
    monkeypatch.setattr(sys, "argv", ["report_portfolio_db.py", "--mode", mode])
    daily.main()
    return capsys.readouterr().out


class TestDailyInstantFollowsTheHoldings:
    def test_the_daily_and_the_executive_report_read_the_same_instant(self, ledger):
        executive = exec_report.gather(ledger)
        snapshot = daily.get_snapshot(ledger, mode="daily")
        assert executive.totals["day_chg"] == pytest.approx(60.0)
        assert snapshot.ts == executive.as_of == FRI_CLOSE

    def test_watchlist_and_sold_quotes_do_not_move_the_daily_report(self, ledger, monkeypatch, capsys):
        out = _run_daily(ledger, monkeypatch, capsys)
        assert "Data: 2026-11-13 23:00" in out           # Friday's close (Asia/Jerusalem in tests)
        assert "Portfolio Value:   $1,100.00" in out
        assert "Stale prices" not in out
        assert "Delta:             $60.00" in out        # 10 × (110 − 104), not $0.00

    def test_the_eod_cutoff_still_applies(self, ledger, monkeypatch):
        with ledger.cursor() as cur:   # a later Friday quote, after 23:05 Jerusalem
            cur.execute("INSERT INTO price_snapshots(symbol, ts, last_price) VALUES "
                        "('AAA','2026-11-13 21:30+00',111)")
        monkeypatch.setattr(daily, "datetime", _clock(datetime(2026, 11, 13, 20, 30, tzinfo=timezone.utc)))
        assert daily.get_snapshot(ledger, mode="eod").ts == FRI_CLOSE

    def test_the_day_start_baseline_is_a_held_quote(self, ledger):
        with ledger.cursor() as cur:   # WATCH quoted first after 16:15 Jerusalem on Friday
            cur.execute("INSERT INTO price_snapshots(symbol, ts, last_price) VALUES "
                        "('WATCH','2026-11-13 14:20+00',199),('AAA','2026-11-13 15:00+00',105)")
        ts, prices = daily.get_day_start_snapshot_map(ledger, ts=FRI_CLOSE)
        assert ts == datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
        assert prices == {"AAA": 105.0}

    def test_a_zero_quote_is_not_a_price(self, ledger):
        # Saturday's 0.0 is newer than Friday's 110 for AAA, and must decide
        # neither the instant nor the value.
        import advisor
        from dashboard import queries

        assert daily.get_snapshot(ledger, mode="daily").prices["AAA"]["last_price"] == 110
        assert exec_report.gather(ledger).totals["value"] == pytest.approx(1100.0)
        # The dashboard's and the Advisor's latest-price reads skip it too.
        assert {r["symbol"]: r["last_price"] for r in queries.latest_prices(ledger)}["AAA"] == 110
        assert advisor._latest_prices(ledger)["AAA"]["last_price"] == 110
        assert all(r["last_price"] > 0 for r in queries.second_latest_prices(ledger))


class TestWeeklyForeignInstrument:
    def test_a_cad_security_is_flagged_not_silently_summed(self, ledger, monkeypatch, capsys):
        monkeypatch.setattr(weekly, "datetime", _clock(datetime(2026, 11, 15, 10, tzinfo=timezone.utc)))
        monkeypatch.setattr(weekly, "connect", lambda cfg: _Borrowed(ledger))
        monkeypatch.setattr(weekly, "load_config", lambda: None)
        monkeypatch.setattr(weekly, "utf8_stdout", lambda: None)
        weekly.main()
        out = capsys.readouterr().out
        assert "End securities:   $1,100.00" in out
        assert "AAA is registered in CAD" in out and "without conversion" in out
