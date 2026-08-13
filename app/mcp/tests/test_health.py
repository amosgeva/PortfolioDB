"""Tests for the health service and the get_health tool."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone


from app.mcp.services import health as health_service


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
