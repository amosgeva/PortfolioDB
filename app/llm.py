"""Provider-agnostic LLM layer for the advisor (public-repo plan §7).

Two code paths, not N:

1. Native ``anthropic`` SDK — the best Claude experience (system content
   blocks with prompt caching, first-class streaming).
2. One OpenAI-compatible path via the ``openai`` SDK with a configurable
   ``base_url`` — covers OpenAI, OpenRouter (300+ models), Ollama / LM Studio
   for fully-local models, Groq, Mistral, DeepSeek, and any other server
   speaking the chat-completions dialect.

Provider and model are runtime-switchable from the Settings page
(DB → env → default via app/settings.py). **API keys are env-only** — they
never live in the database because the dashboard has no auth.

**The base URL is env-only too** (``LLM_BASE_URL``), and for the same reason.
It used to be a Settings-page field, and the page has no login: anyone who
could open the dashboard could point ``openai`` at a server they controlled
and trigger a brief, and the OpenAI-compatible client would send them
``OPENAI_API_KEY`` as a Bearer header (audit F01, 2026-09-09). A URL typed
into an unauthenticated form must never decide where a secret goes. On top
of that, ``_key_for_destination`` binds each vendor's named key to that
vendor's origin: an ``LLM_BASE_URL`` that points ``openai`` or ``openrouter``
elsewhere gets only the generic ``LLM_API_KEY``, never the vendor's.

The advisor's value is its context assembly, which stays in advisor.py; this
module only owns "given system blocks + messages, produce text".
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import settings
from db import parse_env_line

log = logging.getLogger(__name__)

_env_loaded = False


def _load_env_file_once() -> None:
    """Populate missing env vars from the repo-root .env — ALL keys, unlike
    db.py's PORTFOLIODB_*-only loader, because the LLM keys (ANTHROPIC_API_KEY
    etc.) don't carry the prefix. Non-overriding: caller env wins. Makes a
    bare `python advisor.py brief` work without a launcher."""
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    env_path = Path(__file__).resolve().parent.parent / ".env"
    with contextlib.suppress(OSError):
        for line in env_path.read_text(encoding="utf-8").splitlines():
            parsed = parse_env_line(line)
            if parsed and not os.getenv(parsed[0]):
                os.environ[parsed[0]] = parsed[1]


PROVIDERS = ("anthropic", "openai", "openrouter", "ollama", "custom")

_DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5",
    "openrouter": "anthropic/claude-sonnet-5",
    "ollama": "llama3.3",
    "custom": "",
}

_DEFAULT_BASE_URL = {
    # Written out rather than left to the SDK's default so the origin check in
    # _key_for_destination has something to compare against.
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    # From a container, host Ollama is http://host.docker.internal:11434/v1
    # (on Linux add `extra_hosts: host.docker.internal:host-gateway`).
    "ollama": "http://localhost:11434/v1",
}

# Vendors whose named key must only ever reach that vendor's own origin.
_VENDOR_BOUND = frozenset({"openai", "openrouter"})

# First non-empty env var wins. LLM_API_KEY is the generic knob; the
# provider-specific names keep existing setups working unchanged.
_KEY_ENVS = {
    "anthropic": ("LLM_API_KEY", "ANTHROPIC_API_KEY"),
    "openai": ("LLM_API_KEY", "OPENAI_API_KEY"),
    "openrouter": ("LLM_API_KEY", "OPENROUTER_API_KEY"),
    "ollama": ("LLM_API_KEY",),
    "custom": ("LLM_API_KEY",),
}

# Providers that work without a key (a local server doesn't need one).
_KEY_OPTIONAL = frozenset({"ollama", "custom"})


def provider() -> str:
    p = (settings.get("llm_provider", env="LLM_PROVIDER", default="anthropic") or "").lower()
    return p if p in PROVIDERS else "anthropic"


def model(p: str | None = None) -> str:
    p = p or provider()
    # PORTFOLIODB_ADVISOR_MODEL is the pre-Phase-2 name, kept as an alias.
    return settings.get(
        "llm_model",
        env=("LLM_MODEL", "PORTFOLIODB_ADVISOR_MODEL"),
        default=_DEFAULT_MODEL.get(p, ""),
    ) or _DEFAULT_MODEL["anthropic"]


def api_key(p: str | None = None) -> str | None:
    _load_env_file_once()
    p = p or provider()
    for name in _KEY_ENVS.get(p, ()):
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return None


_stale_base_url_row_warned = False


def base_url(p: str | None = None) -> str | None:
    """Where the OpenAI-compatible client connects: ``LLM_BASE_URL`` or the
    provider's default. Never the database — see the module docstring.

    A ``llm_base_url`` row left over from before this change is ignored, and
    said so once in the log; saving the Settings page deletes it.
    """
    global _stale_base_url_row_warned
    p = p or provider()
    if not _stale_base_url_row_warned and settings.source_of("llm_base_url") == "db":
        _stale_base_url_row_warned = True
        log.warning(
            "Ignoring llm_base_url from the settings table: the advisor's base URL "
            "is read from LLM_BASE_URL in .env only, because a URL set from the "
            "unauthenticated dashboard could receive the provider's API key. Save "
            "the Settings page once to remove the stale row."
        )
    return settings.fallback("llm_base_url", env="LLM_BASE_URL", default=_DEFAULT_BASE_URL.get(p))


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _key_for_destination(p: str, url: str | None) -> str | None:
    """The key allowed to travel to ``url`` when the provider is ``p``.

    A vendor's named key (OPENAI_API_KEY, OPENROUTER_API_KEY) goes only to
    that vendor's origin. Point the provider anywhere else with LLM_BASE_URL
    and the request carries the generic LLM_API_KEY or nothing — a proxy or a
    self-hosted server gets a credential meant for it, never one meant for
    the vendor it imitates. Keyless providers (ollama, custom) only ever had
    the generic key, so nothing changes for them.
    """
    if p not in _VENDOR_BOUND:
        return _require_key(p)
    default = _DEFAULT_BASE_URL[p]
    if url is None or _origin(url) == _origin(default):
        return _require_key(p)
    generic = os.environ.get("LLM_API_KEY", "").strip()
    if generic:
        return generic
    named = _KEY_ENVS[p][-1]
    raise RuntimeError(
        f"LLM_BASE_URL points the '{p}' provider at {_origin(url)}, not "
        f"{_origin(default)}. {named} stays with its vendor; set LLM_API_KEY "
        "for that server, or unset LLM_BASE_URL."
    )


def key_status() -> dict:
    """For the Settings page: which key the active provider would use.

    Never returns a key value — only whether one is set and under which
    env var name.
    """
    _load_env_file_once()
    p = provider()
    for name in _KEY_ENVS.get(p, ()):
        if os.environ.get(name, "").strip():
            return {"provider": p, "set": True, "env_var": name, "optional": p in _KEY_OPTIONAL}
    return {
        "provider": p,
        "set": False,
        "env_var": _KEY_ENVS[p][-1] if _KEY_ENVS.get(p) else "LLM_API_KEY",
        "optional": p in _KEY_OPTIONAL,
    }


def _flatten_system(system_blocks: list[dict]) -> str:
    """Anthropic-style system content blocks → one system string.

    The OpenAI dialect has no content blocks or cache_control; the text is
    what matters.
    """
    return "\n\n".join(b.get("text", "") for b in system_blocks if b.get("text"))


def _require_key(p: str) -> str | None:
    key = api_key(p)
    if key is None and p not in _KEY_OPTIONAL:
        names = " or ".join(_KEY_ENVS[p])
        raise RuntimeError(
            f"No API key for LLM provider '{p}'. Set {names} in the repo-root .env."
        )
    return key


def _anthropic_client():
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "anthropic package not installed. Run: pip install -r requirements.txt"
        ) from e
    return anthropic.Anthropic(api_key=_require_key("anthropic"))


def _openai_client(p: str, http_client: Any = None):
    # This module IS the LLM integration point (see docstring) — AI-SDK usage
    # here is intentional and reviewed, hence the nosemgrep markers for
    # Codacy's AI-usage detection rules.
    try:
        from openai import OpenAI  # nosemgrep
    except ImportError as e:
        raise RuntimeError(
            "openai package not installed. Run: pip install -r requirements.txt"
        ) from e
    url = base_url(p)
    # The SDK insists on some api_key; local servers ignore it. `http_client`
    # is for tests, which hand in an in-memory transport and read the request.
    return OpenAI(  # nosemgrep
        api_key=_key_for_destination(p, url) or "not-needed",
        base_url=url,
        http_client=http_client,
    )


def _openai_token_param(p: str, max_tokens: int) -> dict:
    # OpenAI proper renamed the knob; the compat ecosystem (OpenRouter,
    # Ollama, Groq, ...) still speaks max_tokens.
    if p == "openai":
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def complete(system_blocks: list[dict], messages: list[dict], *, max_tokens: int = 8000) -> str:
    """One-shot completion → the response text."""
    p = provider()
    if p == "anthropic":
        resp = _anthropic_client().messages.create(
            model=model(p),
            max_tokens=max_tokens,
            system=system_blocks,
            messages=messages,
        )
        # content may start with a thinking block — take the first text block.
        return next((b.text for b in resp.content if b.type == "text"), "").strip()

    client = _openai_client(p)
    resp = client.chat.completions.create(  # nosemgrep
        model=model(p),
        messages=[{"role": "system", "content": _flatten_system(system_blocks)}] + messages,
        **_openai_token_param(p, max_tokens),
    )
    return (resp.choices[0].message.content or "").strip()


def stream(system_blocks: list[dict], messages: list[dict], *, max_tokens: int = 8000) -> Iterator[str]:
    """Streaming completion → an iterator of text chunks."""
    p = provider()
    if p == "anthropic":
        client = _anthropic_client()
        with client.messages.stream(
            model=model(p),
            max_tokens=max_tokens,
            system=system_blocks,
            messages=messages,
        ) as s:
            yield from s.text_stream
        return

    client = _openai_client(p)
    resp = client.chat.completions.create(  # nosemgrep
        model=model(p),
        messages=[{"role": "system", "content": _flatten_system(system_blocks)}] + messages,
        stream=True,
        **_openai_token_param(p, max_tokens),
    )
    for chunk in resp:
        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content
