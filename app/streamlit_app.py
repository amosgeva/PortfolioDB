"""PortfolioDB — the dashboard.

Renders the "PortfolioDB" design system (deep-slate rail, tabular-mono
numerics, indigo accent, finance green/red semantics) by embedding HTML/CSS/JS
via ``st.components.v1.html`` and feeding it live Postgres data. Native
Streamlit widgets are used only for the Manage/Advisor views (see
``modern2_native.py``).

Run via the repo-root launcher (loads .env, serves on 0.0.0.0:8501):

    .\\run_dashboard.ps1
"""

import contextlib
import json
import os
import subprocess
import sys
import time
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

    return ThreadedConnectionPool(
        1, 10,
        host=cfg.host, port=cfg.port, dbname=cfg.dbname,
        user=cfg.user, password=cfg.password,
    )


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
NEWS_FETCH_THROTTLE_S = 1200  # ...and at most once per 20 min per session


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


def maybe_refresh_news() -> None:
    """Called by the topbar refresh: fetch fresh news if it's stale, throttled."""
    try:
        age = _news_age_hours()
    except Exception:
        return
    if age is not None and age < NEWS_STALE_HOURS:
        return
    last = st.session_state.get("_news_fetch_at", 0.0)
    if time.time() - last < NEWS_FETCH_THROTTLE_S:
        return
    st.session_state["_news_fetch_at"] = time.time()
    with st.spinner("Fetching latest news…"):
        _fetch_news_now()


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

# SVG glyphs reused across the shell
SVG = {
    "logo": '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l5-6 4 3 5-7"/><path d="M17 7h3v3"/></svg>',
    "grid": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/></svg>',
    "trend": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg>',
    "bell": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>',
    "menu": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M3 12h18M3 18h18"/></svg>',
    "search": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
    "refresh": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>',
    "book": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
    "clock": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l4 2"/></svg>',
    "sliders": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>',
    "stats": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><rect x="7" y="12" width="3" height="6" rx="1"/><rect x="12" y="8" width="3" height="10" rx="1"/><rect x="17" y="4" width="3" height="14" rx="1"/></svg>',
    "pulse": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
    "spark": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 4.6L18.5 9l-4.6 1.9L12 15l-1.9-4.1L5.5 9l4.6-1.4z"/><path d="M19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8z"/></svg>',
}


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
        .replace("__SVG_GRID__", SVG["grid"])
        .replace("__SVG_TREND__", SVG["trend"])
        .replace("__SVG_BELL__", SVG["bell"])
        .replace("__SVG_MENU__", SVG["menu"])
        .replace("__SVG_SEARCH__", SVG["search"])
        .replace("__SVG_REFRESH__", SVG["refresh"])
        .replace("__SVG_BOOK__", SVG["book"])
        .replace("__SVG_CLOCK__", SVG["clock"])
        .replace("__SVG_SLIDERS__", SVG["sliders"])
        .replace("__SVG_SPARK__", SVG["spark"])
        .replace("__SVG_PULSE__", SVG["pulse"])
        .replace("__SVG_STATS__", SVG["stats"])
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

if view in ("manage", "advisor", "health"):
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
    if view == "manage":
        modern2_native.render_manage(get_conn, put_conn, rail_watchlist)
    elif view == "health":
        modern2_native.render_health(get_conn, put_conn, rail_watchlist)
    else:
        modern2_native.render_advisor(get_conn, put_conn, rail_watchlist)
else:
    # Read-only surfaces all live in the immersive iframe; `view` selects the
    # pane it opens on (portfolio / movers / alerts / fundamentals / history).
    iframe_views = {"portfolio", "movers", "stats", "alerts", "fundamentals", "history"}
    initial_view = view if view in iframe_views else "portfolio"
    # Hidden control: the in-iframe refresh icon clicks this (via the parent DOM)
    # to trigger a soft websocket rerun with fresh data — no full page reload.
    if st.button("refresh", key="bg_refresh"):
        build_payload.clear()  # keep the 900s fundamentals cache (weekly data)
        maybe_refresh_news()  # top up stale news from Financial Datasets (cache-first)
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
