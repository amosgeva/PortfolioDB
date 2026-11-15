"""Background jobs for the brief and the news fetch (audit N-10).

The page must not block on either, and the throttle must be process-wide —
the old news throttle lived in one browser session's state, so a second tab
started the same paid fetch again at once.
"""

from __future__ import annotations

import threading
import time

import pytest

import native_jobs as jobs


@pytest.fixture(autouse=True)
def clean():
    jobs.reset()
    yield
    jobs.reset()


def test_start_returns_before_the_work_finishes():
    release = threading.Event()
    t0 = time.monotonic()
    res = jobs.start("slow", lambda: release.wait(5), min_interval_s=0)
    assert res.outcome == "started"
    assert time.monotonic() - t0 < 1.0
    assert jobs.status("slow").running
    release.set()
    assert jobs.wait("slow", 5).state == "done"


def test_only_one_run_at_a_time():
    release = threading.Event()
    calls = []

    def body():
        calls.append(1)
        release.wait(5)

    assert jobs.start("one", body, min_interval_s=0).outcome == "started"
    assert jobs.start("one", body, min_interval_s=0).outcome == "running"
    release.set()
    jobs.wait("one", 5)
    assert len(calls) == 1


def test_throttle_is_process_wide_and_reports_the_wait():
    assert jobs.start("news", lambda: None, min_interval_s=1200, now=1000.0).outcome == "started"
    jobs.wait("news", 5)
    # A different caller (another tab, another session) inside the window.
    res = jobs.start("news", lambda: None, min_interval_s=1200, now=1100.0)
    assert res.outcome == "throttled"
    assert res.retry_after == pytest.approx(1100.0)
    assert jobs.start("news", lambda: None, min_interval_s=1200, now=2201.0).outcome == "started"


def test_a_failure_is_recorded_not_raised():
    def boom():
        raise RuntimeError("vendor said no")

    jobs.start("bad", boom, min_interval_s=0)
    st = jobs.wait("bad", 5)
    assert st.state == "failed"
    assert "vendor said no" in st.error


def test_run_ids_tell_runs_apart():
    jobs.start("r", lambda: None, min_interval_s=0)
    first = jobs.wait("r", 5).run_id
    jobs.start("r", lambda: None, min_interval_s=0)
    assert jobs.wait("r", 5).run_id == first + 1
