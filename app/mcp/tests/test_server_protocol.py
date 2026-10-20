"""The production server, driven over the MCP protocol.

The rest of this suite builds small FastMCP instances and calls tools in
process (`mcp.call_tool`). That skips the parts a fastmcp or mcp SDK upgrade
is most likely to break: server.build_server() wiring every module at once,
the client/server handshake, tool schemas as a client receives them, and the
streamable-HTTP transport with bearer auth that real clients connect through.

The counts below are the server's published surface. A deliberate change to
it updates them; an upgrade that silently drops a registration fails here.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timezone

import pytest

EXPECTED_TOOLS = 47
EXPECTED_PROMPTS = {
    "morning_brief", "analyze_concentration_risk", "review_recent_activity",
    "compare_methods_brief", "pre_trade_check", "fundamentals_brief",
    "drawdown_review",
}
EXPECTED_RESOURCES = {
    "portfolio://schema", "portfolio://conventions", "portfolio://summary",
}

ACCEPT = "application/json, text/event-stream"


@pytest.fixture
def server(env_token, fake_db, monkeypatch):
    """A fresh build_server(), without the import-time pool warm-up."""
    from app.mcp import deps

    monkeypatch.setattr(deps, "init_pool", lambda: None)
    module = importlib.import_module("app.mcp.server")
    return module.build_server()


def _run(coro):
    return asyncio.run(coro)


async def _surface(mcp):
    from fastmcp import Client

    async with Client(mcp) as client:
        return (
            await client.list_tools(),
            await client.list_prompts(),
            await client.list_resources(),
        )


def test_every_registration_is_listed_over_the_protocol(server):
    tools, prompts, resources = _run(_surface(server))

    names = [t.name for t in tools]
    assert len(names) == EXPECTED_TOOLS, sorted(names)
    assert len(set(names)) == len(names)
    for anchor in ("get_portfolio_review_snapshot", "get_positions", "get_health",
                   "get_portfolio_kpis", "get_data_quality"):
        assert anchor in names

    assert {p.name for p in prompts} == EXPECTED_PROMPTS
    assert EXPECTED_RESOURCES <= {str(r.uri) for r in resources}


def test_every_tool_publishes_an_object_schema_and_a_description(server):
    tools, _, _ = _run(_surface(server))
    for tool in tools:
        # MCP SDK v2 renamed inputSchema to input_schema; read either.
        schema = getattr(tool, "input_schema", None) or tool.inputSchema
        assert tool.description, tool.name
        assert schema.get("type") == "object", tool.name


def test_a_tool_call_round_trips_structured_content(server, fake_db):
    from fastmcp import Client

    ts = datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)
    fake_db(
        responses=[
            (
                ["id", "ts_start", "ts_end", "status",
                 "symbols_total", "symbols_ok", "symbols_failed", "error"],
                [(7, ts, ts, "ok", 30, 30, 0, None)],
            ),
            *[(["max", "count"], [(ts, 5)]) for _ in range(8)],
        ],
    )

    async def call():
        async with Client(server) as client:
            return await client.call_tool("get_health", {})

    result = _run(call())
    assert not result.is_error
    sc = result.structured_content
    if isinstance(sc, dict) and "result" in sc:
        sc = sc["result"]
    assert sc["last_snapshot_run"]["status"] == "ok"
    assert sc["last_snapshot_run"]["symbols_ok"] == 30


# ── streamable HTTP, the transport real clients use ──────────────


def _rpc(method: str, params: dict | None = None, id_: int | None = 1) -> dict:
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id_ is not None:
        msg["id"] = id_
    return msg


def _body(resp) -> dict:
    """A JSON-RPC reply, whether sent as JSON or as one SSE event."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        data = [ln[5:].strip() for ln in resp.text.splitlines() if ln.startswith("data:")]
        return json.loads(data[-1])
    return resp.json()


_INIT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "pytest", "version": "0"},
}


@pytest.fixture
def http(server):
    from starlette.testclient import TestClient

    with TestClient(server.http_app(transport="streamable-http")) as client:
        yield client


def test_http_rejects_a_missing_or_wrong_token(http):
    for headers in ({}, {"Authorization": "Bearer not-the-token"}):
        resp = http.post("/mcp/", json=_rpc("initialize", _INIT),
                         headers={"Accept": ACCEPT, **headers})
        assert resp.status_code == 401, headers


def test_http_handshake_and_tool_listing_with_the_token(http, env_token):
    auth = {"Accept": ACCEPT, "Authorization": f"Bearer {env_token}"}

    init = http.post("/mcp/", json=_rpc("initialize", _INIT), headers=auth)
    assert init.status_code == 200, init.text
    info = _body(init)["result"]
    assert info["serverInfo"]["name"] == "PortfolioDB"
    assert "get_portfolio_review_snapshot" in info["instructions"]

    session = {"mcp-session-id": init.headers["mcp-session-id"]} if "mcp-session-id" in init.headers else {}
    note = http.post("/mcp/", json=_rpc("notifications/initialized", id_=None),
                     headers={**auth, **session})
    assert note.status_code in (200, 202)

    listed = http.post("/mcp/", json=_rpc("tools/list", id_=2), headers={**auth, **session})
    assert listed.status_code == 200, listed.text
    assert len(_body(listed)["result"]["tools"]) == EXPECTED_TOOLS
