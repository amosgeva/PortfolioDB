"""The prepared ledger is the parity contract — one fixture, every surface.

The engines were always shared; the *inputs* were not. These tests feed one
synthetic ledger (a 2:1 split, a 1:4 reverse split, a sale before and after
an action, two accounts, a missing quote day) through the real entry points
of each surface — the dashboard payload, the executive report's gather(), the
positions CLI, the income backfill — and assert they agree with each other
and with the arithmetic. They exercise the entry points rather than the
adjustment helper, because a passing helper test is exactly what did not
catch F03 and F07.

No database: the loaders are replaced at the module seam (`ledger_inputs.load`
and each surface's own query functions), which is the seam the surfaces are
required to use.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import corporate_actions  # noqa: E402
import ledger_inputs  # noqa: E402
from corporate_actions import CorporateAction  # noqa: E402

D1, D2, D3, D4, D5 = (date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5), date(2026, 3, 6))


def _ts(d: date, hour: int = 20) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)


# AAA: 10 shares at 100 in IBKR on D1, 2:1 split ex D3, sell 4 post-split
#      shares at 55 on D4 (post-split units, as entered at the time).
# AAA in a second account: 2 shares at 100 on D1 (also pre-split → 4).
# BBB: 8 shares at 10 on D1, 1:4 reverse split ex D3 → 2 shares at 40; a sale
#      of 4 raw shares on D2, BEFORE the action, is pre-split too (→ 1 share).
LOTS = [
    {"id": 1, "symbol": "AAA", "account": "IBKR", "side": "BUY", "trade_date": D1,
     "quantity": Decimal("10"), "price": Decimal("100"), "fees": Decimal("0")},
    {"id": 2, "symbol": "AAA", "account": "SCHW", "side": "BUY", "trade_date": D1,
     "quantity": Decimal("2"), "price": Decimal("100"), "fees": Decimal("0")},
    {"id": 3, "symbol": "AAA", "account": "IBKR", "side": "SELL", "trade_date": D4,
     "quantity": Decimal("4"), "price": Decimal("55"), "fees": Decimal("0")},
    {"id": 4, "symbol": "BBB", "account": "IBKR", "side": "BUY", "trade_date": D1,
     "quantity": Decimal("8"), "price": Decimal("10"), "fees": Decimal("0")},
    {"id": 5, "symbol": "BBB", "account": "IBKR", "side": "SELL", "trade_date": D2,
     "quantity": Decimal("4"), "price": Decimal("10"), "fees": Decimal("0")},
]
ACTIONS = [
    CorporateAction(symbol="AAA", kind="SPLIT", ex_date=D3, ratio=Decimal("2")),
    CorporateAction(symbol="BBB", kind="REVERSE_SPLIT", ex_date=D3, ratio=Decimal("0.25")),
]
# Raw quotes as the collector stored them: the AAA step from 100 to 50 and the
# BBB step from 10 to 40 are the splits, not moves. BBB has no quote on D4.
RAW_PRICE_ROWS = [
    {"ts": _ts(D1), "symbol": "AAA", "last_price": Decimal("100")},
    {"ts": _ts(D1), "symbol": "BBB", "last_price": Decimal("10")},
    {"ts": _ts(D2), "symbol": "AAA", "last_price": Decimal("100")},
    {"ts": _ts(D2), "symbol": "BBB", "last_price": Decimal("10")},
    {"ts": _ts(D3), "symbol": "AAA", "last_price": Decimal("50")},
    {"ts": _ts(D3), "symbol": "BBB", "last_price": Decimal("40")},
    {"ts": _ts(D4), "symbol": "AAA", "last_price": Decimal("55")},
    {"ts": _ts(D5), "symbol": "AAA", "last_price": Decimal("55")},
    {"ts": _ts(D5), "symbol": "BBB", "last_price": Decimal("40")},
]


@pytest.fixture
def ledger() -> ledger_inputs.PreparedLedger:
    return ledger_inputs.prepare(LOTS, ACTIONS)


# ── the loader itself ─────────────────────────────────────────────


class TestPrepare:
    def test_lots_are_restated_and_money_is_invariant(self, ledger):
        by_id = {lot["id"]: lot for lot in ledger.lots}
        assert by_id[1]["quantity"] == Decimal("20") and by_id[1]["price"] == Decimal("50")
        assert by_id[2]["quantity"] == Decimal("4") and by_id[2]["price"] == Decimal("50")
        assert by_id[3]["quantity"] == Decimal("4") and by_id[3]["price"] == Decimal("55")  # post-split, untouched
        assert by_id[4]["quantity"] == Decimal("2") and by_id[4]["price"] == Decimal("40")
        assert by_id[5]["quantity"] == Decimal("1") and by_id[5]["price"] == Decimal("40")  # pre-action sale
        for raw, adj in zip(LOTS, ledger.lots):
            assert raw["quantity"] * raw["price"] == adj["quantity"] * adj["price"]

    def test_input_rows_are_not_mutated(self):
        before = [dict(r) for r in LOTS]
        ledger_inputs.prepare(LOTS, ACTIONS)
        assert LOTS == before

    def test_price_rows_are_restated_by_timestamp(self, ledger):
        rows = ledger.price_rows(RAW_PRICE_ROWS)
        by_key = {(r["ts"].date(), r["symbol"]): r["last_price"] for r in rows}
        assert by_key[(D1, "AAA")] == pytest.approx(50.0)     # pre-split quote halved
        assert by_key[(D3, "AAA")] == pytest.approx(50.0)     # ex-date quote untouched
        assert by_key[(D1, "BBB")] == pytest.approx(40.0)     # pre-reverse quote ×4
        assert by_key[(D5, "BBB")] == pytest.approx(40.0)
        assert [r["symbol"] for r in rows] == [r["symbol"] for r in RAW_PRICE_ROWS]

    def test_price_rows_leave_unpriced_and_untimed_rows_alone(self, ledger):
        rows = ledger.price_rows([
            {"ts": _ts(D1), "symbol": "AAA", "last_price": None},
            {"symbol": "AAA", "last_price": Decimal("100")},
        ])
        assert rows[0]["last_price"] is None
        assert rows[1]["last_price"] == Decimal("100")

    def test_price_by_day_matches_price_rows(self, ledger):
        raw = {D1: {"AAA": 100.0, "BBB": 10.0}, D3: {"AAA": 50.0, "BBB": 40.0}}
        assert ledger.price_by_day(raw) == {D1: {"AAA": 50.0, "BBB": 40.0}, D3: {"AAA": 50.0, "BBB": 40.0}}

    def test_no_actions_is_a_passthrough(self):
        plain = ledger_inputs.prepare(LOTS, [])
        assert [lot["quantity"] for lot in plain.lots] == [lot["quantity"] for lot in LOTS]
        assert plain.price_rows(RAW_PRICE_ROWS) == [dict(r) for r in RAW_PRICE_ROWS]
        assert plain.adjusted_symbols == frozenset()

    def test_load_filters_actions_after_as_of(self, monkeypatch):
        """As of D2 the splits have not happened: the position is ten shares."""
        monkeypatch.setattr(ledger_inputs, "fetch_all", lambda conn, q, p: [dict(r) for r in LOTS if r["trade_date"] <= D2])
        monkeypatch.setattr(corporate_actions, "fetch_actions", lambda conn: list(ACTIONS))
        as_of_d2 = ledger_inputs.load(object(), as_of=D2)
        assert as_of_d2.actions == ()
        assert {lot["id"]: lot["quantity"] for lot in as_of_d2.lots}[1] == Decimal("10")
        monkeypatch.setattr(ledger_inputs, "fetch_all", lambda conn, q, p: [dict(r) for r in LOTS])
        assert {lot["id"]: lot["quantity"] for lot in ledger_inputs.load(object()).lots}[1] == Decimal("20")


class TestFetchActionsFailureModes:
    class _Conn:
        def __init__(self, exc):
            self.exc = exc
            self.rolled_back = False

        def cursor(self):
            conn = self

            class Cur:
                def __enter__(s):
                    return s

                def __exit__(s, *a):
                    return False

                def execute(s, *a, **k):
                    raise conn.exc

            return Cur()

        def rollback(self):
            self.rolled_back = True

    def test_missing_table_is_no_actions(self):
        from psycopg2 import errors
        conn = self._Conn(errors.UndefinedTable("relation does not exist"))
        assert corporate_actions.fetch_actions(conn) == []
        assert conn.rolled_back

    def test_any_other_failure_propagates(self):
        from psycopg2 import errors
        conn = self._Conn(errors.QueryCanceled("canceling statement due to lock timeout"))
        with pytest.raises(errors.QueryCanceled):
            corporate_actions.fetch_actions(conn)
        assert conn.rolled_back


# ── the surfaces ──────────────────────────────────────────────────


def _patch_ledger(monkeypatch, module):
    monkeypatch.setattr(module.ledger_inputs, "load", lambda conn, **kw: ledger_inputs.prepare(LOTS, ACTIONS))


EXPECTED_AAA_QTY = 20.0 + 4.0 - 4.0     # two accounts restated, four sold post-split
EXPECTED_AAA_AVG = 50.0                 # 1,200 of cost over 24 shares, 4 sold FIFO at 50 → 20 × 50
EXPECTED_BBB_QTY = 1.0                  # 8 → 2, one (restated) sold


class TestDashboardPayload:
    @pytest.fixture
    def payload_module(self, monkeypatch):
        from dashboard import payload, queries

        # Every query the payload issues answers from the fixture or empty; the
        # connection is never touched. Only functions *defined* in queries are
        # replaced, not the names it imports.
        import inspect

        for name, fn in inspect.getmembers(queries, inspect.isfunction):
            if fn.__module__ == queries.__name__ and not name.startswith("_"):
                monkeypatch.setattr(queries, name, lambda conn, *a, **k: [])
        monkeypatch.setattr(queries, "first_snapshot_run_ts", lambda conn: None)
        monkeypatch.setattr(queries, "price_history", lambda conn, days=370: [dict(r) for r in RAW_PRICE_ROWS])
        latest = {}
        for r in RAW_PRICE_ROWS:
            latest[r["symbol"]] = {"symbol": r["symbol"], "ts": r["ts"], "last_price": r["last_price"],
                                   "bid": None, "ask": None, "source": "test"}
        monkeypatch.setattr(queries, "latest_prices", lambda conn: list(latest.values()))
        monkeypatch.setattr(payload, "_market_overview", lambda conn: ([], None))
        monkeypatch.setattr(payload, "_news_feed", lambda conn, *a: ([], None))
        monkeypatch.setattr(payload, "_logo_data_uris", lambda syms: {})
        _patch_ledger(monkeypatch, payload)
        return payload

    def test_a_failed_section_is_named_not_silently_empty(self, payload_module, monkeypatch):
        """A dead query behind the market strip used to render as an empty
        strip; the payload now says which section it lost."""
        monkeypatch.setattr(payload_module, "_market_overview",
                            lambda conn: ([], "market overview: OperationalError"))
        data = payload_module.build_payload_data(object(), lambda syms: {})
        assert data["markets"] == []
        assert data["degraded"] == ["market overview: OperationalError"]

    def test_a_healthy_payload_reports_nothing_degraded(self, payload_module):
        data = payload_module.build_payload_data(object(), lambda syms: {})
        assert data["degraded"] == []

    def test_holdings_are_in_post_split_units(self, payload_module):
        data = payload_module.build_payload_data(object(), lambda syms: {})
        holdings = {h["sym"]: h for h in data["holdings"]}
        assert holdings["AAA"]["qty"] == pytest.approx(EXPECTED_AAA_QTY)
        assert holdings["AAA"]["avgCost"] == pytest.approx(EXPECTED_AAA_AVG)
        assert holdings["BBB"]["qty"] == pytest.approx(EXPECTED_BBB_QTY)

    def test_split_is_not_a_return(self, payload_module):
        """Economic value is flat across both ex-dates (AAA 100→50 with 2×
        shares, BBB 10→40 with ¼ shares) apart from the genuine AAA +10%
        on D4. No −50% or +300% day may survive into the strip."""
        data = payload_module.build_payload_data(object(), lambda syms: {})
        by_period = {p["period"]: p["portfolio"] for p in data["returns"]["periods"]}
        # Restated values: D1 24×50 + 2×40 = 1,280. D2 sells one BBB at 40:
        # (1,240 + 40) / 1,280 − 1 = 0%. D3 flat across both ex-dates: 0%.
        # D4 AAA 50→55 and four sold at 55: (20×55 + 40 + 220) / 1,240 − 1 =
        # +9.68% — AAA is 96.8% of the book, so its +10% day is that. D5: 0%.
        # Raw, the same days would have read −50% (AAA) and +300% (BBB).
        assert by_period["MAX"] == pytest.approx(9.68, abs=0.01)

    def test_value_history_is_actual_holdings_not_todays(self, payload_module):
        """F07: the D1 point must be what was held on D1 at D1's price — 1,280 —
        however many shares are held today."""
        data = payload_module.build_payload_data(object(), lambda syms: {})
        series = data["pv"]["1Y"]
        first_ts, first_val = series[0]
        assert first_ts == int(_ts(D1).timestamp() * 1000)
        assert first_val == pytest.approx(1280.0)
        # D4: 20 AAA × 55 + 1 BBB carried at 40 (no BBB quote that day) = 1,140.
        d4 = [v for t, v in series if t == int(_ts(D4).timestamp() * 1000)]
        assert d4 == [pytest.approx(1140.0)]

    def test_a_later_purchase_does_not_rewrite_the_past(self, payload_module, monkeypatch):
        more = LOTS + [{"id": 9, "symbol": "AAA", "account": "IBKR", "side": "BUY", "trade_date": D5,
                        "quantity": Decimal("100"), "price": Decimal("55"), "fees": Decimal("0")}]
        monkeypatch.setattr(payload_module.ledger_inputs, "load",
                            lambda conn, **kw: ledger_inputs.prepare(more, ACTIONS))
        data = payload_module.build_payload_data(object(), lambda syms: {})
        assert data["pv"]["1Y"][0][1] == pytest.approx(1280.0)
        assert {h["sym"]: h["qty"] for h in data["holdings"]}["AAA"] == pytest.approx(120.0)

    def test_sparklines_and_price_chart_are_restated(self, payload_module):
        data = payload_module.build_payload_data(object(), lambda syms: {})
        assert data["stocks"]["AAA"]["hist"][0] == pytest.approx(50.0)
        chart_first = data["priceHist"]["AAA"][0][1]
        assert chart_first == pytest.approx(50.0)

    def test_risk_block_declares_its_basis(self, payload_module):
        data = payload_module.build_payload_data(object(), lambda syms: {})
        assert "current" in data["risk"]["basis"]


class TestExecutiveReport:
    def test_gather_uses_the_prepared_ledger(self, monkeypatch):
        import exec_report

        _patch_ledger(monkeypatch, exec_report)
        monkeypatch.setattr(exec_report, "_fetch_latest_prices",
                            lambda conn: {"AAA": {"last_price": 55.0, "ts": _ts(D5)}, "BBB": {"last_price": 40.0, "ts": _ts(D5)}})
        monkeypatch.setattr(exec_report, "_fetch_cash", lambda conn: {})
        monkeypatch.setattr(exec_report, "_fetch_eod_by_day",
                            lambda conn: {D1: {"AAA": 100.0, "BBB": 10.0}, D3: {"AAA": 50.0, "BBB": 40.0}, D5: {"AAA": 55.0, "BBB": 40.0}})
        monkeypatch.setattr(exec_report, "_fetch_latest_brief", lambda conn: None)
        monkeypatch.setattr(exec_report, "_fetch_fd_metrics", lambda conn: {})
        monkeypatch.setattr(exec_report, "_fetch_earnings_window", lambda conn, syms, **kw: ([], []))
        seen: dict = {}
        real_curve = exec_report._build_equity_curve

        def capture(lot_rows, eod_by_day):
            seen["eod"] = eod_by_day
            return real_curve(lot_rows, eod_by_day)

        monkeypatch.setattr(exec_report, "_build_equity_curve", capture)
        data = exec_report.gather(object())
        pos = data.positions.set_index("symbol")
        assert pos.loc["AAA", "qty"] == pytest.approx(EXPECTED_AAA_QTY)
        assert pos.loc["AAA", "avg_cost"] == pytest.approx(EXPECTED_AAA_AVG)
        assert pos.loc["BBB", "qty"] == pytest.approx(EXPECTED_BBB_QTY)
        # The EOD series the report charts is restated too: D1 AAA is 50, not 100.
        assert seen["eod"][D1]["AAA"] == pytest.approx(50.0)
        assert seen["eod"][D1]["BBB"] == pytest.approx(40.0)


class TestPositionsCli:
    def test_print_positions_reports_post_split_units(self, monkeypatch, caplog):
        import positions

        _patch_ledger(monkeypatch, positions)
        monkeypatch.setattr(positions, "load_config", lambda: {})

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(positions, "connect", lambda cfg: _Conn())
        with caplog.at_level(logging.INFO, logger="positions"):
            positions.print_positions("AAA")
        merged = [r.getMessage() for r in caplog.records if r.getMessage().startswith("AAA")]
        assert any("qty=20" in line and "avg_cost=$50.0000" in line for line in merged), merged


class TestIncomeBackfill:
    def test_entitlement_is_in_post_split_shares(self, monkeypatch):
        """Ten pre-split shares (plus two in the other account) and a $1
        per-share dividend after the 2:1: $24, not $12 (audit F10 case)."""
        import add_income

        _patch_ledger(monkeypatch, add_income)
        assert add_income._shares_held_on(object(), "AAA", D3) == {None: pytest.approx(24.0)}
        per_acct = add_income._shares_held_on(object(), "AAA", D3, per_account=True)
        assert per_acct == {"IBKR": pytest.approx(20.0), "SCHW": pytest.approx(4.0)}

    def test_backfill_writes_estimated_rows_and_counts_duplicates(self, monkeypatch):
        import types
        import add_income

        _patch_ledger(monkeypatch, add_income)

        class _Series(dict):
            def items(self):
                return super().items()

        dividends = _Series({_Pandasish(D3): 1.0, _Pandasish(D5): 1.0})
        fake_yf = types.SimpleNamespace(Ticker=lambda sym: types.SimpleNamespace(dividends=dividends))
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

        inserted_rows: list[dict] = []
        seen = set()

        def fake_run(conn, sql_text, params):
            key = params[:6]
            if key in seen:
                return 0
            seen.add(key)
            inserted_rows.append(dict(zip(
                ("symbol", "account", "kind", "ex_date", "pay_date", "amount",
                 "currency", "tax_withheld", "per_share", "source", "notes"), params)))
            return 1

        monkeypatch.setattr(add_income, "run", fake_run)
        inserted, dupes = add_income._backfill(object(), "AAA", since=None)
        assert (inserted, dupes) == (2, 0)
        assert [r["amount"] for r in inserted_rows] == [pytest.approx(24.0), pytest.approx(20.0)]
        assert all("estimate" in r["notes"] for r in inserted_rows)
        assert all(r["pay_date"] == r["ex_date"] for r in inserted_rows)
        # Re-running inserts nothing and says so.
        assert add_income._backfill(object(), "AAA", since=None) == (0, 2)


class _Pandasish:
    """Just enough of a pandas Timestamp for _backfill: .date()."""

    def __init__(self, d: date):
        self._d = d

    def date(self) -> date:
        return self._d

    def __hash__(self):
        return hash(self._d)

    def __eq__(self, other):
        return isinstance(other, _Pandasish) and other._d == self._d


# ── nothing else may read lots raw ────────────────────────────────


def test_no_reader_queries_lots_behind_the_loaders_back():
    """The contract only holds if every reader uses it. Writers and the two
    deliberately raw readers are allowed; anything else that SELECTs FROM lots
    has to justify itself here."""
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1]
    allowed = {
        "ledger_inputs.py",              # the loader
        "dashboard/queries.py",          # recent_lots: the Manage page's trade-history table (raw by design)
        "mcp/services/positions.py",     # composes its own filtered query, then prepare()
        "mcp/services/returns.py",       # adjusts through corporate_actions directly (MCP side, pre-existing)
        "mcp/services/activity.py", "mcp/services/fees.py", "mcp/services/data_quality.py",
        "mcp/services/cutoff.py", "mcp/services/review.py", "mcp/services/kpis.py",
        "mcp/services/analytics.py", "mcp/services/prices.py", "mcp/services/income.py",
        "mcp/resources/summary.py", "mcp/resources/reports.py",
        "add_lot.py", "sell_lot.py", "import_csv_history.py", "demo_seed.py",   # writers / dedupe checks
        "snapshot_prices.py",            # which symbols to collect, not a valuation
        "check_splits.py", "apply_schema.py", "modern2_native.py", "streamlit_app.py",
        "data_quality.py", "market_overview.py", "fd_weekly_enrichment.py",
        # Symbol universes (is the net quantity positive?), not valuations — a
        # split never changes the sign of a position.
        "enrich_instruments.py", "fetch_ticker_logos.py",
        "mcp/services/fundamentals.py", "mcp/tools/meta_tools.py",
        # Uses the loader for its positions and FIFO replay; the raw query left
        # is trades_between, a listing of what was entered in the week.
        "report_weekly_db.py",
        # Uses prepare() for the match-line replay; the raw query left sums
        # quantity × price, which is invariant under the adjustment.
        "mcp/services/pnl.py",
    }
    offenders = []
    for path in app_dir.rglob("*.py"):
        rel = path.relative_to(app_dir).as_posix()
        if rel.startswith("tests/") or "/tests/" in rel:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"FROM\s+lots\b", text) and rel not in allowed:
            offenders.append(rel)
    assert offenders == [], f"these read lots without the prepared ledger: {offenders}"
