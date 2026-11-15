"""PortfolioDB — the dashboard.

Renders the "PortfolioDB" design system (deep-slate rail, tabular-mono
numerics, indigo accent, finance green/red semantics) by embedding HTML/CSS/JS
via ``st.components.v1.html`` and feeding it live Postgres data. Native
Streamlit widgets are used only for the Manage/Advisor views (see
``modern2_native.py``).

Run it with the stack (`make up`, or `docker compose up -d`), or on a host
from app/ with PORTFOLIODB_PASSWORD in the environment:

    cd app && streamlit run streamlit_app.py
"""

import contextlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import streamlit as st

import branding
import version as app_version
# db is a leaf module — it reads the environment inside load_config(), never at
# import time — so pulling the .env line parser from it here cannot defeat the
# _load_dotenv() ordering the imports below depend on.
from db import parse_env_line


def _load_dotenv() -> None:
    """Populate the process env from the repo-root .env (without overriding
    anything already set). db.py only loads PORTFOLIODB_* vars, so this makes
    other secrets — notably ANTHROPIC_API_KEY for the Advisor — available no
    matter how the dashboard is launched (bare `streamlit run` or the .ps1)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = parse_env_line(line)
        if parsed and not os.getenv(parsed[0]):
            os.environ[parsed[0]] = parsed[1]


_load_dotenv()

import modern2_native
import native_auth
import native_jobs
from dashboard import nav as dash_nav
from dashboard import payload as dash_payload
from dashboard import queries as dash_queries
from db import load_config

# ═══════════════════════════════════════════════════════════
# Page config — full-bleed so the embedded design owns the viewport
# ═══════════════════════════════════════════════════════════

st.set_page_config(page_title="PortfolioDB — Modern", page_icon="📊", layout="wide")

# Strip Streamlit chrome so the iframe reads as a standalone app.
st.markdown(
    """
    <style>
      header[data-testid="stHeader"] { display: none; }
      [data-testid="stToolbar"] { display: none; }
      [data-testid="stDecoration"] { display: none; }
      footer { display: none; }
      .block-container { padding: 0 !important; max-width: 100% !important; }
      [data-testid="stAppViewContainer"] > .main { padding: 0; }
      div[data-testid="stVerticalBlock"] { gap: 0; }
      iframe { border: none; display: block; }
      #MainMenu { visibility: hidden; }
      /* hidden control the in-iframe refresh icon clicks for a soft rerun
         (kept off-screen rather than display:none so the click still fires) */
      .st-key-bg_refresh { position: fixed; left: -9999px; top: 0; width: 1px; height: 1px; }
      /* hidden controls the in-iframe rail clicks to open a native page in
         this same session (a rerun, not a page load) */
      .st-key-pdb_nav, .st-key-pdb_news_poll { position: fixed; left: -9999px; top: 0; width: 1px; height: 1px; overflow: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ═══════════════════════════════════════════════════════════
# DB connection (pooled, shared across reruns)
# ═══════════════════════════════════════════════════════════

try:
    cfg = load_config()
except Exception as exc:  # surfaced inline rather than a blank page
    st.error(str(exc))
    st.stop()


@st.cache_resource
def get_pool():
    from psycopg2.pool import ThreadedConnectionPool

    from db import connect_params

    # connect_params carries the connect timeout (audit A-6): without it an
    # unreachable database hung the page instead of failing.
    return ThreadedConnectionPool(1, 10, **connect_params(cfg))


def get_conn():
    import psycopg2

    for _ in range(3):
        pool = get_pool()
        try:
            conn = pool.getconn()
            if conn.closed:
                raise psycopg2.OperationalError("Connection closed")
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            with contextlib.suppress(Exception):
                pool.putconn(conn, close=True)
            get_pool.clear()
    return get_pool().getconn()


def put_conn(conn):
    try:
        if not conn.closed:
            conn.rollback()
        get_pool().putconn(conn, close=conn.closed != 0)
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()


# ═══════════════════════════════════════════════════════════
# On-demand news refresh (the topbar refresh tops this up when stale)
# ═══════════════════════════════════════════════════════════

NEWS_STALE_HOURS = 4          # only fetch on refresh if news is older than this
NEWS_FETCH_THROTTLE_S = 1200  # ...and at most once per 20 min for the whole process


def _news_age_hours() -> float | None:
    """Hours since fd_news was last fetched (None if the table is empty)."""
    conn = get_conn()
    try:
        m = dash_queries.news_max_fetched_at(conn)
    finally:
        put_conn(conn)
    if m is None:
        return None
    if m.tzinfo is None:
        m = m.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - m).total_seconds() / 3600.0


