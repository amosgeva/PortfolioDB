"""MCP correctness fixes, through the services with the SQL checked.

The fake cursor records each statement, checks placeholders against
parameters, and (where a queued response names a fragment) asserts the
statement it answers contains it — so these tests fail on wrong SQL, not only
on wrong arithmetic (audit T-3). Live-database counterparts are in
test_live_surface.py.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from app.mcp.services.cutoff import Cutoff

NOW = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
LOT_COLS = ["id", "symbol", "account", "side", "trade_date", "quantity", "price", "fees"]


def cutoff() -> Cutoff:
    return Cutoff(ts=NOW, trade_date=NOW.date(), coverage_start=date(2026, 1, 1))


# ────────────────────────── X-8: the '(none)' label as a filter ──────────────────────────


def test_x8_none_label_selects_lots_without_an_account(fake_db):
    from app.mcp.services import activity

    fake_db(responses=[(LOT_COLS + ["notes", "created_at"], [], "account IS NULL")])
    activity.lots(account="(none)", limit=5)
    sql, params = fake_db.executed()[0]
    assert "account = %s" not in sql
    assert "(none)" not in (params or [])


def test_x8_named_account_is_still_bound(fake_db):
    from app.mcp.services import activity

    fake_db(responses=[(LOT_COLS + ["notes", "created_at"], [], "account = %s")])
    activity.lots(account="IBKR", limit=5)
    assert "IBKR" in fake_db.executed()[0][1]


@pytest.mark.parametrize("reader", ["positions", "pnl", "fees", "income", "returns_lots", "returns_income"])
def test_x8_every_account_filter_understands_none(fake_db, reader):
    """The same rule in every service that filters lots or income by account."""
    from app.mcp.services import fees, income, pnl, positions, returns

    fake_db(responses=[([], [])], cycle=True)
    calls = {
        "positions": lambda: positions.positions_dataframe("fifo", account="(none)"),
        "pnl": lambda: pnl.realized_pnl("fifo", account="(none)", group_by="none"),
        "fees": lambda: fees.fees_summary(account="(none)"),
        "income": lambda: income.income_summary(account="(none)"),
        "returns_lots": lambda: returns._fetch_lots(cutoff(), account="(none)"),
        "returns_income": lambda: returns._fetch_dividends(cutoff(), account="(none)"),
    }
    calls[reader]()
    filtered = [s for s, _ in fake_db.executed() if " account" in s and ("lots" in s or "income" in s)]
    assert filtered, "no lots/income query ran"
    assert any("account IS NULL" in s for s in filtered)
    assert not any("account = %s" in s for s in filtered)


# ────────────────────────── X-1 / Q-3: account-scoped XIRR ──────────────────────────


def test_x1_q3_account_xirr_reads_that_accounts_lots_and_income(fake_db, monkeypatch):
    """The lots loader now selects and filters account in SQL, and the income
    loader is scoped too. Before: lots were filtered on a key the loader never
    selected (zero lots) and income was every account's."""
    import xirr as xirr_module

    from app.mcp.services import review

    captured = {}
    monkeypatch.setattr(xirr_module, "from_ledger",
                        lambda **kw: captured.update(kw) or {"rate_pct": 1.0})
    fake_db(responses=[
        (LOT_COLS, [(1, "AAA", "IBKR", "BUY", date(2026, 1, 5), 10, 100, 0)],
         ("FROM lots", "account = %s")),
        (["pay_date", "amount", "account"], [(date(2026, 3, 1), 7.0, "IBKR")],
         ("FROM income", "account = %s")),
    ])
    review._xirr(cutoff(), {"invested_market_value": 1200.0}, "IBKR")
    assert len(captured["lots"]) == 1 and captured["lots"][0]["account"] == "IBKR"
    assert captured["income"] == [{"pay_date": date(2026, 3, 1), "amount": 7.0}]
    lot_sql, lot_params = fake_db.executed()[0]
    inc_sql, inc_params = fake_db.executed()[1]
    assert "IBKR" in lot_params and "IBKR" in inc_params


# ────────────────────────── X-2: every review section scoped or labelled ──────────────────────────


