"""Which database role the MCP pool connects as.

The read-only role is the default; the application's read-write credentials
are an explicit, logged opt-out. Before this the fallback was silent — a fresh
install that skipped `make ro-role` ran the LLM-facing server on a role that
could write, protected by a session setting any statement can undo.

No database: the pool constructor is replaced with a recorder.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.mcp import deps


@pytest.fixture
def recorder(monkeypatch):
    calls: list[dict] = []

    class FakePool:
        def __init__(self, minconn, maxconn, **kw):
            calls.append({"minconn": minconn, "maxconn": maxconn, **kw})

    monkeypatch.setattr(deps, "ThreadedConnectionPool", FakePool)
    monkeypatch.setattr(
        deps, "load_config",
        lambda: SimpleNamespace(host="db", port=5432, dbname="portfoliodb",
                                user="portfoliouser", password="app-rw-secret"),
    )
    for name in (deps.RO_USER_ENV, deps.RO_PW_ENV, deps.ALLOW_RW_FALLBACK_ENV):
        monkeypatch.delenv(name, raising=False)
    return calls


def test_read_only_role_is_used_when_configured(recorder, monkeypatch):
    monkeypatch.setenv(deps.RO_USER_ENV, "portfoliodb_ro")
    monkeypatch.setenv(deps.RO_PW_ENV, "ro-secret")
    deps._build_pool()
    (call,) = recorder
    assert call["user"] == "portfoliodb_ro" and call["password"] == "ro-secret"
    assert "default_transaction_read_only=on" in call["options"]


def test_without_the_role_the_pool_refuses_and_says_how_to_fix_it(recorder):
    with pytest.raises(RuntimeError) as exc:
        deps._build_pool()
    msg = str(exc.value)
    assert "make ro-role" in msg
    assert deps.RO_USER_ENV in msg and deps.RO_PW_ENV in msg
    assert deps.ALLOW_RW_FALLBACK_ENV in msg
    assert recorder == [], "no connection may be attempted with read-write credentials"


def test_blank_role_variables_count_as_unset(recorder, monkeypatch):
    """Compose passes `${VAR:-}` through as an empty string; that is not a role."""
    monkeypatch.setenv(deps.RO_USER_ENV, "   ")
    monkeypatch.setenv(deps.RO_PW_ENV, "")
    with pytest.raises(RuntimeError):
        deps._build_pool()


@pytest.mark.parametrize("flag", ["1", "true", "YES"])
def test_the_fallback_is_explicit_and_logged(recorder, monkeypatch, caplog, flag):
    monkeypatch.setenv(deps.ALLOW_RW_FALLBACK_ENV, flag)
    with caplog.at_level(logging.WARNING, logger=deps.__name__):
        deps._build_pool()
    (call,) = recorder
    assert call["user"] == "portfoliouser" and call["password"] == "app-rw-secret"
    assert "default_transaction_read_only=on" in call["options"], "the session guard stays on"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("READ-WRITE" in w and "make ro-role" in w for w in warnings)


def test_a_configured_role_wins_over_the_fallback_flag(recorder, monkeypatch):
    monkeypatch.setenv(deps.ALLOW_RW_FALLBACK_ENV, "1")
    monkeypatch.setenv(deps.RO_USER_ENV, "portfoliodb_ro")
    monkeypatch.setenv(deps.RO_PW_ENV, "ro-secret")
    deps._build_pool()
    assert recorder[0]["user"] == "portfoliodb_ro"
