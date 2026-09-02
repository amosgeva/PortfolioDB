"""What build this is — for the dashboard footer and the MCP provenance block.

Two different answers, because two different questions get asked.

`release_version()` answers "which release am I on", and is what a person reads
in the UI: a bare `1.1.5`. It comes from `server.json`, which is already the
file bumped at every release, so this adds no third place to keep in step —
the changelog's own history has enough entries about versions written down
twice and drifting apart.

`build_stamp()` answers "exactly which build is running", which is the question
you need when a number looks wrong: `v1.1.5@2326d53` from a tagged image,
`main@ad075c3` from main, a bare commit on a host with `.git`, and `unknown`
when a deployed tree has neither. `unknown` is reported rather than hidden — a
build that cannot identify itself is worth saying so about, and that is exactly
the situation where you most want to know.

This module lives in `app/` rather than beside its previous home in
`app/mcp/services/cutoff.py` so the dashboard can import it without pulling in
`app.mcp.deps` — a database pool and the MCP package, which on the dashboard's
import path shadows the official `mcp` SDK (see CLAUDE.md).

Neither function is cached here. `cutoff.app_version` keeps its own lru_cache,
because a server process cannot change build mid-flight; the dashboard re-reads
per render, which is one small file next to the work a render already does.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

# app/version.py -> app/ -> repo root (or /app in the image, where the
# Dockerfile places server.json alongside the app/ tree).
_ROOT = Path(__file__).resolve().parent.parent
_SERVER_JSON = _ROOT / "server.json"


def release_version() -> str:
    """The release number alone, e.g. `1.1.5`.

    Falls back to the build stamp when server.json is missing or unreadable:
    an exact commit is a worse answer to "which release" but a much better one
    than nothing, and it never leaves the footer blank.
    """
    try:
        version = json.loads(_SERVER_JSON.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        version = None
    return version or build_stamp()


def build_stamp() -> str:
    """Best-effort build identifier.

    PORTFOLIODB_APP_VERSION wins, so a container image can stamp itself.
    Otherwise the commit is read straight out of the .git directory — no
    subprocess. Shelling out to `git describe` would put process execution on
    a request path in a server that is otherwise read-only and does nothing but
    query Postgres; reading two small files gets the same answer without that.

    Returns 'unknown' when neither is available (a deployed tree with no .git).
    An unidentified build is worth reporting honestly and is not worth failing
    a request over.
    """
    pinned = os.getenv("PORTFOLIODB_APP_VERSION")
    if pinned:
        return pinned
    return git_head_sha() or "unknown"


def git_head_sha(short: int = 7) -> str | None:
    """Current commit from .git, or None if it cannot be determined.

    Handles the three shapes HEAD takes: a symbolic ref into refs/heads, a
    detached raw SHA, and a branch whose ref has been packed into packed-refs.
    """
    git_dir = _ROOT / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not head.startswith("ref:"):
        # Detached HEAD — the file already holds the SHA.
        return head[:short] or None

    ref = head[4:].strip()
    with contextlib.suppress(OSError):
        return (git_dir / ref).read_text(encoding="utf-8").strip()[:short] or None

    # Ref was packed away by `git gc`; scan packed-refs for it.
    try:
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.startswith(("#", "^")):
                continue
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha[:short] or None
    except OSError:
        return None
    return None