def _fetch_news_now() -> tuple[bool, str]:
    """Run the daily-light Financial Datasets news fetch as a subprocess.

    Cache-first (24h TTL in the enrichment script), so this only hits the paid
    API when the local cache has expired — repeated calls within a day are free.
    """
    app_dir = Path(__file__).resolve().parent
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # the script prints emoji; avoid cp1252 crash
    # Every element of the command is fixed by this module, not by anything a
    # caller supplies: sys.executable is the interpreter already running and the
    # rest are literals. No shell (shell=False is the default), so nothing is
    # word-split or glob-expanded either. The audit rules below fire on any
    # subprocess call whose argv is not a literal string, which this cannot be —
    # hardcoding an interpreter path would break every venv and container.
    try:
        proc = subprocess.run(  # nosec B603  # nosemgrep
            [sys.executable, "fd_weekly_enrichment.py", "--profile", "daily-light"],
            cwd=str(app_dir), env=env, capture_output=True, text=True, timeout=180,
        )
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return False, str(exc)


def _news_job() -> None:
    """Background body: fetch, then drop the cached payload so the next render
    shows the new headlines. Raises on failure so the job records it."""
    ok, output = _fetch_news_now()
    build_payload.clear()
    if not ok:
        raise RuntimeError((output or "news fetch failed").strip()[-400:])


def maybe_refresh_news(session) -> str:
    """Called by the topbar refresh: fetch fresh news if it's stale.

    Returns what happened: "fresh" (nothing to do), "locked", "started",
    "running", "throttled" or "unknown" (the age could not be read).

    Gated (audit A-1): the fetch runs fd_weekly_enrichment, which writes to
    the database and spends the Financial Datasets key, so it needs an
    unlocked session like every other dashboard write. The ordinary refresh —
    clearing the payload cache and redrawing — stays open to everyone.

    Non-blocking and process-wide (audit N-10): the fetch runs on a background
    thread, and starts at most once per NEWS_FETCH_THROTTLE_S whichever
    session or tab asks. The requesting session remembers the run, and
    modern2_native.render_news_status() redraws it when the run lands.
    """
    try:
        age = _news_age_hours()
    except Exception:
        return "unknown"
    if age is not None and age < NEWS_STALE_HOURS:
        return "fresh"
    if not native_auth.is_unlocked(session):
        return "locked"
    result = native_jobs.start("news", _news_job, min_interval_s=NEWS_FETCH_THROTTLE_S)
    if result.outcome in ("started", "running"):
        modern2_native.watch_news_run(native_jobs.status("news").run_id)
    return result.outcome


# ═══════════════════════════════════════════════════════════
# Data loading — SQL lives in dashboard/queries.py, composition in
# dashboard/payload.py; these wrappers own connections + Streamlit caching.
# ═══════════════════════════════════════════════════════════


@st.cache_data(ttl=120)
def build_payload() -> dict:
    """Assemble the live PDB-shaped feed consumed by the embedded design."""
    conn = get_conn()
    try:
        return dash_payload.build_payload_data(conn, load_fundamentals)
    finally:
        put_conn(conn)


@st.cache_data(ttl=900)
def load_fundamentals(universe: tuple[str, ...]) -> dict:
    """Per-symbol FD enrichment for the Fundamentals view.

    FD data refreshes weekly, so this is cached longer than the markets feed.
    """
    conn = get_conn()
    try:
        return dash_payload.build_fundamentals(conn, universe)
    finally:
        put_conn(conn)


# ═══════════════════════════════════════════════════════════
# Embedded design (verbatim tokens from Example/css/app.css)
# ═══════════════════════════════════════════════════════════

# The dashboard front-end lives in real files under dashboard/static/
# (app.css / shell.html / app.js) so it gets syntax highlighting, linting
# and sane diffs. Loaded once per Streamlit run.
_STATIC_DIR = Path(__file__).resolve().parent / "dashboard" / "static"


def _static_text(name: str) -> str:
    return (_STATIC_DIR / name).read_text(encoding="utf-8")


DESIGN_CSS = _static_text("app.css")

# Branded first-paint skeleton (rail + shimmer cards) shown while the payload
# builds — replaces the native spinner-on-white so a cold load never flashes.
SKELETON_HTML = _static_text("skeleton.html")

# The topbar's glyphs. Like the rail's (which app.js draws from the same
# file), they come from dashboard/static/nav.json (audit R-3).
SVG = {name: dash_nav.svg(name) for name in ("logo", "menu", "search", "refresh")}


# ── HTML scaffold: rail + topbar + tape + three client-side views ──────────
DESIGN_HTML = _static_text("shell.html")

# ── Consolidated render logic (ports Example js/* to the live DATA feed) ───
APP_JS = _static_text("app.js")


