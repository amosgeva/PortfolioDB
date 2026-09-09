"""Settings precedence, LLM provider selection, and brief-parse degradation.

No live database: settings' DB layer is faked by seeding the module cache
directly (the resolution logic under test is get()/source_of(), not psycopg2).
"""

from __future__ import annotations

import re
import time

import pytest

import advisor
import llm
import settings


@pytest.fixture
def fake_db(monkeypatch):
    """Seed the settings cache as if a DB read just succeeded."""
    def seed(values: dict[str, str], ok: bool = True):
        monkeypatch.setattr(settings, "_cache", dict(values))
        monkeypatch.setattr(settings, "_db_ok", ok)
        monkeypatch.setattr(settings, "_cache_at", time.monotonic())
    return seed


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # Disable llm's .env loader — these tests control the env explicitly and
    # must not have the operator's real .env leak back in mid-test.
    monkeypatch.setattr(llm, "_env_loaded", True)
    for name in (
        "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY",
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
        "PORTFOLIODB_ADVISOR_MODEL", "PORTFOLIODB_DISPLAY_NAME",
    ):
        monkeypatch.delenv(name, raising=False)


# ── settings precedence ──────────────────────────────────────────


class TestSettingsPrecedence:
    def test_db_wins_over_env_and_default(self, fake_db, monkeypatch):
        fake_db({"display_name": "From DB"})
        monkeypatch.setenv("PORTFOLIODB_DISPLAY_NAME", "From Env")
        assert settings.get("display_name", env="PORTFOLIODB_DISPLAY_NAME", default="Operator") == "From DB"
        assert settings.source_of("display_name", env="PORTFOLIODB_DISPLAY_NAME") == "db"

    def test_env_wins_over_default(self, fake_db, monkeypatch):
        fake_db({})
        monkeypatch.setenv("PORTFOLIODB_DISPLAY_NAME", "From Env")
        assert settings.get("display_name", env="PORTFOLIODB_DISPLAY_NAME", default="Operator") == "From Env"
        assert settings.source_of("display_name", env="PORTFOLIODB_DISPLAY_NAME") == "env"

    def test_default_when_nothing_set(self, fake_db):
        fake_db({})
        assert settings.get("display_name", env="PORTFOLIODB_DISPLAY_NAME", default="Operator") == "Operator"
        assert settings.source_of("display_name", env="PORTFOLIODB_DISPLAY_NAME") == "default"

    def test_blank_db_value_falls_through(self, fake_db, monkeypatch):
        fake_db({"display_name": "   "})
        monkeypatch.setenv("PORTFOLIODB_DISPLAY_NAME", "From Env")
        assert settings.get("display_name", env="PORTFOLIODB_DISPLAY_NAME", default="Operator") == "From Env"

    def test_env_tuple_first_nonblank_wins(self, fake_db, monkeypatch):
        fake_db({})
        monkeypatch.setenv("PORTFOLIODB_ADVISOR_MODEL", "legacy-model")
        got = settings.get("llm_model", env=("LLM_MODEL", "PORTFOLIODB_ADVISOR_MODEL"), default="x")
        assert got == "legacy-model"
        monkeypatch.setenv("LLM_MODEL", "new-model")
        assert settings.get("llm_model", env=("LLM_MODEL", "PORTFOLIODB_ADVISOR_MODEL"), default="x") == "new-model"

    def test_db_unavailable_falls_back_to_env(self, fake_db, monkeypatch):
        fake_db({}, ok=False)
        monkeypatch.setenv("PORTFOLIODB_DISPLAY_NAME", "Still Works")
        assert settings.get("display_name", env="PORTFOLIODB_DISPLAY_NAME", default="Operator") == "Still Works"
        assert settings.db_available() is False


# ── llm provider selection ───────────────────────────────────────


