"""The dashboard script, run end to end by Streamlit's own test harness.

Nothing else executes streamlit_app.py or modern2_native.py: the payload and
query modules are tested, but not the page that calls st.* on top of them. A
Streamlit upgrade that removed or changed an API the page uses (st.iframe,
st.query_params, the cache decorators, the forms on Manage) would pass the
whole suite and show a red traceback on :8501.

Every view reads Postgres, so this needs the real database and follows the
same gate as the other SQL modules: skip without one, fail when
PORTFOLIODB_TESTS_REQUIRE_DB is set. Read-only: no button is pressed, so
nothing is written and no news fetch is started.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from db import connect_for_tests

SCRIPT = Path(__file__).resolve().parent.parent / "streamlit_app.py"
VIEWS = ("portfolio", "manage", "health", "advisor")


@pytest.fixture(scope="module", autouse=True)
def live_db():
    connect_for_tests(skip=pytest.skip, fail=pytest.fail).close()


@pytest.fixture(autouse=True)
def restore_environ():
    # The script loads every key in the repo-root .env into os.environ (API
    # keys, the collector window). Put the process back so later tests see
    # the environment the suite pinned, not the operator's.
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _run(view: str) -> AppTest:
    at = AppTest.from_file(str(SCRIPT), default_timeout=120)
    at.query_params["view"] = view
    return at.run()


@pytest.mark.parametrize("view", VIEWS)
def test_view_renders_without_an_error(view):
    at = _run(view)
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]


def test_portfolio_view_embeds_the_live_payload():
    at = _run("portfolio")
    # st.iframe has no typed accessor in AppTest; it is reachable by type.
    # One frame, not two: the first-paint skeleton must have been replaced.
    frames = at.get("iframe")
    assert len(frames) == 1
    html = frames[0].proto.srcdoc
    for placeholder in ("__DATA_JSON__", "__APP_JS__", "__CSS__"):
        assert placeholder not in html
    assert '"initialView": "portfolio"' in html
    assert '"stocks":' in html
