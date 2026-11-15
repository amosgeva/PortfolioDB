"""The whole MCP surface against a real Postgres, on the read-only role.

Every registered tool, prompt and resource is called once over the MCP
protocol through the production server (server.build_server) and its real
connection pool. That is what makes the fast suite's fakes safe to rely on: a
query that is wrong — a column that does not exist, a privilege the read-only
role lacks, a placeholder mismatch, a type Postgres rejects — fails here
(audit T-3). The tool list is compared with the registrations, so a new tool
without a live call fails too.

The account, timezone, one-pager and snapshot-isolation fixes are also proved
here end to end, against rows this module inserts and removes:

* X-1 / Q-3 account-scoped XIRR, X-8 the '(none)' filter, X-3 allocation;
* X-4 / Q-5 a process holding only the read-only role reads the settings table;
* Q-6 the review's snapshot does not see a commit that lands mid-read;
* X-9 get_schema matches the live catalog; X-10 inclusive cash `until`.

Needs PORTFOLIODB_PASSWORD (to insert fixtures) and the read-only role.
Marked slow: run with `pytest app/mcp/tests/ -m slow`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

REPO = Path(__file__).resolve().parents[3]
SYM = "ZZW3A"
ACCT = "ZZW3-ACCT"
EUR_ACCT = "ZZW3-EUR"


def _run(coro):
    return asyncio.run(coro)


def _required() -> bool:
    return (os.getenv("PORTFOLIODB_TESTS_REQUIRE_DB") or "").strip().lower() in ("1", "true", "yes")


@pytest.fixture(scope="module")
def admin():
    """A read-write connection for inserting and removing fixtures."""
    import db

    report = pytest.fail if _required() else pytest.skip
    if not (os.getenv("PORTFOLIODB_MCP_RO_USER") and os.getenv("PORTFOLIODB_MCP_RO_PASSWORD")):
        report("the read-only role is not configured (PORTFOLIODB_MCP_RO_*)")
    try:
        conn = db.connect(db.load_config())
    except Exception as e:
        report(f"DB unavailable: {e}")
    conn.autocommit = True
    yield conn
    conn.close()


def _wipe(cur):
    cur.execute("DELETE FROM lots WHERE symbol = %s", (SYM,))
    cur.execute("DELETE FROM income WHERE symbol = %s", (SYM,))
    cur.execute("DELETE FROM price_snapshots WHERE symbol = %s", (SYM,))
    cur.execute("DELETE FROM cash_snapshots WHERE account IN (%s, %s)", (ACCT, EUR_ACCT))
    cur.execute("DELETE FROM instruments WHERE symbol = %s", (SYM,))
    cur.execute("DELETE FROM settings WHERE key IN ('reporting_tz', 'philosophy_md')")


@pytest.fixture(scope="module")
def fixtures(admin):
    """A symbol held in a named account and without one, income in each, a
    price, and cash entries on and after a day boundary."""
    today = datetime.now(timezone.utc)
    with admin.cursor() as cur:
        _wipe(cur)
        cur.execute("INSERT INTO instruments(symbol, sector, country, asset_type) "
                    "VALUES (%s, 'Tech', 'US', 'stock')", (SYM,))
        cur.execute(
            "INSERT INTO lots(symbol, account, side, trade_date, quantity, price) VALUES "
            "(%s, NULL, 'BUY', %s, 4, 50), (%s, %s, 'BUY', %s, 6, 50)",
            (SYM, today.date() - timedelta(days=40), SYM, ACCT, today.date() - timedelta(days=40)),
        )
        cur.execute(
            "INSERT INTO income(symbol, account, kind, pay_date, amount, currency, source) VALUES "
            "(%s, NULL, 'DIVIDEND', %s, 3, 'USD', 'test'), (%s, %s, 'DIVIDEND', %s, 11, 'USD', 'test')",
            (SYM, today.date() - timedelta(days=10), SYM, ACCT, today.date() - timedelta(days=10)),
        )
        # Money in another currency: excluded from USD totals, and listed.
        cur.execute(
            "INSERT INTO income(symbol, account, kind, pay_date, amount, currency, source) VALUES "
            "(%s, %s, 'DIVIDEND', %s, 1000, 'EUR', 'test')",
            (SYM, ACCT, today.date() - timedelta(days=9)),
        )
        cur.execute(
            "INSERT INTO cash_snapshots(ts, account, cash, currency, note) VALUES "
            "(%s, %s, 5000, 'EUR', 'euro cash')", (today - timedelta(hours=1), EUR_ACCT),
        )
        cur.execute("INSERT INTO price_snapshots(ts, symbol, last_price, source) VALUES "
                    "(%s, %s, 60, 'test')", (today - timedelta(minutes=1), SYM))
        cur.execute(
            "INSERT INTO cash_snapshots(ts, account, cash, note) VALUES "
            "('2026-08-01 23:30+00', %s, 100, 'late on the 1st'), "
            "('2026-08-02 00:30+00', %s, 200, 'early on the 2nd')",
            (ACCT, ACCT),
        )
    from app.mcp.services import positions

    positions.clear_frame_cache()
    yield
    with admin.cursor() as cur:
        _wipe(cur)
    positions.clear_frame_cache()


@pytest.fixture(scope="module")
def server(fixtures):
    import importlib

    had = os.environ.get("PORTFOLIODB_MCP_TOKEN")
    os.environ["PORTFOLIODB_MCP_TOKEN"] = had or "live-surface-test-token-0123456789-abcdef"
    try:
        module = importlib.import_module("app.mcp.server")
        yield module.build_server()
    finally:
        if had is None:
            os.environ.pop("PORTFOLIODB_MCP_TOKEN", None)


# Every tool, with arguments that exercise its SQL. Compared with the
# registrations below: add a tool, add it here.
def _tool_calls():
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    year_ago = (date.today() - timedelta(days=365)).isoformat()
    return {
        "list_symbols": [{}, {"held_only": True}],
        "list_accounts": [{}],
        "get_schema": [{}],
        "get_health": [{}],
        "get_positions": [{}, {"account": ACCT}, {"account": "(none)"}, {"method": "avg"},
                          {"as_of": week_ago}],
        "get_position_detail": [{"symbol": SYM}, {"symbol": SYM, "method": "FIFO"}],
        "get_open_lots": [{}, {"account": "(none)"}],
        "get_latest_price": [{"symbol": SYM}],
        "get_latest_prices": [{}, {"symbols": [SYM]}],
        "get_price_history": [{"symbol": SYM, "since": year_ago},
                              {"symbol": SYM, "since": year_ago, "resample": "daily"}],
        "get_price_change": [{"symbol": SYM, "window": "1m"}],
        "get_top_movers": [{}, {"window": "1d", "limit": 3}],
        "get_portfolio_value_history": [{"since": week_ago, "freq": "daily"},
                                        {"since": week_ago, "holdings_basis": "current_constant"}],
        "get_data_quality": [{}],
        "get_realized_pnl": [{}, {"account": "(none)"}],
        "get_unrealized_pnl": [{}, {"account": ACCT}],
        "get_pnl_by_symbol": [{}],
        "get_pnl_summary": [{}],
        "compare_methods": [{}],
        "get_trade_quality": [{}, {"account": ACCT}],
        "get_portfolio_kpis": [{}],
        "get_period_returns": [{}, {"account": ACCT}],
        "get_benchmark_comparison": [{"period": "1D"}, {"period": "WTD"}, {"period": "YTD", "account": ACCT}],
        "get_portfolio_review_snapshot": [{"detail_level": "full"}, {"account": ACCT}, {"account": "(none)"}],
        "get_lots": [{"limit": 5}, {"account": "(none)", "limit": 5}],
        "get_recent_trades": [{"limit": 5}],
        "get_trading_activity": [{"group_by": "account"}],
        "get_cash_balance": [{}, {"account": ACCT}],
        "get_cash_history": [{"account": ACCT, "until": "2026-08-01", "limit": 10}],
        "get_concentration": [{}, {"account": ACCT}],
        "get_sector_allocation": [{}],
        "get_allocation": [{"dimension": d} for d in ("sector", "asset_class", "currency", "region", "account")]
                          + [{"dimension": "sector", "account": ACCT}],
        "get_correlation_matrix": [{}, {"symbols": [SYM, "SPY"], "window": "all"}],
        "get_drawdown_stats": [{}, {"symbol": SYM}, {"account": ACCT},
                               {"holdings_basis": "current_constant"}],
        "get_position_weights": [{}],
        "get_fees_summary": [{}, {"account": "(none)"}],
        "get_income": [{}, {"group_by": "account"}],
        "get_dividend_summary": [{}, {"account": ACCT}],
        "get_company_facts": [{"symbol": SYM}],
        "get_financial_metrics": [{"symbol": SYM}],
        "get_financial_statements": [{"symbol": SYM, "statement_type": "income_statement"}],
        "get_earnings_history": [{"symbol": SYM}],
        "get_filings": [{"symbol": SYM}],
        "get_insider_trades": [{"symbol": SYM}],
        "get_top_holders": [{"symbol": SYM}],
        "get_news": [{"symbols": [SYM]}, {}],
        "get_fd_freshness": [{}, {"symbol": SYM}],
    }


PROMPTS = {
    "morning_brief": {},
    "analyze_concentration_risk": {},
    "review_recent_activity": {},
    "compare_methods_brief": {},
    "pre_trade_check": {"symbol": SYM, "side": "BUY", "qty": "1"},
    "fundamentals_brief": {"symbol": SYM},
    "drawdown_review": {},
}


def _payload(result):
    sc = result.structured_content
    if isinstance(sc, dict) and set(sc) == {"result"}:
        return sc["result"]
    if sc is not None:
        return sc
    text = " ".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(text)
    except ValueError:
        return text


def test_every_tool_runs_against_the_real_database(server):
    from fastmcp import Client

    calls = _tool_calls()

    async def go():
        out = {}
        async with Client(server) as client:
            registered = {t.name for t in await client.list_tools()}
            assert registered == set(calls), (
                f"untested: {sorted(registered - set(calls))}, "
                f"stale: {sorted(set(calls) - registered)}"
            )
            for name, arg_sets in calls.items():
                for args in arg_sets:
                    res = await client.call_tool(name, args, raise_on_error=False)
                    text = " ".join(getattr(c, "text", "") for c in res.content)
                    assert not res.is_error, f"{name}({args}) failed: {text}"
                    out[(name, json.dumps(args, sort_keys=True))] = _payload(res)
        return out

    results = _run(go())

    # A few answers worth checking while we are here.
    none_pos = results[("get_positions", json.dumps({"account": "(none)"}))]
    assert any(r["symbol"] == SYM and r["qty"] == pytest.approx(4) for r in none_pos)
    acct_pos = results[("get_positions", json.dumps({"account": ACCT}))]
    assert [r["symbol"] for r in acct_pos] == [SYM]
    assert acct_pos[0]["qty"] == pytest.approx(6)
    schema = results[("get_schema", "{}")]
    assert schema["source"] == "live"
    one_d = results[("get_benchmark_comparison", json.dumps({"period": "1D"}))]
    assert one_d["alignment"]["minimum_observations"] == 2


def test_every_prompt_and_resource_renders(server):
    from fastmcp import Client

    async def go():
        async with Client(server) as client:
            for name, args in PROMPTS.items():
                await client.get_prompt(name, args)
            for res in await client.list_resources():
                contents = await client.read_resource(res.uri)
                text = " ".join(getattr(c, "text", "") or "" for c in contents)
                assert "failed:" not in text.lower(), (res.uri, text[:200])

    _run(go())


# ────────────────────────── account scoping ──────────────────────────


def test_x8_none_filter_returns_exactly_the_unassigned_lots(fixtures):
    from app.mcp.services import activity

    rows = [r for r in activity.lots(account="(none)", symbol=SYM, limit=50)]
    assert len(rows) == 1 and rows[0]["account"] is None and rows[0]["quantity"] == 4


def test_x3_allocation_by_account_does_not_double_count(fixtures):
    from app.mcp.services import analytics, positions

    alloc = analytics.allocation_by("account")
    total = positions.positions_summary("fifo")["market_value"]
    assert alloc["total_market_value"] == pytest.approx(total)
    keys = {r["key"] for r in alloc["rows"]}
    assert "(none)" in keys and ACCT in keys


def test_x1_q3_account_xirr_uses_that_accounts_lots_and_income(fixtures, monkeypatch):
    import xirr as xirr_module

    from app.mcp.services import cutoff as cutoff_service
    from app.mcp.services import review

    captured = {}
    real = xirr_module.from_ledger
    monkeypatch.setattr(xirr_module, "from_ledger",
                        lambda **kw: captured.update(kw) or real(**kw))
    review._xirr(cutoff_service.resolve(), {"invested_market_value": 360.0}, ACCT)
    assert len(captured["lots"]) == 1
    assert captured["lots"][0]["account"] == ACCT
    assert [i["amount"] for i in captured["income"]] == [11.0]


def test_x10_cash_history_until_is_inclusive_in_the_reporting_timezone(fixtures):
    """PORTFOLIODB_TZ=UTC in CI: 23:30 on the 1st is the 1st; 00:30 on the 2nd
    is not. A bare-date bound used to stop at midnight at the START of the 1st."""
    from app.mcp.services import activity, cutoff

    rows = activity.cash_history(ACCT, until=date(2026, 8, 1), limit=10)
    if cutoff.tz_name() == "UTC":
        assert [r["cash"] for r in rows] == [100.0]
    assert all(r["note"]["untrusted_text"] for r in rows)


# ────────────────────────── Q-6: one snapshot ──────────────────────────


def test_q6_snapshot_does_not_see_a_commit_that_lands_mid_read(admin, fixtures):
    from app.mcp import deps

    with deps.snapshot():
        with deps.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM lots WHERE symbol = %s", (SYM,))
                before = cur.fetchone()[0]
        with admin.cursor() as cur:
            cur.execute("INSERT INTO lots(symbol, account, side, trade_date, quantity, price) "
                        "VALUES (%s, %s, 'BUY', %s, 1, 50)", (SYM, ACCT, date(2026, 1, 2)))
        with deps.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM lots WHERE symbol = %s", (SYM,))
                during = cur.fetchone()[0]
    with deps.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM lots WHERE symbol = %s", (SYM,))
            after = cur.fetchone()[0]
    with admin.cursor() as cur:
        cur.execute("DELETE FROM lots WHERE symbol = %s AND trade_date = %s", (SYM, date(2026, 1, 2)))
    assert during == before
    assert after == before + 1


def test_q6_the_review_runs_in_one_repeatable_read_transaction(fixtures, monkeypatch):
    from app.mcp import deps
    from app.mcp.services import positions, review

    seen = []
    orig = positions.current_positions

    def spy(*a, **kw):
        with deps.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW transaction_isolation")
                seen.append((id(conn), cur.fetchone()[0]))
        return orig(*a, **kw)

    monkeypatch.setattr(positions, "current_positions", spy)
    out = review.portfolio_review_snapshot(detail_level="summary")
    assert out["meta"]["consistent_read"] == "repeatable_read_snapshot"
    assert seen and {lvl for _, lvl in seen} == {"repeatable read"}
    assert len({cid for cid, _ in seen}) == 1


# ────────────────────────── X-4 / Q-5: settings through the read-only role ──────────────────────────


def test_x4_q5_read_only_process_reads_the_settings_table(admin, fixtures):
    """A fresh process with only the read-only role — no PORTFOLIODB_PASSWORD,
    as in the shipped mcp container — resolves the reporting timezone and the
    saved one-pager from the settings table, not the environment's fallback."""
    with admin.cursor() as cur:
        cur.execute("INSERT INTO settings(key, value) VALUES ('reporting_tz', 'Asia/Tokyo'), "
                    "('philosophy_md', 'W3 one-pager: buy what you understand.')")
    env = {k: v for k, v in os.environ.items() if k != "PORTFOLIODB_PASSWORD"}
    env["PORTFOLIODB_TZ"] = "UTC"  # the fallback the table must win over
    env.setdefault("PORTFOLIODB_MCP_TOKEN", "live-surface-test-token-0123456789-abcdef")
    code = (
        "import sys; sys.path.insert(0, '.');"
        # The server's own startup — which is what installs the RO source.
        "import app.mcp.server;"
        "from app.mcp.services import cutoff;"
        "import advisor, db, os;"
        "assert not os.getenv('PORTFOLIODB_PASSWORD');"
        "print(cutoff.tz_name());"
        "print(cutoff.resolve().tz);"
        "print(advisor.load_philosophy())"
    )
    try:
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                             capture_output=True, text=True, timeout=60)
    finally:
        with admin.cursor() as cur:
            cur.execute("DELETE FROM settings WHERE key IN ('reporting_tz', 'philosophy_md')")
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert lines[0] == "Asia/Tokyo"
    assert lines[1] == "Asia/Tokyo"
    assert lines[2] == "W3 one-pager: buy what you understand."


