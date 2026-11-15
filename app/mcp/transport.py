"""The HTTP application uvicorn serves, around a FastMCP server.

Kept out of server.py (which builds the production server and warms the pool
at import) so tests can wrap a small server in exactly the production
transport: the Host/Origin guard with an explicit allowlist, and the
failed-auth throttle in front of the token check.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP
from starlette.middleware import Middleware as ASGIMiddleware

from app.mcp.ratelimit import FailedAuthLimiter

ALLOWED_HOSTS_ENV = "PORTFOLIODB_MCP_ALLOWED_HOSTS"


def allowed_hosts() -> list[str]:
    """Extra Host-header values to accept, from PORTFOLIODB_MCP_ALLOWED_HOSTS.

    Always an explicit list — empty by default — because an explicit list is
    what makes FastMCP validate the Host header on *every* request. Left
    unset, its guard only checks requests that arrive on a loopback socket;
    inside the compose container the server binds 0.0.0.0, so the guard was
    off and /healthz was reachable by DNS rebinding (audit M-12). With the
    list, loopback names (localhost, 127.0.0.1, ::1) are accepted, as is the
    exact bind address when it is not a wildcard, and anything else gets 421.

    To serve other machines, name them: e.g.
    ``PORTFOLIODB_MCP_ALLOWED_HOSTS=192.168.1.20,nas.lan``. ``*`` accepts any
    Host (the old behaviour) and is an explicit opt-out.
    """
    raw = os.getenv(ALLOWED_HOSTS_ENV, "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def build_asgi(server: FastMCP):
    """The Starlette app: MCP at /mcp/, /healthz, host guard, auth throttle.

    ``host_origin_protection=True`` is explicit on purpose: FastMCP 4.2 ships
    the guard *off* by default (``http_host_origin_protection = False``), so
    relying on its default — as the server did — meant no Host check at all.
    """
    return server.http_app(
        transport="streamable-http",
        host_origin_protection=True,
        allowed_hosts=allowed_hosts(),
        middleware=[ASGIMiddleware(FailedAuthLimiter)],
    )