class TestProviderSelection:
    def test_defaults_to_anthropic(self, fake_db):
        fake_db({})
        assert llm.provider() == "anthropic"
        assert llm.model() == "claude-sonnet-5"

    def test_provider_from_db_setting(self, fake_db):
        fake_db({"llm_provider": "ollama"})
        assert llm.provider() == "ollama"
        assert llm.model() == "llama3.3"
        assert llm.base_url() == "http://localhost:11434/v1"

    def test_provider_from_env(self, fake_db, monkeypatch):
        fake_db({})
        monkeypatch.setenv("LLM_PROVIDER", "openrouter")
        assert llm.provider() == "openrouter"
        assert llm.base_url() == "https://openrouter.ai/api/v1"

    def test_unknown_provider_falls_back(self, fake_db):
        fake_db({"llm_provider": "skynet"})
        assert llm.provider() == "anthropic"

    def test_api_key_provider_specific_alias(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "anthropic"})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        assert llm.api_key() == "sk-ant-x"
        status = llm.key_status()
        assert status["set"] is True and status["env_var"] == "ANTHROPIC_API_KEY"

    def test_generic_key_wins(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "openai"})
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        monkeypatch.setenv("LLM_API_KEY", "sk-generic")
        assert llm.api_key() == "sk-generic"

    def test_missing_key_raises_with_env_names(self, fake_db):
        fake_db({"llm_provider": "openai"})
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            llm._require_key("openai")

    def test_key_optional_for_ollama(self, fake_db):
        fake_db({"llm_provider": "ollama"})
        assert llm._require_key("ollama") is None
        assert llm.key_status()["optional"] is True

    def test_complete_routes_to_openai_path(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "ollama", "llm_model": "test-model"})
        calls = {}

        class FakeCompletions:
            def create(self, **kw):
                calls.update(kw)
                msg = type("M", (), {"content": "hello"})
                choice = type("C", (), {"message": msg})
                return type("R", (), {"choices": [choice]})

        class FakeClient:
            chat = type("Chat", (), {"completions": FakeCompletions()})

        monkeypatch.setattr(llm, "_openai_client", lambda p: FakeClient())
        out = llm.complete([{"type": "text", "text": "sys A"}, {"type": "text", "text": "sys B"}],
                           [{"role": "user", "content": "q"}], max_tokens=123)
        assert out == "hello"
        assert calls["model"] == "test-model"
        assert calls["max_tokens"] == 123          # compat servers speak max_tokens
        assert calls["messages"][0] == {"role": "system", "content": "sys A\n\nsys B"}

    def test_openai_proper_uses_max_completion_tokens(self, fake_db):
        fake_db({})
        assert llm._openai_token_param("openai", 50) == {"max_completion_tokens": 50}
        assert llm._openai_token_param("openrouter", 50) == {"max_tokens": 50}


# ── where the key is allowed to go (audit F01) ────────────────────


def _chat_completion_json() -> dict:
    return {
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
    }


def _drive_real_sdk(provider: str):
    """Build the real client, send one completion through an in-memory
    transport, and return (url, authorization header) as the server saw them.
    No network, no real key."""
    import httpx

    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_chat_completion_json())

    client = llm._openai_client(
        provider, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    return seen["url"], seen["auth"]


class TestBaseUrlIsEnvOnly:
    """The Settings page has no login. A base URL saved there decided where the
    provider's key was sent, so a dashboard visitor could point `openai` at
    their own server and collect OPENAI_API_KEY. The database is no longer
    consulted for the URL at all."""

    def test_a_database_row_is_ignored(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "openai", "llm_base_url": "https://audit-sink.invalid/v1"})
        monkeypatch.setattr(llm, "_stale_base_url_row_warned", False)
        assert llm.base_url() == "https://api.openai.com/v1"

    def test_a_database_row_is_reported_once(self, fake_db, monkeypatch, caplog):
        fake_db({"llm_provider": "openai", "llm_base_url": "https://audit-sink.invalid/v1"})
        monkeypatch.setattr(llm, "_stale_base_url_row_warned", False)
        with caplog.at_level("WARNING", logger="llm"):
            llm.base_url()
            llm.base_url()
        assert sum("llm_base_url" in r.getMessage() for r in caplog.records) == 1

    def test_env_still_works(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "ollama"})
        monkeypatch.setenv("LLM_BASE_URL", "http://host.docker.internal:11434/v1")
        assert llm.base_url() == "http://host.docker.internal:11434/v1"

    def test_real_sdk_ignores_the_row_and_keys_the_vendor(self, fake_db, monkeypatch):
        """End to end through the OpenAI SDK: a poisoned settings row does not
        move the request, and the vendor key reaches the vendor."""
        fake_db({"llm_provider": "openai", "llm_base_url": "https://audit-sink.invalid/v1"})
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-synthetic")
        url, auth = _drive_real_sdk("openai")
        assert url.startswith("https://api.openai.com/v1/")
        assert auth == "Bearer sk-openai-synthetic"