def test_r5_importing_the_services_opens_no_connection(fixtures):
    """R-5: a module-level timezone constant no longer reads settings at
    import. A process with no database at all can import the services; the
    first *use* resolves the zone."""
    env = {k: v for k, v in os.environ.items()}
    env["PORTFOLIODB_PORT"] = "1"  # nothing listens there
    env["PORTFOLIODB_CONNECT_TIMEOUT"] = "2"
    code = (
        "import sys, time; sys.path.insert(0, '.');"
        "t = time.monotonic();"
        "import psycopg2; real = psycopg2.connect;"
        "calls = [];"
        "psycopg2.connect = lambda *a, **k: calls.append(1) or real(*a, **k);"
        "import app.mcp.services.review, holdings, corporate_actions;"
        "print(len(calls))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "0"


# ────────────────────────── X-9: schema against the catalog ──────────────────────────


def test_x9_get_schema_is_the_live_catalog():
    from app.mcp.tools import meta_tools

    doc = meta_tools.schema_document()
    assert doc["source"] == "live"
    tables = doc["tables"]
    # The facts the static dictionary used to get wrong.
    assert tables["cash_snapshots"]["primary_key"] == "(account, ts)"
    assert any("side" in d for d in tables["lots"]["unique_indexes"])
    for t in ("income", "corporate_actions", "settings"):
        assert t in tables
    assert "chat_log" not in tables, "the read-only role cannot read chat_log"


def test_x9_every_live_table_and_column_is_described():
    """The anti-drift guard: a schema change that adds a table or column the
    MCP can read must describe it here (meta_tools._SCHEMA_DICT), and a
    description of something that no longer exists fails too."""
    from app.mcp.tools import meta_tools

    live = meta_tools.live_structure()
    curated = meta_tools._SCHEMA_DICT["tables"]
    fd = set(meta_tools._SCHEMA_DICT["fd_tables"]["names"])
    undocumented, stale = [], []
    for table, info in live.items():
        if table in fd:
            continue
        cols = {c for c, _, _ in info["columns"]}
        described = set(curated.get(table, {}).get("columns", {}))
        if table not in curated:
            undocumented.append(table)
        undocumented += [f"{table}.{c}" for c in sorted(cols - described)]
        stale += [f"{table}.{c}" for c in sorted(described - cols)]
    stale += [t for t in curated if t not in live and t not in meta_tools.UNDESCRIBED_TABLES]
    assert not undocumented, f"describe these in get_schema: {undocumented}"
    assert not stale, f"get_schema describes what does not exist: {stale}"


# ────────────────────────── 1.8.0 KPI definitions, live ──────────────────────────


def test_kpis_match_portfolio_kpis_on_the_same_rows(fixtures):
    """The MCP KPI payload equals app/portfolio_kpis.summary() over the rows
    the MCP valued and the lots it read — the dashboard's own module."""
    import db
    import ledger_inputs
    import portfolio_kpis

    from app.mcp.services import cutoff as cutoff_service
    from app.mcp.services import kpis, positions

    c = cutoff_service.resolve()
    k = kpis.portfolio_kpis("fifo", cutoff=c)
    rows = positions.positions_dataframe("fifo", cutoff=c).to_dict(orient="records")
    conn = db.connect(db.load_config())
    try:
        lots = ledger_inputs.load(conn, as_of=c.trade_date).lots
    finally:
        conn.close()
    expected = portfolio_kpis.summary(rows, lots, portfolio_kpis.to_dec(k["income_total"]))
    f = portfolio_kpis.as_float
    assert k["invested_capital"] == pytest.approx(f(expected["invested_capital"]))
    assert k["unrealized_pct"] == pytest.approx(f(expected["unrealized_pct"]))
    assert k["total_return_pct"] == pytest.approx(f(expected["total_return_pct"]))
    assert k["total_return_with_income_pct"] == pytest.approx(
        f(expected["total_return_with_income_pct"]))
    assert k["unpriced_symbols"] == expected["unpriced_symbols"]


def test_non_usd_money_is_excluded_everywhere(fixtures):
    from app.mcp.services import cutoff as cutoff_service
    from app.mcp.services import data_quality, income, kpis, review

    c = cutoff_service.resolve()
    k = kpis.portfolio_kpis("fifo", cutoff=c)
    assert all(r["account"] != EUR_ACCT or r["included"] is False for r in k["cash_by_account"])
    kinds = {w["kind"] for w in k["excluded_non_usd"]}
    assert kinds == {"income", "cash"}

    inc = income.income_summary(group_by="none", account=ACCT)
    assert inc["total_income"] == pytest.approx(11.0)  # the EUR 1,000 is not dollars

    r = review.portfolio_review_snapshot(cutoff=c, account=ACCT, detail_level="summary")
    assert r["summary"]["income_total"] == pytest.approx(11.0)
    assert any(w["currency"] == "EUR" for w in r["summary"]["excluded_non_usd"])

    dq = data_quality.portfolio_data_quality(cutoff=c)
    codes = {i["code"] for i in dq["material_issues"]}
    assert {"foreign_currency_income", "foreign_currency_cash"} <= codes


# ────────────────────────── QA round 1 ──────────────────────────


def test_importing_mcp_services_leaves_the_dashboards_settings_alone(admin, fixtures):
    """The dashboard imports MCP data quality for Data Health. With the
    application's credentials and NO MCP role configured, settings must still
    come from the table afterwards — the MCP used to swap the process-global
    settings source at package import, and every later refresh fell back."""
    with admin.cursor() as cur:
        cur.execute("INSERT INTO settings(key, value) VALUES ('reporting_tz', 'Asia/Tokyo')")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PORTFOLIODB_MCP_")}
    code = (
        "import sys; sys.path.insert(0, 'app'); sys.path.insert(0, '.');"
        "import settings;"
        "print(settings.get('reporting_tz'), settings.db_available());"
        "import app.mcp.services.data_quality, app.mcp.services.review;"
        "settings.refresh();"
        "print(settings.get('reporting_tz'), settings.db_available());"
        "print(settings._connection_source is settings._application_connection)"
    )
    try:
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                             capture_output=True, text=True, timeout=60)
    finally:
        with admin.cursor() as cur:
            cur.execute("DELETE FROM settings WHERE key = 'reporting_tz'")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines() == ["Asia/Tokyo True", "Asia/Tokyo True", "True"]


