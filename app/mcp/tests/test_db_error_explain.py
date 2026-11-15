"""Tests for explain_db_error — the one place a database failure becomes prose.

Two failures shaped it. Stale PORTFOLIODB_MCP_RO_* values used to take out the
dashboard's Data Health page with psycopg2's raw text, container IP and all.
And the same raw first line was handed to the MCP client by get_health, naming
the role and the database host to the model (audit M-7). The explanation is now
a category plus an action, and never echoes the driver's message.

It also used to tell the operator to "clear both PORTFOLIODB_MCP_RO_* values to
fall back to the application credentials" — a fallback that no longer exists
without an explicit flag, so following the advice left every database-backed
call failing (audit M-11).
"""

from __future__ import annotations

import psycopg2
import pytest

from app.mcp.deps import explain_db_error

AUTH_FAILED = (
    'connection to server at "postgres" (172.18.0.2), port 5432 failed: '
    'FATAL:  password authentication failed for user "portfoliodb_ro"\n'
    "extra psycopg2 detail that is noise in a UI"
)


def test_explains_a_stale_read_only_password(monkeypatch):
    monkeypatch.setenv("PORTFOLIODB_MCP_RO_USER", "portfoliodb_ro")
    out = explain_db_error(psycopg2.OperationalError(AUTH_FAILED))
    assert "PORTFOLIODB_MCP_RO_PASSWORD" in out   # which setting is wrong
    assert "make ro-role" in out                  # how to fix it
    assert "172.18.0.2" not in out                # not the container's plumbing
    assert "postgres" not in out.replace("PORTFOLIODB", "")


def test_no_stale_fallback_advice(monkeypatch):
    """M-11: clearing the RO variables no longer falls back to anything."""
    monkeypatch.setenv("PORTFOLIODB_MCP_RO_USER", "portfoliodb_ro")
    out = explain_db_error(psycopg2.OperationalError(AUTH_FAILED))
    assert "clear" not in out.lower()
    assert "fall back" not in out.lower()


def test_application_password_failure_is_not_blamed_on_the_role(monkeypatch):
    """The app credentials can fail while a read-only role is also configured.
    Blaming the role then would be a confident wrong answer."""
    monkeypatch.setenv("PORTFOLIODB_MCP_RO_USER", "portfoliodb_ro")
    other = 'FATAL:  password authentication failed for user "portfoliouser"'
    out = explain_db_error(psycopg2.OperationalError(other))
    assert "read-only role" not in out
    assert "portfoliouser" not in out


@pytest.mark.parametrize(
    "raw, expect",
    [
        ('connection to server at "10.0.0.7", port 5432 failed: Connection refused', "Could not reach"),
        ('connection to server at "10.0.0.7", port 5432 failed: timeout expired', "Timed out"),
        ('could not translate host name "db.internal" to address', "Could not reach"),
        ('FATAL:  role "ghost_ro" does not exist', "role does not exist"),
        ('FATAL:  database "nope" does not exist', "database"),
        ("ERROR:  canceling statement due to statement timeout", "statement timeout"),
        ("ERROR:  permission denied for table chat_log", "privilege"),
        ("something nobody anticipated at 10.0.0.7", "server log"),
    ],
)
def test_categories_never_echo_the_driver_text(monkeypatch, raw, expect):
    monkeypatch.delenv("PORTFOLIODB_MCP_RO_USER", raising=False)
    out = explain_db_error(psycopg2.OperationalError(raw))
    assert expect in out
    for leak in ("10.0.0.7", "db.internal", "ghost_ro", "chat_log", '"nope"'):
        assert leak not in out


def test_our_own_configuration_error_is_passed_through():
    """A RuntimeError from _credentials is written by us, free of secrets, and
    is the actionable answer — so it is shown as is (first line)."""
    err = RuntimeError("The MCP server needs its read-only database role: set X\nmore")
    assert explain_db_error(err) == "The MCP server needs its read-only database role: set X"


def test_empty_message_still_explains():
    out = explain_db_error(psycopg2.OperationalError(""))
    assert "OperationalError" in out
