"""Tests for explain_db_error — the one place a connection failure becomes prose.

The failure this exists for: stale PORTFOLIODB_MCP_RO_* values used to take out
the dashboard's Data Health page with psycopg2's raw text, container IP and all,
on the one page whose whole job is telling you whether to trust your numbers.
"""

from __future__ import annotations

import psycopg2

from app.mcp.deps import explain_db_error

AUTH_FAILED = (
    'connection to server at "postgres" (172.18.0.2), port 5432 failed: '
    'FATAL:  password authentication failed for user "portfoliodb_ro"\n'
    "extra psycopg2 detail that is noise in a UI"
)


def test_explains_a_stale_read_only_password(monkeypatch):
    monkeypatch.setenv("PORTFOLIODB_MCP_RO_USER", "portfoliodb_ro")
    out = explain_db_error(psycopg2.OperationalError(AUTH_FAILED))
    assert "portfoliodb_ro" in out
    assert "make ro-role" in out            # how to fix it
    assert "clear both PORTFOLIODB_MCP_RO_" in out   # or how to opt out
    assert "172.18.0.2" not in out          # not the container's plumbing


def test_untouched_when_no_read_only_role_is_configured(monkeypatch):
    """Without the role configured, the same message is somebody else's problem:
    a genuinely wrong application password, and inventing role advice would send
    the reader down the wrong path."""
    monkeypatch.delenv("PORTFOLIODB_MCP_RO_USER", raising=False)
    out = explain_db_error(psycopg2.OperationalError(AUTH_FAILED))
    assert out.startswith("connection to server")
    assert "make ro-role" not in out


def test_only_claims_the_role_when_the_role_is_the_one_rejected(monkeypatch):
    """The app credentials can fail while a read-only role is also configured.
    Blaming the role then would be a confident wrong answer."""
    monkeypatch.setenv("PORTFOLIODB_MCP_RO_USER", "portfoliodb_ro")
    other = 'FATAL:  password authentication failed for user "portfoliouser"'
    assert explain_db_error(psycopg2.OperationalError(other)) == other


def test_first_line_only(monkeypatch):
    monkeypatch.delenv("PORTFOLIODB_MCP_RO_USER", raising=False)
    out = explain_db_error(psycopg2.OperationalError("line one\nline two"))
    assert out == "line one"


def test_falls_back_to_the_exception_class_when_there_is_no_message(monkeypatch):
    monkeypatch.delenv("PORTFOLIODB_MCP_RO_USER", raising=False)
    assert explain_db_error(psycopg2.OperationalError("")) == "OperationalError"
