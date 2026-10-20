"""The advisor's two LLM paths, driven through the real SDKs.

test_settings_llm.py covers which provider, model and key are chosen, mostly
with fake clients. This file is the other half: the real ``anthropic`` and
``openai`` packages build the request and parse the response, over an
in-memory transport. No network, no real key.

It exists for dependency upgrades. Both SDKs ship breaking majors, and a fake
client agrees with whatever llm.py calls — so a renamed method, a changed
streaming helper or a new response shape would pass every other test and fail
the first brief after the upgrade.
"""

from __future__ import annotations

import functools
import importlib
import json
import time

import pytest

import llm
import settings

SYSTEM = [
    {"type": "text", "text": "sys A", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "sys B"},
]
MESSAGES = [{"role": "user", "content": "q"}]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # Same isolation as test_settings_llm.py: no operator .env, no DB row.
    monkeypatch.setattr(llm, "_env_loaded", True)
    for name in (
        "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY",
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
        "PORTFOLIODB_ADVISOR_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def use_settings(monkeypatch):
    def seed(values: dict[str, str]):
        monkeypatch.setattr(settings, "_cache", dict(values))
        monkeypatch.setattr(settings, "_db_ok", True)
        monkeypatch.setattr(settings, "_cache_at", time.monotonic())
    return seed


def _sse(events: list[tuple[str | None, dict | str]]) -> bytes:
    out = []
    for event, data in events:
        body = data if isinstance(data, str) else json.dumps(data)
        out.append((f"event: {event}\n" if event else "") + f"data: {body}\n\n")
    return "".join(out).encode()


def _http_for(sdk):
    """The HTTP package `sdk` is built on. anthropic 1.x and openai 3.x moved
    from httpx to its fork httpx2, and anthropic rejects a client from the
    other package, so the in-memory transport comes from whichever one the
    installed SDK uses."""
    base = sdk.DefaultHttpxClient.__mro__[1]
    return importlib.import_module(base.__module__.partition(".")[0])


class _Recorder:
    """A mock transport that keeps every request and answers with
    `reply(request, http)`, where `http` is the SDK's HTTP package."""

    def __init__(self, sdk, reply):
        self.http = _http_for(sdk)
        self.requests: list = []
        self._reply = reply

    def __call__(self, request):
        self.requests.append(request)
        return self._reply(request, self.http)

    def client(self):
        return self.http.Client(transport=self.http.MockTransport(self))

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


# ── anthropic ─────────────────────────────────────────────────────


def _anthropic_message() -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
        # A thinking block first: complete() must take the first *text* block.
        "content": [
            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
            {"type": "text", "text": "  the brief  "},
        ],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }


def _anthropic_stream() -> bytes:
    msg = {**_anthropic_message(), "content": [], "stop_reason": None}
    return _sse([
        ("message_start", {"type": "message_start", "message": msg}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "Hel"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "lo"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 2}}),
        ("message_stop", {"type": "message_stop"}),
    ])


@pytest.fixture
def anthropic_server(use_settings, monkeypatch):
    """Route llm's own `anthropic.Anthropic(api_key=...)` call through a recorder."""
    import anthropic

    use_settings({"llm_provider": "anthropic", "llm_model": "claude-test"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def reply(request, http):
        if json.loads(request.content).get("stream"):
            return http.Response(200, content=_anthropic_stream(),
                                 headers={"content-type": "text/event-stream"})
        return http.Response(200, json=_anthropic_message())

    rec = _Recorder(anthropic, reply)
    monkeypatch.setattr(anthropic, "Anthropic",
                        functools.partial(anthropic.Anthropic, http_client=rec.client()))
    return rec


class TestAnthropicSdk:
    def test_complete_sends_the_messages_request(self, anthropic_server):
        llm.complete(SYSTEM, MESSAGES, max_tokens=321)

        req = anthropic_server.requests[-1]
        assert req.method == "POST" and req.url.path == "/v1/messages"
        assert req.headers["x-api-key"] == "sk-ant-test"
        body = anthropic_server.body
        assert body["model"] == "claude-test"
        assert body["max_tokens"] == 321
        # System blocks go through untouched: cache_control is the point of
        # the native path.
        assert body["system"] == SYSTEM
        assert body["messages"] == MESSAGES

    def test_complete_returns_the_first_text_block_stripped(self, anthropic_server):
        assert llm.complete(SYSTEM, MESSAGES) == "the brief"

    def test_stream_yields_the_text_deltas(self, anthropic_server):
        assert list(llm.stream(SYSTEM, MESSAGES, max_tokens=99)) == ["Hel", "lo"]
        body = anthropic_server.body
        assert body["stream"] is True
        assert body["max_tokens"] == 99
        assert body["system"] == SYSTEM


# ── openai-compatible ─────────────────────────────────────────────


def _chat_completion() -> dict:
    return {
        "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "  answer  "}}],
    }


def _chunk(choices: list) -> dict:
    return {"id": "c1", "object": "chat.completion.chunk", "created": 0,
            "model": "m", "choices": choices}


def _chat_stream() -> bytes:
    return _sse([
        (None, _chunk([{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}])),
        (None, _chunk([{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}])),
        (None, _chunk([{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}])),
        (None, _chunk([{"index": 0, "delta": {}, "finish_reason": "stop"}])),
        # A usage-only chunk carries no choices; stream() must skip it.
        (None, _chunk([])),
        (None, "[DONE]"),
    ])


@pytest.fixture
def openai_server(use_settings, monkeypatch):
    import openai

    def reply(request, http):
        if json.loads(request.content).get("stream"):
            return http.Response(200, content=_chat_stream(),
                                 headers={"content-type": "text/event-stream"})
        return http.Response(200, json=_chat_completion())

    rec = _Recorder(openai, reply)
    real = llm._openai_client
    monkeypatch.setattr(llm, "_openai_client", lambda p: real(p, http_client=rec.client()))

    def start(provider: str):
        use_settings({"llm_provider": provider, "llm_model": "gpt-test"})
        return rec
    return start


class TestOpenAiSdk:
    def test_complete_flattens_the_system_blocks(self, openai_server, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        rec = openai_server("openai")

        assert llm.complete(SYSTEM, MESSAGES, max_tokens=50) == "answer"

        req = rec.requests[-1]
        assert str(req.url) == "https://api.openai.com/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer sk-openai-test"
        body = rec.body
        assert body["model"] == "gpt-test"
        assert body["messages"] == [{"role": "system", "content": "sys A\n\nsys B"}] + MESSAGES
        assert body["max_completion_tokens"] == 50
        assert "max_tokens" not in body

    def test_compat_server_gets_max_tokens(self, openai_server):
        rec = openai_server("ollama")
        llm.complete(SYSTEM, MESSAGES, max_tokens=50)
        assert str(rec.requests[-1].url) == "http://localhost:11434/v1/chat/completions"
        assert rec.body["max_tokens"] == 50
        assert "max_completion_tokens" not in rec.body

    def test_stream_yields_only_content_deltas(self, openai_server):
        rec = openai_server("ollama")
        assert list(llm.stream(SYSTEM, MESSAGES, max_tokens=10)) == ["Hel", "lo"]
        assert rec.body["stream"] is True
