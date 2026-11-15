"""The unauthenticated ``/healthz`` route.

A tunnel, a load balancer or the compose healthcheck needs to ask "are you up"
without holding the bearer token, so this route is open by design. Open means
it must say as little as possible: ``health_service.liveness()`` is the whole
payload, and an exception on the way is logged, never served — psycopg2's text
names the host and the role, and a traceback names whatever it likes.

Open also means it must not be a lever on the server. The probe is blocking
database work; it used to run directly on the event loop, so a slow or
unreachable Postgres let any unauthenticated caller stall every authenticated
request (audit M-1). Now:

* the probe runs in a worker thread, never on the loop;
* at most one probe is in flight — concurrent callers await the same one, so a
  flood of requests cannot pile up blocked threads;
* each caller waits at most ``DEADLINE_S`` and then gets ``db: "timeout"``
  (503) while the probe finishes in the background;
* a result is served from cache for ``CACHE_TTL_S``, so polling costs at most
  one database round trip per interval.

Kept out of server.py so a test can mount it on a bare FastMCP and hit it over
HTTP: importing server.py initialises the connection pool as a side effect.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.mcp.services import health as health_service

log = logging.getLogger("portfoliodb.mcp")

# What the route serves when it cannot even run the probe. Same shape as a
# normal answer so a monitor parsing it never sees a surprise key.
_UNKNOWN = {"ok": False, "db": "unknown", "last_snapshot_age_s": None}
_TIMEOUT = {"ok": False, "db": "timeout", "last_snapshot_age_s": None}


def _env_seconds(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return value if value >= 0 else default


DEADLINE_S = _env_seconds("PORTFOLIODB_MCP_HEALTHZ_DEADLINE_S", 3.0)
CACHE_TTL_S = _env_seconds("PORTFOLIODB_MCP_HEALTHZ_CACHE_S", 2.0)

_state: dict[str, Any] = {"at": 0.0, "payload": None, "future": None, "loop": None}


def reset_cache() -> None:
    """Forget the cached probe and any in-flight one. For tests."""
    _state.update(at=0.0, payload=None, future=None, loop=None)


def _probe_blocking() -> dict[str, Any]:
    """Runs in a worker thread. Never raises: an exception is logged and the
    shape-stable unknown payload is returned instead."""
    try:
        return health_service.liveness()
    except Exception:
        log.exception("healthz failed")
        return dict(_UNKNOWN)


async def probe() -> dict[str, Any]:
    """The liveness payload, bounded in time and shared between callers."""
    now = time.monotonic()
    cached = _state["payload"]
    if cached is not None and now - _state["at"] < CACHE_TTL_S:
        return dict(cached)

    loop = asyncio.get_running_loop()
    future = _state["future"]
    if future is None or future.done() or _state["loop"] is not loop:
        future = loop.run_in_executor(None, _probe_blocking)
        _state.update(future=future, loop=loop)

    try:
        payload = await asyncio.wait_for(asyncio.shield(future), DEADLINE_S)
    except asyncio.TimeoutError:
        log.warning("healthz probe exceeded %.1fs; answering 'timeout'", DEADLINE_S)
        return dict(_TIMEOUT)
    _state.update(at=time.monotonic(), payload=dict(payload))
    return dict(payload)


def register(mcp: FastMCP) -> None:
    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        """Unauthenticated liveness probe: ok / db / last_snapshot_age_s.

        200 when the database answers, 503 otherwise (including when the probe
        does not finish within the deadline). For counts, the last run's error
        text and per-table freshness, call the ``get_health`` tool with the
        bearer token.
        """
        payload = await probe()
        return JSONResponse(payload, status_code=200 if payload.get("ok") else 503)