def build_html(payload: dict, initial_view: str = "portfolio") -> str:
    # Escape "<" so an external news title containing "</script>" cannot break
    # out of the inline <script> block (json.dumps leaves "<" untouched).
    data_json = json.dumps({**payload, "initialView": initial_view}).replace("<", "\\u003c")
    return (
        DESIGN_HTML
        .replace("__CSS__", DESIGN_CSS)
        .replace("__SVG_LOGO__", SVG["logo"])
        .replace("__SVG_MENU__", SVG["menu"])
        .replace("__SVG_SEARCH__", SVG["search"])
        .replace("__SVG_REFRESH__", SVG["refresh"])
        .replace("__USER_NAME__", escape(branding.display_name()))
        .replace("__APP_VERSION__", escape(app_version.release_version()))
        .replace("__APP_BUILD__", escape(app_version.build_stamp()))
        .replace("__USER_INITIALS__", escape(branding.display_initials()))
        .replace("__DATA_JSON__", data_json)
        .replace("__APP_JS__", APP_JS)
    )


# ═══════════════════════════════════════════════════════════
# Render
# ═══════════════════════════════════════════════════════════

view = st.query_params.get("view", "portfolio")

# The in-iframe refresh button navigates here with a "?r=<ts>" cache-bust;
# clear the markets payload so the reload shows fresh prices, then drop the
# param. Only build_payload — the 900s fundamentals cache changes weekly and
# clearing it too would force a full FD re-read on every manual refresh.
if "r" in st.query_params:
    build_payload.clear()
    with contextlib.suppress(Exception):
        del st.query_params["r"]

# Which views are native pages and which are panes of the iframe: nav.json's
# groups, the same split the front end's rail draws.
NATIVE_VIEWS = dash_nav.ids_for("native")
FRAME_VIEWS = dash_nav.ids_for("frame")

if view in NATIVE_VIEWS:
    # reuse the cached markets payload to render the rail watchlist consistently
    try:
        _p = build_payload()
        _stocks = _p.get("stocks", {})
        rail_watchlist = [
            {"sym": s, "price": _stocks[s]["price"], "dayPct": _stocks[s]["dayPct"]}
            for s in _p.get("watchSyms", []) if s in _stocks
        ]
    except Exception:
        rail_watchlist = []
    renderers = {
        "manage": modern2_native.render_manage,
        "advisor": modern2_native.render_advisor,
        "health": modern2_native.render_health,
    }
    renderers[view](get_conn, put_conn, rail_watchlist)
else:
    # Read-only surfaces all live in the immersive iframe; `view` selects the
    # pane it opens on (portfolio / movers / alerts / fundamentals / history).
    initial_view = view if view in FRAME_VIEWS else FRAME_VIEWS[0]
    # Hidden control: the in-iframe refresh icon clicks this (via the parent DOM)
    # to trigger a soft websocket rerun with fresh data — no full page reload.
    if st.button("refresh", key="bg_refresh"):
        build_payload.clear()  # keep the 900s fundamentals cache (weekly data)
        # Top up stale news in the background (cache-first) — unlocked sessions only.
        if maybe_refresh_news(st.session_state) == "locked":
            # Fetching headlines writes to the database and spends the
            # Financial Datasets key, so it is a gated write (A-1).
            st.toast("Prices refreshed. New headlines need the dashboard password: unlock on Manage.")
    # Redraws this view when a news fetch this session asked for lands, and
    # reports a failure — without another click.
    modern2_native.render_news_status()
    # Hidden controls: the iframe rail's Manage / Advisor / Data Health entries
    # click these (via the parent DOM, like bg_refresh) so opening a native page
    # is a rerun in this session, not a page load (audit N-5). The callback sets
    # ?view=, so the address bar and a reload still agree.
    with st.container(key="pdb_nav"):
        for _native in NATIVE_VIEWS:
            st.button(_native, key=f"pdb_nav_{_native}", on_click=modern2_native._go, args=(_native,))
    # First paint of a session (cold cache / hard reload): show the branded
    # skeleton while the payload builds. Soft refreshes skip it — the previous
    # view stays on screen while data rebuilds (the iframe toasts progress).
    skeleton_slot = None
    if not st.session_state.get("_painted"):
        skeleton_slot = st.empty()
        with skeleton_slot:
            st.iframe(SKELETON_HTML, height=1180)
    try:
        payload = build_payload()
    except Exception as exc:
        if skeleton_slot is not None:
            skeleton_slot.empty()
        st.error(f"Failed to load portfolio data: {exc}")
        st.stop()
    if skeleton_slot is not None:
        skeleton_slot.empty()
    st.session_state["_painted"] = True
    # height is the no-JS fallback only: app.js resizes its own iframe to the
    # parent viewport (fitViewport) so the 100vh shell owns exactly one screen.
    st.iframe(build_html(payload, initial_view), height=1180)
