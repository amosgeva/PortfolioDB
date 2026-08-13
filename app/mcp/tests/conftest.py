"""Shared pytest fixtures for MCP server tests.

Tests run against a FastMCP server built in-process with a fake DB pool so
they don't require a live Postgres or uvicorn.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

# Make the repo root importable so `app.mcp.*` resolves the same way the
# uvicorn launcher sees it.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_bare_module(name: str):
    """Import a module from app/ *without* putting app/ on sys.path.

    app/ must not go on sys.path from here: this conftest runs before fastmcp
    has been imported, and app/mcp/ on the path shadows the official `mcp` SDK
    that fastmcp needs — which surfaces as the misleading "FastMCP server
    support is not installed" (see CLAUDE.md). Registering the module under its
    bare name means the services' later `import settings` / `import db` get
    this same instance rather than loading a second copy.
    """
    spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / "app" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Pin the reporting timezone and cut the settings table out of the loop before
# any service imports and captures LOCAL_TZ / REPORTING_TZ. The shipped default
# is UTC; these tests assert non-UTC behaviour deliberately (e.g. 22:30 UTC on
# the 11th already being the 12th locally), so they must follow neither the
# default nor whatever the local settings table happens to hold. Mirrors
# app/tests/conftest.py — see the rationale there.
os.environ["PORTFOLIODB_TZ"] = "Asia/Jerusalem"

_load_bare_module("db")  # settings imports it
_settings = _load_bare_module("settings")
_settings.TTL_SECONDS = float("inf")
_settings._cache = {}
_settings._cache_at = time.monotonic()
_settings._db_ok = False

TEST_TOKEN = "test-bearer-token-abcdef"


@pytest.fixture
def env_token(monkeypatch):
    """Set the MCP token env var for the test process."""
    monkeypatch.setenv("PORTFOLIODB_MCP_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("PORTFOLIODB_PASSWORD", "fake-password-not-used")
    return TEST_TOKEN


class FakeCursor:
    """Minimal psycopg2-cursor shim for tests."""

    def __init__(self, rows: list[tuple], columns: list[str]):
        self._rows = rows
        self._columns = columns
        self._iter = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql: str, params=None):
        self._iter = iter(self._rows)

    def fetchone(self):
        try:
            return next(self._iter) if self._iter else None
        except StopIteration:
            return None

    def fetchall(self):
        return list(self._iter) if self._iter else []

    @property
    def description(self):
        return [(c,) for c in self._columns]


class FakeConn:
    """Configurable fake psycopg2 connection.

    The `state` dict holds a shared response queue keyed by `idx` so multiple
    FakeConn instances yielded from successive `get_conn()` calls within one
    test all consume from the same queue in order.
    """

    def __init__(self, state: dict):
        self._state = state
        self.closed = 0

    def cursor(self, *_, **__):
        responses = self._state["responses"]
        idx = self._state["idx"]
        if idx >= len(responses):
            cols, rows = responses[-1] if self._state["cycle"] and responses else ([], [])
        else:
            cols, rows = responses[idx]
            self._state["idx"] = idx + 1
        return FakeCursor(rows, cols)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = 1


@pytest.fixture
def fake_db(monkeypatch):
    """Patch deps.get_conn to yield a configurable FakeConn.

    Returns a setter so individual tests can shape the response sequence.
    Default behavior: SELECT 1 returns (1,), everything else returns no rows.
    """
    state: dict[str, Any] = {
        "responses": [(["?column?"], [(1,)])],
        "cycle": True,
        "idx": 0,
    }

    @contextmanager
    def fake_get_conn():
        yield FakeConn(state)

    # Patch all call sites. Services and tools imported `get_conn` by name at
    # import time, so we have to replace it inside every module that uses it.
    from app.mcp import deps
    monkeypatch.setattr(deps, "get_conn", fake_get_conn)
    from app.mcp.services import (
        activity as activity_service,
        analytics as analytics_service,
        fees as fees_service,
        fundamentals as fundamentals_service,
        health as health_service,
        income as income_service,
        kpis as kpis_service,
        pnl as pnl_service,
        positions as positions_service,
        prices as prices_service,
        returns as returns_service,
    )
    for mod in (
        health_service, activity_service, analytics_service,
        fees_service, fundamentals_service, income_service, kpis_service,
        pnl_service, positions_service, prices_service, returns_service,
    ):
        monkeypatch.setattr(mod, "get_conn", fake_get_conn)
    from app.mcp.tools import meta_tools
    monkeypatch.setattr(meta_tools, "get_conn", fake_get_conn)
    # ping_db inside health.py uses deps.ping_db -> deps.get_conn, but
    # ping_db is also imported by name; patch that direct reference too.
    monkeypatch.setattr(
        health_service,
        "ping_db_detail",
        lambda: (True, None),  # health-check passes; failure-mode tests override
    )

    def setter(responses, cycle: bool = False):
        state["responses"] = responses
        state["cycle"] = cycle
        state["idx"] = 0

    return setter


@pytest.fixture
def mcp_server(env_token, fake_db):
    """Build a fresh FastMCP instance with meta tools and resources registered.

    Importing app.mcp.server has module-level side effects (eager pool init),
    so tests build a clean server here instead. The auth verifier uses the
    same TEST_TOKEN value as the env_token fixture.
    """
    from fastmcp import FastMCP
    from app.mcp.auth import build_verifier
    from app.mcp.resources import conventions as conventions_resource
    from app.mcp.resources import schema as schema_resource
    from app.mcp.tools import meta_tools

    mcp = FastMCP(name="PortfolioDB-test", auth=build_verifier())
    meta_tools.register(mcp)
    schema_resource.register(mcp)
    conventions_resource.register(mcp)
    return mcp


@pytest.fixture(autouse=True)
def _clear_positions_cache():
    """Drop the memoised positions frames around every test.

    The cache is keyed on the cutoff instant, which is sound in production —
    same cutoff, same data — but test fixtures deliberately put *different*
    stubbed data behind the same fixed cutoff, so one test would otherwise see
    another's frame.
    """
    from app.mcp.services import positions

    positions.clear_frame_cache()
    yield
    positions.clear_frame_cache()