def test_cash_balance_tool_is_usd_only(admin, fixtures):
    """USD 50 + EUR 500 in two accounts: the public cash tool agrees with the
    KPIs (50), lists the euros, and never reports 550."""
    from app.mcp.services import activity, kpis

    with admin.cursor() as cur:
        cur.execute("DELETE FROM cash_snapshots WHERE account IN (%s, %s)", (ACCT, EUR_ACCT))
        cur.execute("INSERT INTO cash_snapshots(ts, account, cash, currency) VALUES "
                    "(now() - interval '1 minute', %s, 50, 'USD'), "
                    "(now() - interval '1 minute', %s, 500, 'EUR')", (ACCT, EUR_ACCT))
    try:
        bal = activity.cash_balance()
        mine = {r["account"]: r for r in bal["by_account"]}
        assert mine[ACCT]["included"] is True and mine[ACCT]["cash"] == 50.0
        assert mine[EUR_ACCT]["included"] is False and mine[EUR_ACCT]["currency"] == "EUR"
        assert any(w["account"] == EUR_ACCT for w in bal["excluded_non_usd"])
        assert bal["total_cash"] == pytest.approx(kpis.portfolio_kpis("fifo")["cash"])
        one = activity.cash_balance(EUR_ACCT)
        assert one["included"] is False and one["excluded_non_usd"]
        hist = activity.cash_history(EUR_ACCT, limit=5)
        assert hist[0]["currency"] == "EUR"
    finally:
        with admin.cursor() as cur:
            cur.execute("DELETE FROM cash_snapshots WHERE account IN (%s, %s)", (ACCT, EUR_ACCT))
