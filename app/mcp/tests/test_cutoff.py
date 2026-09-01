"""Cutoff resolution and the provenance block.

The property that matters: a cutoff resolved once and passed to several
services makes them agree. Without it each service independently reads "the
latest price", so a snapshot landing mid-request leaves market value on the new
prices and daily change on the old ones — with nothing in either response to
show the two disagree.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


from app.mcp.services import cutoff as cutoff_service
from app.mcp.services.cutoff import Cutoff

JER = ZoneInfo("Asia/Jerusalem")


class TestToInstant:
    def test_none_resolves_to_now(self):
        before = datetime.now(timezone.utc)
        got = cutoff_service.to_instant(None)
        after = datetime.now(timezone.utc)
        assert before <= got <= after

    def test_aware_datetime_is_converted_to_utc(self):
        ts = datetime(2026, 8, 12, 16, 5, tzinfo=JER)
        got = cutoff_service.to_instant(ts)
        assert got.tzinfo is timezone.utc
        assert got == ts

    def test_naive_datetime_is_read_as_reporting_local(self):
        """Not UTC — a bare '16:05' from this app means Jerusalem, and reading
        it as UTC would shift the cutoff by three hours."""
        got = cutoff_service.to_instant(datetime(2026, 8, 12, 16, 5))
        assert got == datetime(2026, 8, 12, 16, 5, tzinfo=JER)

    def test_a_date_means_the_end_of_that_day(self):
        """Pinning a date to midnight would exclude everything that happened on
        the requested day, which is the opposite of what 'as of the 12th' means."""
        got = cutoff_service.to_instant(date(2026, 8, 12))
        local = got.astimezone(JER)
        assert local.date() == date(2026, 8, 12)
        assert (local.hour, local.minute) == (23, 59)

    def test_a_date_includes_a_late_observation_that_day(self):
        cutoff_ts = cutoff_service.to_instant(date(2026, 8, 12))
        late_trade = datetime(2026, 8, 12, 23, 13, tzinfo=JER)
        assert late_trade <= cutoff_ts


class TestCutoffHelpers:
    def _cutoff(self) -> Cutoff:
        ts = datetime(2026, 8, 12, 20, 0, tzinfo=timezone.utc)
        return Cutoff(
            ts=ts,
            trade_date=ts.astimezone(JER).date(),
            price_ts_by_symbol={
                "NVDA": datetime(2026, 8, 12, 19, 55, tzinfo=timezone.utc),
                "PRIM": datetime(2026, 8, 5, 19, 55, tzinfo=timezone.utc),
            },
            coverage_start=date(2025, 9, 22),
            coverage_end=date(2026, 8, 12),
        )

    def test_local_renders_in_the_reporting_timezone(self):
        assert self._cutoff().local.tzinfo == JER

    def test_price_ts_lookup_is_case_insensitive(self):
        assert self._cutoff().price_ts("nvda") is not None

    def test_staleness_is_measured_against_the_cutoff_not_now(self):
        c = self._cutoff()
        assert c.is_stale_for("NVDA", max_age_hours=24) is False
        assert c.is_stale_for("PRIM", max_age_hours=24) is True

    def test_staleness_is_none_when_there_is_no_observation(self):
        """Absent is not the same as stale, and neither is the same as fresh."""
        assert self._cutoff().is_stale_for("MSFT", max_age_hours=24) is None


class TestMeta:
    def test_carries_provenance_the_caller_would_otherwise_assume(self):
        c = Cutoff(
            ts=datetime(2026, 8, 12, 13, 5, tzinfo=timezone.utc),
            trade_date=date(2026, 8, 12),
            coverage_start=date(2025, 9, 22),
            coverage_end=date(2026, 8, 12),
        )
        m = cutoff_service.meta(c, method="fifo")

        assert m["as_of"] == "2026-08-12T13:05:00+00:00"
        assert m["timezone"] == cutoff_service.REPORTING_TZ
        assert m["reporting_currency"] == "USD"
        assert m["cost_basis_method"] == "fifo"
        assert m["coverage_start"] == "2025-09-22"
        assert m["coverage_end"] == "2026-08-12"
        assert m["schema_version"] == cutoff_service.SCHEMA_VERSION
        assert m["app_version"]

    def test_method_is_omitted_when_not_applicable(self):
        m = cutoff_service.meta(Cutoff(ts=datetime.now(timezone.utc)))
        assert "cost_basis_method" not in m

    def test_extra_fields_are_merged(self):
        m = cutoff_service.meta(
            Cutoff(ts=datetime.now(timezone.utc)), benchmark_symbol="SPY"
        )
        assert m["benchmark_symbol"] == "SPY"

    def test_null_coverage_is_reported_as_null_not_omitted(self):
        m = cutoff_service.meta(Cutoff(ts=datetime.now(timezone.utc)))
        assert m["coverage_start"] is None
        assert m["coverage_end"] is None


class TestAppVersion:
    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("PORTFOLIODB_APP_VERSION", "v9.9.9-test")
        cutoff_service.app_version.cache_clear()
        try:
            assert cutoff_service.app_version() == "v9.9.9-test"
        finally:
            cutoff_service.app_version.cache_clear()

    def test_always_returns_something(self):
        cutoff_service.app_version.cache_clear()
        try:
            assert isinstance(cutoff_service.app_version(), str)
            assert cutoff_service.app_version()
        finally:
            cutoff_service.app_version.cache_clear()

    def test_falls_back_to_unknown_without_env_or_git(self, monkeypatch):
        """A deployed tree with no .git reports honestly rather than failing."""
        monkeypatch.delenv("PORTFOLIODB_APP_VERSION", raising=False)
        monkeypatch.setattr(cutoff_service.version, "git_head_sha", lambda: None)
        cutoff_service.app_version.cache_clear()
        try:
            assert cutoff_service.app_version() == "unknown"
        finally:
            cutoff_service.app_version.cache_clear()


class TestResolve:
    """resolve() against a faked DB, checking what it records."""

    def _patch_db(
        self, monkeypatch, price_rows, cash_rows, first_ts, inflight=None
    ):
        from contextlib import contextmanager

        class Cur:
            def __init__(self): self.result = None
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, sql, params=None):
                if "snapshot_runs" in sql:
                    self.result = [inflight] if inflight else []
                elif "price_snapshots" in sql and "MIN" in sql:
                    self.result = [(first_ts,)]
                elif "price_snapshots" in sql:
                    self.result = price_rows
                else:
                    self.result = cash_rows
            def fetchall(self): return self.result
            def fetchone(self): return self.result[0] if self.result else None

        class Conn:
            def cursor(self, *a, **kw): return Cur()

        @contextmanager
        def fake_conn():
            yield Conn()

        monkeypatch.setattr(cutoff_service, "get_conn", fake_conn)

    def test_records_the_observation_used_per_symbol(self, env_token, monkeypatch):
        seen = datetime(2026, 8, 12, 19, 55, tzinfo=timezone.utc)
        self._patch_db(
            monkeypatch,
            price_rows=[("NVDA", seen), ("SPY", seen)],
            cash_rows=[("IBKR", seen)],
            first_ts=datetime(2025, 9, 22, 12, 0, tzinfo=timezone.utc),
        )
        c = cutoff_service.resolve(datetime(2026, 8, 12, 20, 0, tzinfo=timezone.utc))

        assert c.price_ts_by_symbol == {"NVDA": seen, "SPY": seen}
        assert c.cash_ts_by_account == {"IBKR": seen}
        assert c.coverage_start == date(2025, 9, 22)
        assert c.coverage_end == date(2026, 8, 12)

    def test_trade_date_uses_local_not_utc_day(self, env_token, monkeypatch):
        """22:30 UTC on the 11th is already the 12th in Jerusalem, and a trade
        booked then belongs to the 12th."""
        self._patch_db(monkeypatch, [], [], None)
        c = cutoff_service.resolve(datetime(2026, 8, 11, 22, 30, tzinfo=timezone.utc))
        assert c.trade_date == date(2026, 8, 12)

    def test_empty_database_yields_null_coverage(self, env_token, monkeypatch):
        self._patch_db(monkeypatch, [], [], None)
        c = cutoff_service.resolve()
        assert c.coverage_start is None
        assert c.coverage_end is None
        assert c.price_ts_by_symbol == {}


class TestInflightRun:
    """A snapshot run mid-write must not be read half-committed.

    Measured on the live collector: a run stamps all 12 rows with its start
    time, then commits them over 7-12 seconds as each yfinance call returns.
    A cutoff inside that window sees a different subset of the run depending on
    exactly when each service happens to query — so two services resolving
    microseconds apart return different portfolio values, with nothing in
    either response to indicate disagreement.
    """

    _patch_db = TestResolve._patch_db

    def test_cutoff_steps_back_behind_a_run_in_flight(self, env_token, monkeypatch):
        run_start = datetime(2026, 8, 12, 16, 33, 2, 918375, tzinfo=timezone.utc)
        requested = datetime(2026, 8, 12, 16, 33, 3, 394167, tzinfo=timezone.utc)
        self._patch_db(
            monkeypatch, [], [], None, inflight=(4998, run_start)
        )
        c = cutoff_service.resolve(requested)

        assert c.ts < run_start
        assert c.requested_ts == requested
        assert c.inflight_run_id == 4998
        assert c.was_pulled_back is True

    def test_the_adjustment_is_reported_not_hidden(self, env_token, monkeypatch):
        run_start = datetime(2026, 8, 12, 16, 33, 2, 918375, tzinfo=timezone.utc)
        requested = datetime(2026, 8, 12, 16, 33, 3, 394167, tzinfo=timezone.utc)
        self._patch_db(monkeypatch, [], [], None, inflight=(4998, run_start))

        m = cutoff_service.meta(cutoff_service.resolve(requested))
        assert m["as_of_requested"] == requested.isoformat()
        assert m["as_of_adjusted_reason"] == "snapshot_run_in_flight"
        assert m["inflight_run_id"] == 4998

    def test_no_run_in_flight_leaves_the_cutoff_alone(self, env_token, monkeypatch):
        requested = datetime(2026, 8, 12, 16, 40, tzinfo=timezone.utc)
        self._patch_db(monkeypatch, [], [], None, inflight=None)
        c = cutoff_service.resolve(requested)

        assert c.ts == requested
        assert c.inflight_run_id is None
        assert c.was_pulled_back is False

    def test_untouched_cutoff_carries_no_adjustment_fields(
        self, env_token, monkeypatch
    ):
        self._patch_db(monkeypatch, [], [], None, inflight=None)
        m = cutoff_service.meta(cutoff_service.resolve())
        assert "as_of_adjusted_reason" not in m
        assert "inflight_run_id" not in m