class TestKeyStaysWithItsVendor:
    """Belt and braces under the env-only rule: even an operator-set
    LLM_BASE_URL that points a vendor elsewhere does not carry that vendor's
    named key. A proxy gets LLM_API_KEY or nothing."""

    def test_default_origin_gets_the_named_key(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "openrouter"})
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-x")
        assert llm._key_for_destination("openrouter", llm.base_url("openrouter")) == "sk-or-x"

    def test_same_origin_different_path_is_still_the_vendor(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "openai"})
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        assert llm._key_for_destination("openai", "https://API.OpenAI.com/v2/beta") == "sk-openai"

    def test_other_origin_refuses_the_named_key(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "openai"})
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY stays with its vendor"):
            llm._key_for_destination("openai", "https://proxy.example/v1")

    def test_other_origin_gets_the_generic_key(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "openai"})
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        monkeypatch.setenv("LLM_API_KEY", "sk-for-the-proxy")
        assert llm._key_for_destination("openai", "https://proxy.example/v1") == "sk-for-the-proxy"

    def test_real_sdk_sends_only_the_generic_key_to_a_proxy(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "openai"})
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-leave")
        monkeypatch.setenv("LLM_API_KEY", "sk-proxy")
        monkeypatch.setenv("LLM_BASE_URL", "https://proxy.example/v1")
        url, auth = _drive_real_sdk("openai")
        assert url.startswith("https://proxy.example/v1/")
        assert auth == "Bearer sk-proxy"
        assert "must-not-leave" not in (auth or "")

    def test_keyless_local_provider_still_works(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "ollama"})
        monkeypatch.setenv("LLM_BASE_URL", "http://host.docker.internal:11434/v1")
        url, auth = _drive_real_sdk("ollama")
        assert url.startswith("http://host.docker.internal:11434/v1/")
        assert auth == "Bearer not-needed"

    def test_custom_provider_uses_the_generic_key_only(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "custom"})
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-leave")
        monkeypatch.setenv("LLM_API_KEY", "sk-custom")
        monkeypatch.setenv("LLM_BASE_URL", "https://llm.internal/v1")
        url, auth = _drive_real_sdk("custom")
        assert url.startswith("https://llm.internal/v1/")
        assert auth == "Bearer sk-custom"


class TestSettingsPageHasNoBaseUrlField:
    """The form is the attack surface; the source must not grow the field back."""

    def test_no_base_url_input_and_a_stale_row_cleanup(self):
        from pathlib import Path
        src = Path(llm.__file__).with_name("modern2_native.py").read_text(encoding="utf-8")
        # The save loop's tuple form, `("llm_base_url", <value>, ...)`; the
        # cleanup call `unset("llm_base_url")` has no comma and is allowed.
        assert '("llm_base_url",' not in src, "the Settings save loop writes llm_base_url again"
        assert 'settings.unset("llm_base_url")' in src, "saving must delete a pre-1.7.0 row"
        assert not re.search(r'text_input\(\s*"Base URL', src), "the base URL field is back"


# ── brief-parse degradation ──────────────────────────────────────


