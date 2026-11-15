"""Throttle repeated failed authentication, per client address.

A static bearer token is only as good as the number of guesses an attacker
gets. Nothing used to count them (audit M-9). This ASGI middleware watches the
responses: every 401 from the MCP endpoint is a failed attempt for that client
address, and a client that fails too often inside the window is answered 429
for a cool-down period without its request reaching the token check at all.

Deliberately simple — in-process, per worker, keyed on the peer address the
ASGI server reports. It is a brake on brute force against a long random token,
not an identity system. A successful request does not reset the count, so
interleaving one good guess cannot launder bad ones; the window does.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_FAILURES_ENV = "PORTFOLIODB_MCP_AUTH_MAX_FAILURES"
WINDOW_ENV = "PORTFOLIODB_MCP_AUTH_WINDOW_S"
DEFAULT_MAX_FAILURES = 10
DEFAULT_WINDOW_S = 60.0


def _env_number(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return value if value > 0 else default


class FailedAuthLimiter:
    """ASGI middleware: 429 for a client with too many recent 401s."""

    def __init__(
        self,
        app: ASGIApp,
        max_failures: int | None = None,
        window_s: float | None = None,
        clock=time.monotonic,
    ) -> None:
        self.app = app
        self.max_failures = int(max_failures or _env_number(MAX_FAILURES_ENV, DEFAULT_MAX_FAILURES))
        self.window_s = float(window_s or _env_number(WINDOW_ENV, DEFAULT_WINDOW_S))
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _client(self, scope: Scope) -> str:
        client = scope.get("client")
        return str(client[0]) if client else "unknown"

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._failures.setdefault(key, deque())
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def blocked_for(self, key: str) -> float:
        """Seconds until ``key`` may try again; 0 when it is not blocked."""
        now = self._clock()
        with self._lock:
            q = self._recent(key, now)
            if len(q) < self.max_failures:
                if not q:
                    self._failures.pop(key, None)
                return 0.0
            return max(0.0, self.window_s - (now - q[0]))

    def record_failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            self._recent(key, now).append(now)
            # Bound memory: an address that stopped trying is dropped once its
            # window lapses, and the table never grows without limit.
            if len(self._failures) > 10_000:
                for k in list(self._failures)[: len(self._failures) // 2]:
                    self._failures.pop(k, None)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        key = self._client(scope)
        wait = self.blocked_for(key)
        if wait > 0:
            response = JSONResponse(
                {"error": "too_many_failed_auth_attempts", "retry_after_s": int(wait) + 1},
                status_code=429,
                headers={"Retry-After": str(int(wait) + 1)},
            )
            await response(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and message.get("status") == 401:
                self.record_failure(key)
            await send(message)

        await self.app(scope, receive, send_wrapper)
