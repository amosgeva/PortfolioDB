"""What an MCP client is told when a call fails.

The server runs with FastMCP's ``mask_error_details=True``: an unexpected
exception reaches the client as "Error calling tool 'x'" and nothing more,
because psycopg2's text names the database host and the role, and a traceback
names whatever it likes (audit M-7). The full exception is logged server-side.

Masking everything would also hide the answers a caller needs to correct its
own request, so two kinds of failure are let through, rewritten:

* ``ValueError`` — the services' argument validation ("window must be one of
  ..."). Our text, describing the caller's input.
* ``psycopg2.Error`` — replaced by ``deps.explain_db_error``: a category and an
  action ("A query exceeded the server's statement timeout"), never the
  driver's message.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg2
from fastmcp.exceptions import PromptError, ResourceError, ToolError
from fastmcp.server.middleware import Middleware

from app.mcp.deps import explain_db_error

log = logging.getLogger("portfoliodb.mcp")


def _root(exc: BaseException) -> BaseException:
    """The original exception under FastMCP's masking wrapper, if any."""
    cause = exc.__cause__
    return cause if cause is not None else exc


def client_message(exc: BaseException) -> str | None:
    """The text a client may see for ``exc``, or None to keep it masked."""
    root = _root(exc)
    if isinstance(root, psycopg2.Error):
        return explain_db_error(root)
    if isinstance(root, ValueError):
        text = str(root).strip()
        return text.splitlines()[0][:500] if text else "Invalid argument."
    return None


class ClientSafeErrors(Middleware):
    """Re-raise validation and database failures with a client-safe message."""

    async def _guard(self, call_next, context, error_cls) -> Any:
        try:
            return await call_next(context)
        except (ToolError, ResourceError, PromptError, ValueError, psycopg2.Error) as e:
            message = client_message(e)
            if message is None:
                raise
            raise error_cls(message) from e

    async def on_call_tool(self, context, call_next):
        return await self._guard(call_next, context, ToolError)

    async def on_read_resource(self, context, call_next):
        return await self._guard(call_next, context, ResourceError)

    async def on_get_prompt(self, context, call_next):
        return await self._guard(call_next, context, PromptError)