class TestBriefParsing:
    def test_valid_json_passes_through(self):
        payload = advisor.parse_brief_text('{"summary": "s", "insights": [], "suggestions": [], "markdown": "m"}')
        assert payload["summary"] == "s" and payload.get("parse_error") is None

    def test_code_fenced_json(self):
        payload = advisor.parse_brief_text('```json\n{"summary": "fenced"}\n```')
        assert payload["summary"] == "fenced"
        assert payload["insights"] == [] and payload["suggestions"] == []

    def test_prose_degrades_to_markdown(self, fake_db):
        fake_db({})
        payload = advisor.parse_brief_text("Here is your brief:\n- everything is fine")
        assert payload["parse_error"] is True
        assert "everything is fine" in payload["markdown"]
        assert payload["insights"] == [] and payload["suggestions"] == []

    def test_json_array_degrades(self, fake_db):
        fake_db({})
        payload = advisor.parse_brief_text('["not", "an", "object"]')
        assert payload["parse_error"] is True

    def test_missing_keys_filled(self):
        payload = advisor.parse_brief_text('{"summary": "only summary"}')
        assert payload["insights"] == []
        assert payload["suggestions"] == []
        assert payload["markdown"]


# ── scheduled-job degradation (keyless install) ───────────────────


class TestAdvisorDisabledReason:
    def test_none_when_key_present(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "anthropic"})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        assert advisor._advisor_disabled_reason() is None

    def test_reason_names_the_env_var_when_missing(self, fake_db):
        fake_db({"llm_provider": "openai"})
        reason = advisor._advisor_disabled_reason()
        assert reason and "OPENAI_API_KEY" in reason

    def test_local_provider_needs_no_key(self, fake_db):
        fake_db({"llm_provider": "ollama"})
        assert advisor._advisor_disabled_reason() is None


# ── dashboard regressions (found by driving the UI in a browser) ──


class TestDashboardAdvisorWiring:
    """The Advisor tab reaches into advisor/llm; these are the names it uses.

    Every one of these was broken at some point: the tab read
    advisor.DEFAULT_MODEL (removed when the provider layer landed, so the tab
    raised AttributeError for anyone who had a key) and gated itself on
    ANTHROPIC_API_KEY (so no other provider could ever be used from the UI).
    """

    def test_names_the_dashboard_depends_on_exist(self):
        assert hasattr(advisor, "_advisor_disabled_reason")
        assert hasattr(advisor, "PHILOSOPHY_PATH")
        assert callable(llm.provider) and callable(llm.model)
        # The attribute the tab used to read must stay gone — if it comes back,
        # someone has reintroduced a second source of truth for the model.
        assert not hasattr(advisor, "DEFAULT_MODEL")

    def test_local_provider_is_usable_without_any_key(self, fake_db, monkeypatch):
        """Ollama needs no key, so the tab must not refuse to render."""
        fake_db({"llm_provider": "ollama"})
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert advisor._advisor_disabled_reason() is None
        assert llm.provider() == "ollama"

    def test_openai_key_alone_is_enough(self, fake_db, monkeypatch):
        fake_db({"llm_provider": "openai"})
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        assert advisor._advisor_disabled_reason() is None


class TestSettingsFallback:
    """settings.fallback() is what stops the Settings page writing overrides
    for fields the operator never touched."""

    def test_fallback_ignores_the_database(self, fake_db, monkeypatch):
        fake_db({"display_name": "From DB"})
        monkeypatch.setenv("PORTFOLIODB_DISPLAY_NAME", "From Env")
        # get() prefers the DB; fallback() answers "what if there were no row?"
        assert settings.get("display_name", env="PORTFOLIODB_DISPLAY_NAME") == "From DB"
        assert settings.fallback("display_name", env="PORTFOLIODB_DISPLAY_NAME") == "From Env"

    def test_fallback_returns_default_when_env_absent(self, fake_db):
        fake_db({})
        assert settings.fallback("display_name", env="PORTFOLIODB_DISPLAY_NAME",
                                 default="Operator") == "Operator"

    def test_env_value_needs_no_override(self, fake_db, monkeypatch):
        """The save path's rule: a submitted value equal to the fallback should
        not become a DB row."""
        fake_db({})
        monkeypatch.setenv("PORTFOLIODB_DISPLAY_NAME", "Demo Operator")
        submitted = "Demo Operator"          # what the pre-filled field returns
        assert submitted == settings.fallback("display_name", env="PORTFOLIODB_DISPLAY_NAME")
