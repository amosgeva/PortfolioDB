"""The password gate in front of every dashboard write.

The read-only views (Portfolio … History, Data Health) stay open. Everything
that changes the ledger, the settings or the one-pager, and everything that
spends money on an LLM call, needs ``PORTFOLIODB_DASHBOARD_PASSWORD`` first
(audit A-1). Until 1.8.0 anyone who could reach :8501 could delete lots,
repoint the advisor and run up a model bill.

The rules, all enforced here so the pages cannot drift apart:

* **Unset means read-only.** No password configured is not "no gate" — the
  write pages still render, with their forms disabled and a note saying how
  to set one. That is the secure default on upgrade: an existing install that
  never had the variable does not silently stay writable.
* **Session-scoped unlock.** A correct password unlocks the one browser
  session that typed it. The session holds a fingerprint of the password,
  never the password, so a password changed in ``.env`` relocks every session
  on the next restart, and an idle session relocks after ``IDLE_TIMEOUT_S``.
* **Constant-time compare, on bytes.** ``hmac.compare_digest`` on ``str``
  raises for non-ASCII input (the MCP had exactly that bug, M-8); both sides
  are UTF-8 encoded first.
* **Throttled.** Failed attempts are counted per client and process-wide, in
  this process's memory rather than in the browser session, so opening a new
  tab does not reset the count. After ``FREE_ATTEMPTS`` failures a client
  waits ``LOCKOUT_BASE_S`` seconds, doubling per further failure up to
  ``LOCKOUT_MAX_S``. While locked out the password is not even compared.
* **Never echoed or logged.** The input is a password field inside a form
  that clears on submit; the value is dropped from session state as soon as
  it has been checked, and no message ever includes it.

Every write path calls :func:`require_unlocked` immediately before it writes,
so a disabled button is a courtesy, not the control.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections.abc import MutableMapping
from dataclasses import dataclass

# The variable's name, not a credential (same marker as mcp/auth.py's env names).
# Spelled _PW_ like mcp/deps.py's RO_PW_ENV: Prospector's dodgy reads any
# `*PASSWORD* = "..."` assignment as a hardcoded password and ignores nosec.
DASHBOARD_PW_ENV = "PORTFOLIODB_DASHBOARD_PASSWORD"  # nosec B105  # nosemgrep

FREE_ATTEMPTS = 5          # per client, before the first lockout
GLOBAL_FREE_ATTEMPTS = 25  # across all clients, before everyone waits
LOCKOUT_BASE_S = 30.0
LOCKOUT_MAX_S = 900.0
IDLE_TIMEOUT_S = 30 * 60.0

# Session-state keys. The leading underscore keeps them out of any widget's
# namespace; nothing here is ever the password itself.
_UNLOCK_KEY = "_pdb_write_unlock"
_SEEN_KEY = "_pdb_write_seen"

# Per-process secret for the session fingerprint. A fingerprint read out of a
# session (there is no way to, but suppose) is useless in another process and
# says nothing about the password.
_PROCESS_KEY = secrets.token_bytes(32)


class WriteLocked(RuntimeError):
    """Raised by require_unlocked when a write is attempted without unlock."""


def _expected() -> str:
    return (os.environ.get(DASHBOARD_PW_ENV) or "").strip()


def configured() -> bool:
    """Whether a dashboard password is set at all."""
    return bool(_expected())


def _fingerprint(password: str) -> str:
    return hmac.new(_PROCESS_KEY, password.encode("utf-8"), hashlib.sha256).hexdigest()


def _matches(candidate: str, expected: str) -> bool:
    # Bytes on both sides: compare_digest on str raises TypeError for any
    # non-ASCII character, which would turn a typo into a crash.
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


# ── throttling ──────────────────────────────────────────────────────────────


@dataclass
class _Slot:
    failures: int = 0
    locked_until: float = 0.0


_slots: dict[str, _Slot] = {}
_slots_lock = threading.Lock()
_GLOBAL = "*"


def _lockout_for(failures: int, free: int) -> float:
    if failures < free:
        return 0.0
    return min(LOCKOUT_BASE_S * (2 ** (failures - free)), LOCKOUT_MAX_S)


def retry_after(client: str, now: float | None = None) -> float:
    """Seconds this client must wait before another attempt (0 = may try)."""
    now = time.monotonic() if now is None else now
    with _slots_lock:
        waits = [
            s.locked_until - now
            for key in (client, _GLOBAL)
            if (s := _slots.get(key)) is not None
        ]
    return max([0.0, *waits])


def _record_failure(client: str, now: float) -> None:
    with _slots_lock:
        for key, free in ((client, FREE_ATTEMPTS), (_GLOBAL, GLOBAL_FREE_ATTEMPTS)):
            slot = _slots.setdefault(key, _Slot())
            slot.failures += 1
            wait = _lockout_for(slot.failures, free)
            if wait:
                slot.locked_until = now + wait


def _record_success(client: str) -> None:
    with _slots_lock:
        _slots.pop(client, None)


def reset_throttle() -> None:
    """Forget every failed attempt (tests; a process restart does the same)."""
    with _slots_lock:
        _slots.clear()


# ── session state ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Attempt:
    ok: bool
    message: str


def attempt(
    candidate: str,
    session: MutableMapping,
    client: str = "unknown",
    now: float | None = None,
) -> Attempt:
    """Check a typed password and, when it is right, unlock ``session``.

    The returned message is safe to show: it never contains the candidate.
    """
    now = time.monotonic() if now is None else now
    expected = _expected()
    if not expected:
        return Attempt(False, f"No dashboard password is configured. Set {DASHBOARD_PW_ENV} in .env.")
    wait = retry_after(client, now)
    if wait > 0:
        return Attempt(False, f"Too many failed attempts. Try again in {int(wait) + 1} s.")
    if not _matches(candidate or "", expected):
        _record_failure(client, now)
        wait = retry_after(client, now)
        if wait > 0:
            return Attempt(False, f"Wrong password. Too many failed attempts: wait {int(wait) + 1} s.")
        return Attempt(False, "Wrong password.")
    _record_success(client)
    session[_UNLOCK_KEY] = _fingerprint(expected)
    session[_SEEN_KEY] = now
    return Attempt(True, "Unlocked for this browser session.")


def is_unlocked(session: MutableMapping, now: float | None = None) -> bool:
    """Whether this session may write right now. Refreshes the idle timer."""
    now = time.monotonic() if now is None else now
    expected = _expected()
    held = session.get(_UNLOCK_KEY)
    if not expected or not held:
        return False
    if not hmac.compare_digest(str(held).encode(), _fingerprint(expected).encode()):
        lock(session)  # the password changed under this session
        return False
    seen = session.get(_SEEN_KEY) or 0.0
    if now - seen > IDLE_TIMEOUT_S:
        lock(session)
        return False
    session[_SEEN_KEY] = now
    return True


def lock(session: MutableMapping) -> None:
    session.pop(_UNLOCK_KEY, None)
    session.pop(_SEEN_KEY, None)


def require_unlocked(session: MutableMapping) -> None:
    """The control every write path calls immediately before writing."""
    if not is_unlocked(session):
        raise WriteLocked(
            "This change needs the dashboard password: unlock at the top of the page."
            if configured()
            else f"Writes are disabled: {DASHBOARD_PW_ENV} is not set."
        )
