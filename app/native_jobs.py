"""Slow, paid work off the page's thread: the advisor brief and the news fetch.

Both used to run inside the Streamlit script (audit N-10): the brief behind a
spinner, the news fetch as a subprocess with a 180-second timeout, so the
session that asked sat frozen until the call returned. And the news throttle
lived in that browser session's state, so a second tab — or a reload — could
start the same paid fetch again at once.

A job here runs on a daemon thread, at most one of each name at a time, and
the throttle is process-wide: whoever asks, a job of a given name starts at
most once per ``min_interval_s``. The page polls :func:`status` and shows it.

Job bodies must not call ``st.*`` — they run outside any script context.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class JobStatus:
    name: str
    state: str = "idle"          # idle | running | done | failed
    started_at: float = 0.0      # time.time() of the last start
    finished_at: float = 0.0
    error: str = ""
    run_id: int = 0              # increments per start, so a page can tell runs apart

    @property
    def running(self) -> bool:
        return self.state == "running"


@dataclass(frozen=True)
class StartResult:
    outcome: str                 # started | running | throttled
    retry_after: float = 0.0


_lock = threading.Lock()
_jobs: dict[str, JobStatus] = {}
_last_start: dict[str, float] = {}


def status(name: str) -> JobStatus:
    with _lock:
        return _jobs.get(name, JobStatus(name))


def start(name: str, fn: Callable[[], object], *, min_interval_s: float,
          now: float | None = None) -> StartResult:
    """Start ``fn`` on a background thread unless one is running or the
    process-wide throttle says it ran too recently."""
    now = time.time() if now is None else now
    with _lock:
        current = _jobs.get(name, JobStatus(name))
        if current.running:
            return StartResult("running")
        last = _last_start.get(name)
        if last is not None and now - last < min_interval_s:
            return StartResult("throttled", min_interval_s - (now - last))
        _last_start[name] = now
        run_id = current.run_id + 1
        _jobs[name] = JobStatus(name, "running", started_at=now, run_id=run_id)

    def _body() -> None:
        try:
            fn()
        except Exception as exc:  # surfaced on the page, never re-raised
            _finish(name, run_id, "failed", f"{type(exc).__name__}: {exc}")
        else:
            _finish(name, run_id, "done", "")

    threading.Thread(target=_body, name=f"pdb-job-{name}", daemon=True).start()
    return StartResult("started")


def _finish(name: str, run_id: int, state: str, error: str) -> None:
    with _lock:
        current = _jobs.get(name)
        if current is not None and current.run_id == run_id:
            _jobs[name] = replace(current, state=state, finished_at=time.time(), error=error)


def wait(name: str, timeout: float = 30.0) -> JobStatus:
    """Block until the named job is not running (tests and the CLI only)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = status(name)
        if not st.running:
            return st
        time.sleep(0.05)
    return status(name)


def reset() -> None:
    """Forget every job and throttle (tests only)."""
    with _lock:
        _jobs.clear()
        _last_start.clear()
