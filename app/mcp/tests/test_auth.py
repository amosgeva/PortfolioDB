"""Bearer-auth tests for the MCP server.

We test the verifier directly (it's the trust boundary) rather than spinning
up uvicorn — the network-level enforcement was smoke-tested by hand and is
identical across requests since FastMCP wraps it in middleware.
"""

from __future__ import annotations

import os

import pytest

from app.mcp.auth import StaticBearerVerifier, build_verifier, load_token


def test_load_token_raises_when_missing(monkeypatch):
    monkeypatch.delenv("PORTFOLIODB_MCP_TOKEN", raising=False)
    # The .env fallback uses an absolute path inside app/, so monkeypatch.chdir
    # isn't enough. Stub out the fallback helpers so the test is hermetic and
    # doesn't depend on what's in the real repo-root .env.
    import app.mcp.auth as auth_module
    monkeypatch.setattr(auth_module, "_load_env_file_for_mcp", lambda: None)
    monkeypatch.setattr(auth_module, "_load_env_file_if_needed", lambda: None)
    with pytest.raises(RuntimeError, match="PORTFOLIODB_MCP_TOKEN"):
        load_token()


def test_load_token_reads_env(monkeypatch):
    monkeypatch.setenv("PORTFOLIODB_MCP_TOKEN", "  hello-world  ")
    assert load_token() == "hello-world"


def test_verifier_rejects_missing_token():
    v = StaticBearerVerifier(token="secret")
    assert _run(v.verify_token("")) is None


def test_verifier_rejects_wrong_token():
    v = StaticBearerVerifier(token="secret")
    assert _run(v.verify_token("not-the-token")) is None


def test_verifier_accepts_correct_token():
    v = StaticBearerVerifier(token="secret")
    tok = _run(v.verify_token("secret"))
    assert tok is not None
    assert tok.token == "secret"
    assert tok.client_id  # default principal name is set


def test_empty_token_construction_fails():
    with pytest.raises(ValueError):
        StaticBearerVerifier(token="")


def test_build_verifier_picks_up_env(env_token):
    v = build_verifier()
    assert isinstance(v, StaticBearerVerifier)
    # The token round-trips: a request carrying env_token must succeed.
    tok = _run(v.verify_token(env_token))
    assert tok is not None


def test_build_verifier_rejects_close_token(env_token):
    # Single-character difference must NOT pass (sanity check the compare).
    v = build_verifier()
    almost = env_token[:-1] + ("y" if env_token[-1] != "y" else "z")
    assert _run(v.verify_token(almost)) is None


# ──────────────────────────────────────────────────────────────────────────
# Helper: synchronously drive the async verify_token coroutine.
# ──────────────────────────────────────────────────────────────────────────

import asyncio


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)
