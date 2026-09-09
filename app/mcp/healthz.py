"""The unauthenticated ``/healthz`` route.

A tunnel, a load balancer or the compose healthcheck needs to ask "are you up"
without holding the bearer token, so this route is open by design. Open means
it must say as little as possible: ``health_service.liveness()`` is the whole
payload, and an exception on the way is logged, never served — psycopg2's text
names the host and the role, and a traceback names whatever it likes.

Kept out of server.py so a test can mount it on a bare FastMCP and hit it over
HTTP: importing server.py initialises the connection pool as a side effect.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.mcp.services import health as health_service

log = logging.getLogger("portfoliodb.mcp")

# What the route serves when it cannot even run the probe. Same shape as a
# normal answer so a monitor parsing it never sees a surprise key.
_UNKNOWN = {"ok": False, "db": "unknown", "last_snapshot_age_s": None}


def register(mcp: FastMCP) -> None:
    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        """Unauthenticated liveness probe: ok / db / last_snapshot_age_s.

        200 when the database answers, 503 otherwise. For counts, the last
        run's error text and per-table freshness, call the ``get_health``
        tool with the bearer token.
        """
        try:
            payload = health_service.liveness()
        except Exception:
            log.exception("healthz failed")
            return JSONResponse(dict(_UNKNOWN), status_code=503)
        return JSONResponse(payload, status_code=200 if payload.get("ok") else 503)