@pytest.fixture
def review_recorder(env_token, fake_db, monkeypatch):
    """Every collaborator stubbed; records the account each one was given."""
    from app.mcp.services import (
        analytics, data_quality, fees, income, pnl, positions, returns, review,
    )

    seen: dict[str, object] = {}

    def rec(name, value):
        def fn(*a, **kw):
            seen.setdefault(name, []).append(kw.get("account", "<not passed>"))
            return value
        return fn

    pos = [{"symbol": "AAA", "qty": 1.0, "market_value": 100.0, "open_cost": 90.0, "weight_pct": 100.0}]
    monkeypatch.setattr(positions, "current_positions", rec("positions", pos))
    monkeypatch.setattr(positions, "positions_summary", rec("totals", {
        "market_value": 100.0, "cost_basis": 90.0, "unrealized_pnl": 10.0,
        "realized_pnl": 0.0}))
    monkeypatch.setattr(income, "income_summary", rec("income", {
        "total_income": 0.0, "total_tax_withheld": 0.0, "count": 0}))
    monkeypatch.setattr(fees, "fees_summary", rec("fees", {
        "total_fees": 0.0, "fee_count": 0, "fee_drag_pct": None, "by_symbol": []}))
    monkeypatch.setattr(returns, "period_returns", rec("twr", {
        "basis": "twr", "periods": {}, "observations": 0, "coverage": {}}))
    monkeypatch.setattr(returns, "volatility", rec("volatility", {}))
    monkeypatch.setattr(returns, "benchmark_comparison", rec("benchmark", {"status": "ok"}))
    monkeypatch.setattr(returns, "_fetch_lots", rec("xirr_lots", []))
    monkeypatch.setattr(returns, "_fetch_dividends", rec("xirr_income", []))
    monkeypatch.setattr(analytics, "drawdown_stats", rec("drawdown", {
        "max_drawdown_pct": 0.0, "current_drawdown_pct": 0.0}))
    monkeypatch.setattr(analytics, "correlation_matrix", rec("correlation", {
        "symbols": [], "window": "3m", "observations": 0, "matrix": {}, "pairs": [],
        "diversifiers": [], "clusters": {}}))
    monkeypatch.setattr(analytics, "concentration", rec("concentration", {
        "single_largest_pct": None, "top_n": 10, "top_n_share_pct": None, "hhi": None,
        "effective_n": None, "total_positions": 0, "rows": []}))
    monkeypatch.setattr(analytics, "allocation_by", rec("allocation", {"rows": []}))
    monkeypatch.setattr(analytics, "stress_scenarios", rec("scenarios", {"scenarios": []}))
    monkeypatch.setattr(pnl, "trade_quality", rec("trade_quality", {}))
    monkeypatch.setattr(data_quality, "portfolio_data_quality", rec("data_quality", {
        "overall_status": "complete", "overall_explanation": "", "counts": {},
        "collector": {"status": "complete", "message": ""},
        "material_issues": [], "minor_issues": []}))
    monkeypatch.setattr(review, "_cash", lambda c, a: (0.0, []))
    monkeypatch.setattr(review, "_first_trade_date", lambda c, a=None: None)
    return review, seen


def test_x2_account_review_scopes_every_computable_section(review_recorder):
    review, seen = review_recorder
    out = review.portfolio_review_snapshot(cutoff=cutoff(), account="IBKR")
    for name in ("positions", "totals", "income", "fees", "twr", "volatility",
                 "benchmark", "xirr_lots", "xirr_income", "drawdown", "concentration",
                 "allocation", "scenarios", "trade_quality"):
        assert set(seen[name]) == {"IBKR"}, (name, seen[name])
    # Data quality is about the shared price/reference data: portfolio-wide,
    # and the response says so.
    assert out["data_quality"]["scope"] == "portfolio"
    for section in ("summary", "returns", "benchmark", "risk", "concentration",
                    "attribution", "detail"):
        assert out[section]["scope"] == "account:IBKR", section
    assert out["meta"]["section_scope"]["data_quality"] == "portfolio"


def test_x2_whole_book_review_is_labelled_portfolio(review_recorder):
    review, _ = review_recorder
    out = review.portfolio_review_snapshot(cutoff=cutoff())
    assert {out[s]["scope"] for s in ("summary", "risk", "data_quality")} == {"portfolio"}


def test_q6_review_reads_inside_one_snapshot(review_recorder, monkeypatch):
    """Every section is assembled inside deps.snapshot(): one REPEATABLE READ
    transaction (the live test proves the isolation itself)."""
    from contextlib import contextmanager

    from app.mcp import deps
    from app.mcp.services import positions

    review, seen = review_recorder
    state = {"inside": False, "entered": 0}

    @contextmanager
    def snap():
        state["entered"] += 1
        state["inside"] = True
        try:
            yield None
        finally:
            state["inside"] = False

    monkeypatch.setattr(deps, "snapshot", snap)
    calls_inside = []
    orig = positions.current_positions
    monkeypatch.setattr(positions, "current_positions",
                        lambda *a, **kw: calls_inside.append(state["inside"]) or orig(*a, **kw))
    review.portfolio_review_snapshot(cutoff=cutoff(), account="IBKR")
    assert state["entered"] == 1
    assert calls_inside and all(calls_inside)


