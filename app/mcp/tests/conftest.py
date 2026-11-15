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

TEST_TOKEN = "test-bearer-token-0123456789-abcdefghijklm"


@pytest.fixture
def env_token(monkeypatch):
    """Set the MCP token env var for the test process."""
    monkeypatch.setenv("PORTFOLIODB_MCP_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("PORTFOLIODB_PASSWORD", "fake-password-not-used")
    return TEST_TOKEN


def render_sql(query) -> str:
    """The text of a str or psycopg2.sql composition, without a connection.

    psycopg2's own as_string() needs a live connection to quote identifiers;
    the fake has none, so the composition is walked here.
    """
    from psycopg2 import sql as pgsql

    if isinstance(query, str):
        return query
    if isinstance(query, pgsql.Composed):
        return "".join(render_sql(part) for part in query.seq)
    if isinstance(query, pgsql.SQL):
        return query.string
    if isinstance(query, pgsql.Identifier):
        return ".".join('"' + s.replace('"', '""') + '"' for s in query.strings)
    if isinstance(query, pgsql.Placeholder):
        return "%s" if query.name is None else f"%({query.name})s"
    return str(query)


def _placeholder_count(text: str) -> int:
    """Positional %s placeholders, ignoring the %% escape."""
    return text.replace("%%", "").count("%s")


class FakeCursor:
    """Minimal psycopg2-cursor shim for tests.

    It used to discard the statement and replay rows by position, so a test
    could not tell a right query from a wrong one (audit T-3). It now:

    * records every statement and its parameters in ``state["executed"]``;
    * checks that the number of ``%s`` placeholders matches the parameters
      passed, which is how a dropped or extra filter usually shows up;
    * when a queued response carries a third element — a substring, or a
      tuple of substrings — asserts the statement it answers contains them.
    """

    def __init__(self, rows: list[tuple], columns: list[str], state: dict | None = None,
                 expect=None):
        self._rows = rows
        self._columns = columns
        self._iter = None
        self._state = state
        self._expect = expect

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        text = render_sql(sql)
        if self._state is not None:
            self._state.setdefault("executed", []).append((text, params))
        if params is not None and not isinstance(params, dict):
            assert _placeholder_count(text) == len(params), (
                f"{_placeholder_count(text)} placeholders but {len(params)} parameters in:\n{text}"
            )
        if self._expect:
            wanted = (self._expect,) if isinstance(self._expect, str) else self._expect
            for fragment in wanted:
                assert fragment in text, f"expected {fragment!r} in:\n{text}"
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
            entry = responses[-1] if self._state["cycle"] and responses else ([], [])
        else:
            entry = responses[idx]
            self._state["idx"] = idx + 1
        cols, rows = entry[0], entry[1]
        expect = entry[2] if len(entry) > 2 else None
        factory = __.get("cursor_factory") or (_[0] if _ else None)
        if factory is not None and getattr(factory, "__name__", "") == "RealDictCursor":
            # db.fetch_all asks for dict rows; give it what psycopg2 would.
            rows = [dict(zip(cols, r)) for r in rows]
        return FakeCursor(rows, cols, self._state, expect)

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

    # Import every call site BEFORE patching anything. Each of these modules
    # binds `get_conn` into its own namespace with `from app.mcp.deps import
    # get_conn` at import time, so a module first imported *after* deps was
    # patched captures the fake as its module-level binding. monkeypatch then
    # records the fake as that module's "original" and faithfully restores it
    # on teardown — leaking a FakeConn into every later test, including the
    # live-database ones in test_reconciliation.py. Which modules were already
    # imported depends on which tests ran first, which is what made the leak
    # look like flakiness. Import first, patch second, and the recorded
    # original is always the real function.
    from app.mcp import deps
    from app.mcp.resources import reports as reports_resource
    from app.mcp.services import (
        activity as activity_service,
        cash as cash_service,
        analytics as analytics_service,
        cutoff as cutoff_service,
        data_quality as data_quality_service,
        fees as fees_service,
        fundamentals as fundamentals_service,
        health as health_service,
        income as income_service,
        kpis as kpis_service,
        pnl as pnl_service,
        positions as positions_service,
        prices as prices_service,
        returns as returns_service,
        review as review_service,
    )
    from app.mcp.tools import meta_tools

    # Every module that holds its own `get_conn` reference — keep in sync with
    # `grep -rl 'import get_conn' app/mcp`, which the hygiene test enforces.
    for mod in (
        deps, health_service, activity_service, cash_service, analytics_service,
        cutoff_service, data_quality_service, fees_service,
        fundamentals_service, income_service, kpis_service, pnl_service,
        positions_service, prices_service, returns_service, review_service,
        reports_resource, meta_tools,
    ):
        monkeypatch.setattr(mod, "get_conn", fake_get_conn)

    # The review reads inside deps.snapshot(), which borrows a real pooled
    # connection. Under the fake it pins nothing: every get_conn() already
    # answers from the same queue.
    @contextmanager
    def fake_snapshot():
        yield FakeConn(state)

    monkeypatch.setattr(deps, "snapshot", fake_snapshot)
    # No corporate actions unless a test says otherwise. Every reader now asks
    # for them on the connection it holds, and with a cycling fake response the
    # actions query would be answered with whatever rows the test queued for
    # lots. Tests about split handling patch this themselves.
    import corporate_actions
    monkeypatch.setattr(corporate_actions, "fetch_actions", lambda conn: [])
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
        state["executed"] = []

    # The statements run so far, for tests that assert on the SQL itself.
    setter.executed = lambda: list(state.get("executed", []))
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
def _reset_healthz_cache():
    """/healthz caches its probe for a couple of seconds; tests that stub the
    probe differently must not see each other's answer."""
    from app.mcp import healthz

    healthz.reset_cache()
    yield
    healthz.reset_cache()


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
