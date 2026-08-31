"""Bearer-token auth for the PortfolioDB MCP server.

Single shared token loaded from PORTFOLIODB_MCP_TOKEN. The token is compared
in constant time to defeat timing oracles. There is no OAuth dance — clients
just send `Authorization: Bearer <token>` with every request.

FastMCP applies this verifier to the MCP protocol endpoints. Routes registered
with @mcp.custom_route (e.g. /healthz) are unauthenticated by design.
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastmcp.server.auth import AccessToken, TokenVerifier

# Make the sibling app/db.py importable (same trick as deps.py).
# Append, not insert(0): see deps.py — prepending lets the local app/mcp
# package shadow the installed `mcp` SDK that fastmcp needs.
_APP_DIR = Path(__file__).resolve().parent.parent
if str(_APP_DIR) not in sys.path:
    sys.path.append(str(_APP_DIR))

from db import _load_env_file_if_needed, parse_env_line  # noqa: E402

log = logging.getLogger(__name__)

TOKEN_ENV_VAR = "PORTFOLIODB_MCP_TOKEN"
DEFAULT_CLIENT_ID = "portfoliodb-mcp"


class StaticBearerVerifier(TokenVerifier):
    """Verify a single shared Bearer token via constant-time compare.

    Args:
        token: The expected bearer token. Must be non-empty.
        client_id: The principal recorded on the AccessToken (informational).
    """

    def __init__(self, token: str, client_id: str = DEFAULT_CLIENT_ID):
        super().__init__(required_scopes=None)
        if not token:
            raise ValueError("StaticBearerVerifier requires a non-empty token")
        self._token = token
        self._client_id = client_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        if not hmac.compare_digest(token, self._token):
            return None
        # No real expiry on a static token, but the SDK expects an int — give
        # it a far-future timestamp so framework-level expiry checks pass.
        far_future = int(
            (datetime.now(timezone.utc) + timedelta(days=365 * 10)).timestamp()
        )
        return AccessToken(
            token=token,
            client_id=self._client_id,
            scopes=[],
            expires_at=far_future,
        )


def load_token() -> str:
    """Read PORTFOLIODB_MCP_TOKEN from env (with .env fallback)."""
    if not os.getenv(TOKEN_ENV_VAR):
        # Reuse db.py's .env loader so we honor the same .env file. It only
        # populates PORTFOLIODB_* vars and skips already-set ones.
        try:
            _load_env_file_if_needed()
            _load_env_file_for_mcp()
        except Exception:
            log.warning("Could not load .env for MCP token", exc_info=True)
    token = os.getenv(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise RuntimeError(
            f"{TOKEN_ENV_VAR} is not set. Add it to .env (repo root) or export it "
            "before starting the MCP server."
        )
    return token


def _load_env_file_for_mcp() -> None:
    """Populate PORTFOLIODB_MCP_* env vars from the repo-root .env file.

    db.py's loader only sets PORTFOLIODB_* prefixed vars; we extend that to
    cover the MCP-specific knobs (token, port).
    """
    if os.getenv(TOKEN_ENV_VAR):
        return
    env_path = _APP_DIR.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = parse_env_line(line)
        if parsed is None:
            continue
        key, val = parsed
        if key.startswith("PORTFOLIODB_MCP_") and not os.getenv(key):
            os.environ[key] = val


def build_verifier() -> StaticBearerVerifier:
    """Convenience constructor used by server.py."""
    return StaticBearerVerifier(load_token())
