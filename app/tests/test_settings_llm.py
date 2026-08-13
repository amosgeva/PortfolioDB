"""Settings precedence, LLM provider selection, and brief-parse degradation.

No live database: settings' DB layer is faked by seeding the module cache
directly (the resolution logic under test is get()/source_of(), not psycopg2).
"""

from __future__ import annotations

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
