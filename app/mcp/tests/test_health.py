"""Tests for the health service, the get_health tool, and the /healthz route."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.mcp.services import health as health_service

# What a collector failure actually looks like in snapshot_runs.error: it names
# the symbols and, when yfinance raised, carries the traceback. None of these
# words may appear in an unauthenticated response.
_POISON = (
    'NVDA: HTTPError 429 Too Many Requests; Traceback (most recent call last): '
    'File "/app/app/snapshot_prices.py", line 488 — AMD: no price'
)
_MUST_NOT_LEAK = ("NVDA", "AMD", "Traceback", "snapshot_prices", "429", "symbols", "fd_", "error")


def test_liveness_has_exactly_three_keys_and_an_age(fake_db):
    ts = datetime.now(timezone.utc) - timedelta(seconds=90)
    fake_db(responses=[(["ts_end"], [(ts,)])])
    out = health_service.liveness()
    assert set(out) == set(health_service.LIVENESS_KEYS)
    assert out["ok"] is True
    assert out["db"] == "up"
    assert 85 <= out["last_snapshot_age_s"] <= 300


def test_liveness_treats_a_naive_timestamp_as_utc(fake_db):
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
    fake_db(responses=[(["ts_end"], [(naive,)])])
    out = health_service.liveness()
    assert 25 <= out["last_snapshot_age_s"] <= 300


def test_liveness_with_no_snapshot_yet(fake_db):
    fake_db(responses=[(["ts_end"], [])])
    assert health_service.liveness() == {"ok": True, "db": "up", "last_snapshot_age_s": None}


def test_liveness_db_down_keeps_the_reason_to_itself(monkeypatch):
    """The ping's reason names the role and the container IP. get_health may
    say it; the open route may not."""
    reason = 'FATAL:  password authentication failed for user "portfoliodb_ro" (172.18.0.2)'
    monkeypatch.setattr(health_service, "ping_db_detail", lambda: (False, reason))
    out = health_service.liveness()
    assert out == {"ok": False, "db": "down", "last_snapshot_age_s": None}


def test_liveness_survives_a_missing_snapshot_runs_table(fake_db, monkeypatch):
    """A fresh install has no snapshot_runs yet; that is 'up, age unknown',
    not 'down' — and the exception text stays in the log."""
    class Boom:
        def __enter__(self):
            raise RuntimeError('relation "snapshot_runs" does not exist')

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(health_service, "get_conn", lambda: Boom())
    assert health_service.liveness() == {"ok": True, "db": "up", "last_snapshot_age_s": None}


def _route_client():
    """A bare FastMCP with only /healthz mounted, driven over real HTTP."""
    from fastmcp import FastMCP
    from starlette.testclient import TestClient
    from app.mcp import healthz

    mcp = FastMCP("healthz-test")
    healthz.register(mcp)
    return TestClient(mcp.http_app())


def test_healthz_route_serves_liveness_not_the_diagnostic(fake_db, monkeypatch):
    """The HTTP boundary after a failed collector run. server_health() is
    replaced with a tripwire: the open route must not even ask for it."""
    def tripwire():
        raise AssertionError("/healthz must not call server_health()")

    monkeypatch.setattr(health_service, "server_health", tripwire)
    fake_db(responses=[(["ts_end"], [(datetime.now(timezone.utc),)])], cycle=True)

    with _route_client() as client:
        resp = client.get("/healthz")

    assert resp.status_code == 200
    assert set(resp.json()) == set(health_service.LIVENESS_KEYS)
    for word in _MUST_NOT_LEAK:
        assert word not in resp.text, f"unauthenticated /healthz leaked {word!r}"


def test_healthz_route_hides_the_failure_reason_on_503(fake_db, monkeypatch):
    reason = 'could not connect to server at "172.18.0.2", role "portfoliodb_ro"'
    monkeypatch.setattr(health_service, "ping_db_detail", lambda: (False, reason))

    with _route_client() as client:
        resp = client.get("/healthz")

    assert resp.status_code == 503
    assert resp.json() == {"ok": False, "db": "down", "last_snapshot_age_s": None}
    assert "172.18" not in resp.text and "portfoliodb_ro" not in resp.text


def test_healthz_route_logs_an_exception_but_never_serves_it(fake_db, monkeypatch):
    def explode():
        raise RuntimeError('psycopg2.OperationalError: host "172.18.0.2" refused')

    monkeypatch.setattr(health_service, "liveness", explode)

    with _route_client() as client:
        resp = client.get("/healthz")

    assert resp.status_code == 503
    assert set(resp.json()) == set(health_service.LIVENESS_KEYS)
    assert "172.18" not in resp.text and "psycopg2" not in resp.text


def test_get_health_tool_still_returns_the_diagnostic(env_token, fake_db, monkeypatch):
    """The other half of the contract: behind the token, the error text and
    the counts are still there for a human to debug with."""
    from fastmcp import FastMCP
    from app.mcp.tools import meta_tools

    ts = datetime(2026, 9, 9, 19, 30, tzinfo=timezone.utc)
    fake_db(
        responses=[
            (
                ["id", "ts_start", "ts_end", "status",
                 "symbols_total", "symbols_ok", "symbols_failed", "error"],
                [(43, ts, ts, "partial", 35, 33, 2, _POISON)],
            ),
            *[(["max", "count"], [(ts, 5)]) for _ in range(8)],
        ],
    )
    mcp = FastMCP("t")
    meta_tools.register(mcp)
    result = asyncio.run(mcp.call_tool("get_health", {}))
    sc = result.structured_content
    if isinstance(sc, dict) and "result" in sc:
        sc = sc["result"]
    assert sc["last_snapshot_run"]["error"] == _POISON
    assert sc["last_snapshot_run"]["symbols_failed"] == 2
    assert "NVDA" in json.dumps(sc)


@pytest.mark.parametrize("word", _MUST_NOT_LEAK)
def test_liveness_key_names_are_not_themselves_leaky(word):
    """Guards the constant the route is judged against: none of the words the
    route must not emit may be one of its own key names."""
    assert word not in health_service.LIVENESS_KEYS


def test_server_health_db_unreachable(monkeypatch):
    monkeypatch.setattr(
        health_service, "ping_db_detail",
        lambda: (False, 'FATAL:  password authentication failed for user "portfoliodb_ro"'),
    )
    out = health_service.server_health()
    assert out["ok"] is False
    assert out["db"]["reachable"] is False
    # The reason has to reach the caller: "reachable: false" alone sends you
    # hunting for a dead Postgres when the role simply does not exist yet.
    assert "portfoliodb_ro" in out["db"]["error"]
    # When DB is down we should NOT attempt to read snapshot / FD tables.
    assert out["last_snapshot_run"] is None
    assert out["fd_freshness"] == {}
    assert "now" in out


def test_db_status_shapes(monkeypatch):
    """A healthy probe reports only `reachable`; a failing one adds the reason."""
    monkeypatch.setattr(health_service, "ping_db_detail", lambda: (True, None))
    assert health_service.db_status() == {"reachable": True}

    monkeypatch.setattr(
        health_service, "ping_db_detail",
        lambda: (False, 'FATAL:  role "portfoliodb_ro" does not exist'),
    )
    status = health_service.db_status()
    assert status["reachable"] is False
    assert "portfoliodb_ro" in status["error"]


def test_server_health_db_reachable_no_snapshot(monkeypatch, fake_db):
    # ping_db True, last_snapshot_run returns nothing, fd queries each return (None, 0)
    fake_db(
        responses=[
            # last_snapshot_run query — no rows
            (
                ["id", "ts_start", "ts_end", "status",
                 "symbols_total", "symbols_ok", "symbols_failed", "error"],
                [],
            ),
            # 8 fd_freshness queries — each returns one row (max_ts, count)
            *[(["max", "count"], [(None, 0)]) for _ in range(8)],
        ],
    )
    monkeypatch.setattr(health_service, "ping_db_detail", lambda: (True, None))

    out = health_service.server_health()
    assert out["ok"] is True
    assert out["db"]["reachable"] is True
    assert out["last_snapshot_run"] is None
    # All FD tables should show row_count=0 and no fetched_at.
    assert set(out["fd_freshness"].keys()) >= {
        "fd_company_facts",
        "fd_financial_metrics",
        "fd_news",
    }
    for tbl, payload in out["fd_freshness"].items():
        assert payload == {"latest_fetched_at": None, "row_count": 0}


def test_server_health_with_snapshot_row(monkeypatch, fake_db):
    ts = datetime(2026, 5, 20, 19, 30, tzinfo=timezone.utc)
    fake_db(
        responses=[
            (
                ["id", "ts_start", "ts_end", "status",
                 "symbols_total", "symbols_ok", "symbols_failed", "error"],
                [(42, ts, ts, "ok", 19, 19, 0, None)],
            ),
            *[(["max", "count"], [(ts, 5)]) for _ in range(8)],
        ],
    )
    monkeypatch.setattr(health_service, "ping_db_detail", lambda: (True, None))

    out = health_service.server_health()
    assert out["ok"] is True
    snap = out["last_snapshot_run"]
    assert snap["id"] == 42
    assert snap["status"] == "ok"
    assert snap["symbols_ok"] == 19


def test_get_health_tool_returns_health_payload(env_token, fake_db, monkeypatch):
    """The MCP tool is a thin wrapper — it should just call server_health()."""
    from fastmcp import FastMCP
    from app.mcp.tools import meta_tools

    monkeypatch.setattr(health_service, "ping_db_detail", lambda: (True, None))
    fake_db(
        responses=[
            (
                ["id", "ts_start", "ts_end", "status",
                 "symbols_total", "symbols_ok", "symbols_failed", "error"],
                [],
            ),
            *[(["max", "count"], [(None, 0)]) for _ in range(8)],
        ],
    )

    mcp = FastMCP("t")
    meta_tools.register(mcp)
    result = asyncio.run(mcp.call_tool("get_health", {}))
    sc = result.structured_content
    if isinstance(sc, dict) and "result" in sc:
        sc = sc["result"]

    assert sc["ok"] is True
    assert sc["db"] == {"reachable": True}
    assert "fd_freshness" in sc