def test_q6_positions_cache_is_bypassed_inside_a_snapshot(fake_db, monkeypatch):
    from app.mcp import deps
    from app.mcp.services import positions

    key = ("fifo", None, None, NOW)
    frame = pd.DataFrame([{"symbol": "AAA", "qty": 1.0}])
    positions._cache_put(key, frame)                       # process-wide cache
    token = deps._memo.set({})                             # as snapshot() does
    try:
        assert positions._cache_get(key) is None           # not visible inside
        positions._cache_put(key, frame)
        assert positions._cache_get(key) is not None       # snapshot-local memo
    finally:
        deps._memo.reset(token)


# ────────────────────────── X-3: allocation by account ──────────────────────────


def test_x3_null_account_lots_are_their_own_bucket(env_token, fake_db, monkeypatch):
    """A=100, unassigned=50 used to report none=150 (the whole book), A=100,
    total=250."""
    from app.mcp.services import analytics, positions

    fake_db(responses=[(["account"], [("A",), (None,)], "DISTINCT account FROM lots")])
    by_label = {"A": 100.0, "(none)": 50.0}
    monkeypatch.setattr(positions, "positions_summary",
                        lambda method="fifo", account=None, **kw: {"market_value": by_label[account]})
    out = analytics.allocation_by("account")
    assert {r["key"]: r["market_value"] for r in out["rows"]} == {"A": 100.0, "(none)": 50.0}
    assert out["total_market_value"] == 150.0


# ────────────────────────── X-6: benchmark 1D / WTD ──────────────────────────


def _records(days, mv=100.0):
    return [{"day": d, "mv": mv, "flow": 0.0, "income": 0.0} for d in days]


@pytest.mark.parametrize("period, today, days", [
    ("1D", date(2026, 8, 10), [date(2026, 8, 7), date(2026, 8, 10)]),   # Monday vs Friday
    ("WTD", date(2026, 8, 12), [date(2026, 8, 7), date(2026, 8, 10),
                                date(2026, 8, 11), date(2026, 8, 12)]),
])
def test_x6_short_periods_can_be_answered(fake_db, monkeypatch, period, today, days):
    import twr

    from app.mcp.services import returns

    prices = {d: {"SPY": 100.0 + i, "AAA": 10.0 + i} for i, d in enumerate(days)}
    monkeypatch.setattr(returns, "_price_by_day", lambda cutoff=None: prices)
    monkeypatch.setattr(returns, "_records", lambda c, a, p: _records(days))
    monkeypatch.setattr(twr, "period_returns", lambda records, t: {p: 1.0 for p in twr.PERIODS})
    monkeypatch.setattr(twr, "benchmark_records", lambda p, s: [])
    out = returns.benchmark_comparison(period, today=today, symbol="SPY")
    assert out["status"] == "ok", out.get("explanation")
    assert out["alignment"]["minimum_observations"] == 2
    assert out["alignment"]["aligned_observations"] == len(days) if period == "1D" else True


def test_x6_long_periods_keep_the_ten_observation_floor(fake_db, monkeypatch):
    import twr

    from app.mcp.services import returns

    days = [date(2026, 8, 3) + timedelta(days=i) for i in range(5)]
    prices = {d: {"SPY": 100.0} for d in days}
    monkeypatch.setattr(returns, "_price_by_day", lambda cutoff=None: prices)
    monkeypatch.setattr(returns, "_records", lambda c, a, p: _records(days))
    monkeypatch.setattr(twr, "period_returns", lambda records, t: {p: 1.0 for p in twr.PERIODS})
    monkeypatch.setattr(twr, "benchmark_records", lambda p, s: [])
    out = returns.benchmark_comparison("YTD", today=days[-1], symbol="SPY")
    assert out["status"] == "insufficient_alignment"


# ────────────────────────── X-7: top movers ──────────────────────────


def test_x7_a_symbol_is_never_both_a_gainer_and_a_loser(fake_db, monkeypatch):
    from app.mcp.services import positions, prices

    held = [{"symbol": s, "qty": 1.0, "last_price": p} for s, p in
            (("UP1", 11.0), ("UP2", 12.0), ("FLAT", 10.0), ("DN1", 9.0))]
    monkeypatch.setattr(positions, "current_positions", lambda *a, **kw: held)
    monkeypatch.setattr(prices, "second_latest_price_map", lambda **kw: {s: 10.0 for s in
                                                                         ("UP1", "UP2", "FLAT", "DN1")})
    out = prices.top_movers("snapshot", limit=5)
    assert [m["symbol"] for m in out["gainers"]] == ["UP2", "UP1"]
    assert [m["symbol"] for m in out["losers"]] == ["DN1"]
    assert all(m["delta_usd"] > 0 for m in out["gainers"])
    assert all(m["delta_usd"] < 0 for m in out["losers"])


