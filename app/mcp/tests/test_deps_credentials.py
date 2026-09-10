"""Which database role the MCP pool connects as.

The read-only role is the default; the application's read-write credentials
are an explicit, logged opt-out. Before this the fallback was silent — a fresh
install that skipped `make ro-role` ran the LLM-facing server on a role that
could write, protected by a session setting any statement can undo.

No database: the pool constructor is replaced with a recorder.

Re-audit N03: the read-only path must also work with no PORTFOLIODB_PASSWORD
in the environment at all, because the mcp compose service is no longer given
one. `load_config` is the function that insists on it, so the recorder makes
it observable and the tests assert it is never called on the role path.
"""

from __future__ import annotations

import logging
import os
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
    # Where the database is comes from the credential-free loader...
    monkeypatch.setattr(deps, "load_target", lambda: ("db", 5432, "portfoliodb"))

    # ...and the application's login only from load_config, which the fallback
    # alone may call. It behaves like the real one: no password, no config.
    def load_config():
        calls.append({"load_config": True})
        if not os.getenv("PORTFOLIODB_PASSWORD"):
            raise RuntimeError("PORTFOLIODB_PASSWORD is not set. Set it in the environment or in the repo-root .env file.")
        return SimpleNamespace(host="db", port=5432, dbname="portfoliodb",
                               user="portfoliouser", password=os.environ["PORTFOLIODB_PASSWORD"])
    monkeypatch.setattr(deps, "load_config", load_config)
    for name in (deps.RO_USER_ENV, deps.RO_PW_ENV, deps.ALLOW_RW_FALLBACK_ENV, "PORTFOLIODB_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    return calls


def _pools(calls):
    return [c for c in calls if "minconn" in c]


def test_read_only_role_is_used_when_configured(recorder, monkeypatch):
    monkeypatch.setenv(deps.RO_USER_ENV, "portfoliodb_ro")
    monkeypatch.setenv(deps.RO_PW_ENV, "ro-secret")
    deps._build_pool()
    (call,) = recorder
    assert call["user"] == "portfoliodb_ro"
    assert call["password"] == "ro-secret"
    assert call["host"] == "db" and call["port"] == 5432 and call["dbname"] == "portfoliodb"
    assert "default_transaction_read_only=on" in call["options"]


def test_the_role_path_needs_no_write_password_at_all(recorder, monkeypatch):
    """The mcp compose service is not given PORTFOLIODB_PASSWORD (re-audit N03)."""
    assert os.getenv("PORTFOLIODB_PASSWORD") is None
    monkeypatch.setenv(deps.RO_USER_ENV, "portfoliodb_ro")
    monkeypatch.setenv(deps.RO_PW_ENV, "ro-secret")
    deps._build_pool()
    assert len(_pools(recorder)) == 1
    assert not any("load_config" in c for c in recorder), \
        "the read-only path must never read the application's configuration"


def test_without_the_role_the_pool_refuses_and_says_how_to_fix_it(recorder):
    with pytest.raises(RuntimeError) as exc:
        deps._build_pool()
    msg = str(exc.value)
    assert "make ro-role" in msg
    assert deps.RO_USER_ENV in msg
    assert deps.RO_PW_ENV in msg
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
    monkeypatch.setenv("PORTFOLIODB_PASSWORD", "app-rw-secret")
    with caplog.at_level(logging.WARNING, logger=deps.__name__):
        deps._build_pool()
    (call,) = _pools(recorder)
    assert call["user"] == "portfoliouser"
    assert call["password"] == "app-rw-secret"
    assert "default_transaction_read_only=on" in call["options"], "the session guard stays on"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("READ-WRITE" in w and "make ro-role" in w for w in warnings)


def test_a_configured_role_wins_over_the_fallback_flag(recorder, monkeypatch):
    monkeypatch.setenv(deps.ALLOW_RW_FALLBACK_ENV, "1")
    monkeypatch.setenv(deps.RO_USER_ENV, "portfoliodb_ro")
    monkeypatch.setenv(deps.RO_PW_ENV, "ro-secret")
    deps._build_pool()
    assert _pools(recorder)[0]["user"] == "portfoliodb_ro"


def test_the_fallback_without_the_write_password_refuses_and_says_where_it_goes(recorder, monkeypatch):
    """Compose no longer hands mcp the password; the operator adds it in the override."""
    monkeypatch.setenv(deps.ALLOW_RW_FALLBACK_ENV, "1")
    with pytest.raises(RuntimeError) as exc:
        deps._build_pool()
    msg = str(exc.value)
    assert "PORTFOLIODB_PASSWORD" in msg
    assert "docker-compose.override.yml" in msg
    assert "make ro-role" in msg
    assert _pools(recorder) == [], "no connection may be attempted"