# ────────────────────────── X-10: smaller inconsistencies ──────────────────────────


def test_x10_correlation_window_is_anchored_to_the_cutoff(fake_db, monkeypatch):
    from app.mcp.services import analytics

    old = Cutoff(ts=datetime(2026, 3, 31, 20, tzinfo=timezone.utc), trade_date=date(2026, 3, 31))
    fake_db(responses=[(["symbol", "day", "last_price"], [], "price_snapshots")])
    analytics._daily_price_frame(["AAA", "BBB"], "1m", cutoff=old)
    sql, params = next((s, p) for s, p in fake_db.executed() if "price_snapshots" in s)
    since = [p for p in params if isinstance(p, datetime) and p != old.ts][0]
    assert since == old.ts - timedelta(days=30)


def test_x10_cash_history_until_includes_the_whole_day(fake_db):
    from app.mcp.services import activity

    fake_db(responses=[(["ts", "account", "cash", "note"], [], "ts < %s")])
    activity.cash_history(until=date(2026, 8, 1), limit=10)
    sql, params = fake_db.executed()[0]
    upper = [p for p in params if isinstance(p, datetime)][0]
    # The end bound is the start of the NEXT reporting-timezone day, exclusive.
    assert upper.date() == date(2026, 8, 2) and upper.hour == 0
    assert upper.tzinfo is not None


@pytest.mark.parametrize("method", ["FIFO", "Fifo", "avg_cost", "AVG"])
def test_x10_method_casing_is_one_rule(fake_db, method):
    from app.mcp.services import common, pnl

    fake_db(responses=[([], [])], cycle=True)
    assert common.normalize_method(method) in ("fifo", "avg")
    pnl.realized_pnl(method, group_by="none")  # used to raise for "FIFO"


def test_x10_position_detail_upper_case_fifo_returns_open_lots(fake_db, monkeypatch):
    from app.mcp.services import positions

    called = []
    monkeypatch.setattr(positions, "current_positions", lambda *a, **kw: [])
    monkeypatch.setattr(positions, "_per_account_breakdown", lambda s, m: [])
    monkeypatch.setattr(positions, "open_lots", lambda **kw: called.append(kw) or [{"x": 1}])
    monkeypatch.setattr(positions, "_symbol_trades", lambda s: [])
    out = positions.position_detail("aaa", "FIFO")
    assert called and out["open_lots"] == [{"x": 1}]


def test_x10_frame_cache_survives_concurrent_writers(fake_db):
    """The evict-then-insert is now under a lock; unlocked, concurrent puts
    raced on next(iter(dict)) while another thread resized it."""
    from app.mcp.services import positions

    frame = pd.DataFrame([{"symbol": "AAA"}])
    errors = []

    def writer(base):
        try:
            for i in range(300):
                positions._cache_put(("fifo", None, None, base + i), frame)
                positions._cache_get(("fifo", None, None, base + i))
        except Exception as e:  # pragma: no cover - the failure being guarded
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n * 1000,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(positions._FRAME_CACHE) <= positions._FRAME_CACHE_MAX


# ────────────────────────── X-5: orphan sells use the engine ──────────────────────────


def test_x5_orphan_check_reads_the_prepared_ledger(fake_db, monkeypatch):
    """Buy 10, 2:1 split, sell 20: matched in full on post-split units."""
    import corporate_actions
    from decimal import Decimal

    from app.mcp.services import data_quality

    split = corporate_actions.CorporateAction(
        symbol="AAA", kind="SPLIT", ex_date=date(2026, 1, 20), ratio=Decimal(2),
        adjust_prices=True, adjust_lots=True,
    ) if hasattr(corporate_actions, "CorporateAction") else None
    monkeypatch.setattr(corporate_actions, "fetch_actions", lambda conn: [split])
    fake_db(responses=[(LOT_COLS, [
        (1, "AAA", "A", "BUY", date(2026, 1, 5), Decimal(10), Decimal(100), Decimal(0)),
        (2, "AAA", "A", "SELL", date(2026, 2, 5), Decimal(20), Decimal(55), Decimal(0)),
    ], "FROM lots")])
    assert data_quality._orphan_sells(cutoff()) == {}
