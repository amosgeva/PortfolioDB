"""Native (server-interactive) pages for the modern v2 dashboard.

The immersive design lives in a sandboxed ``components.html`` iframe, which is
great for read-only surfaces but cannot write to the DB or call Claude. The
**Manage** (DB writes) and **Advisor** (Claude briefs + chat) surfaces are
therefore rendered with native Streamlit widgets, themed with the same design
tokens (deep-slate rail, indigo accent, Space Grotesk / JetBrains Mono) so they
feel like the same product.

Navigation is unified through the URL ``?view=`` query param: the rail rendered
here links back to the iframe views (``?view=portfolio`` …) and between the two
native views (``?view=manage`` / ``?view=advisor``).
"""

from __future__ import annotations

import html
import os
import re
from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import advisor
import branding
import llm
import market_overview
import market_window
import reporting_tz
import settings
from db import execute, fetch_all

# Tickers only ever contain these characters; reject anything else at the input
# boundary so no HTML can be stored in instruments.symbol (defense vs stored XSS).
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")

# ── rail glyphs (shared visual language with the iframe) ───────────────────
_ICONS = {
    "logo": '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l5-6 4 3 5-7"/><path d="M17 7h3v3"/></svg>',
    "portfolio": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/></svg>',
    "movers": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg>',
    "alerts": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>',
    "fundamentals": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
    "history": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l4 2"/></svg>',
    "manage": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>',
    "advisor": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 4.6L18.5 9l-4.6 1.9L12 15l-1.9-4.1L5.5 9l4.6-1.4z"/><path d="M19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8z"/></svg>',
}

_ICONS["stats"] = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><rect x="7" y="12" width="3" height="6" rx="1"/><rect x="12" y="8" width="3" height="10" rx="1"/><rect x="17" y="4" width="3" height="14" rx="1"/></svg>'
)

_ICONS["health"] = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>'
)

_NAV = [
    ("portfolio", "Portfolio"),
    ("movers", "Market Movers"),
    ("stats", "Statistics"),
    ("alerts", "Alerts & News"),
    ("fundamentals", "Fundamentals"),
    ("history", "History"),
    ("manage", "Manage"),
    ("advisor", "Advisor"),
    ("health", "Data Health"),
]

# ── themed CSS for native pages ────────────────────────────────────────────
NATIVE_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
:root{
  --bg:oklch(98% 0.005 255);--surface:#fff;--surface-2:oklch(97.5% 0.006 255);--surface-3:oklch(95.5% 0.008 255);
  --fg:oklch(24% 0.022 260);--fg-soft:oklch(38% 0.02 260);--muted:oklch(46% 0.018 260);--faint:oklch(53.5% 0.012 260);
  --border:oklch(91% 0.008 260);--border-2:oklch(86% 0.01 260);
  --rail:oklch(26% 0.035 264);--rail-fg:oklch(92% 0.01 264);--rail-muted:oklch(68% 0.02 264);
  --accent:oklch(55% 0.19 264);--accent-soft:oklch(95% 0.04 264);
  --up:oklch(51% 0.16 152);--down:oklch(56% 0.20 26);--warn:oklch(54% 0.15 75);
  --r:11px;--shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.04);
  --font:'Space Grotesk',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  --mono:'JetBrains Mono',ui-monospace,'SFMono-Regular',Menlo,monospace;--rail-w:248px;
  color-scheme:light;
}
/* follow the dashboard's saved theme (pdb_theme) — same tokens as the iframe */
html[data-theme="dark"]{
  --bg:oklch(16.5% 0.015 260);--surface:oklch(20.5% 0.018 260);--surface-2:oklch(24% 0.02 260);--surface-3:oklch(28% 0.022 260);
  --fg:oklch(93% 0.008 260);--fg-soft:oklch(82% 0.012 260);--muted:oklch(74% 0.015 260);--faint:oklch(66.5% 0.012 260);
  --border:oklch(29% 0.015 260);--border-2:oklch(35% 0.018 260);
  --rail:oklch(18.5% 0.028 264);--rail-fg:oklch(92% 0.01 264);--rail-muted:oklch(66% 0.018 264);
  --accent:oklch(62% 0.18 264);--accent-soft:oklch(29% 0.06 264);
  --up:oklch(70% 0.14 152);--down:oklch(68% 0.18 26);--warn:oklch(76% 0.13 75);
  --shadow:0 1px 2px rgba(0,0,0,.25),0 1px 3px rgba(0,0,0,.2);
  color-scheme:dark;
}
html[data-theme="dark"] .stApp,html[data-theme="dark"] body{color:var(--fg)}
.stTextInput input,.stNumberInput input,.stDateInput input{color:var(--fg)!important}
/* hide Streamlit's own sidebar/header; we draw our own rail */
section[data-testid="stSidebar"]{display:none!important}
header[data-testid="stHeader"]{display:none}
#MainMenu, footer{display:none}
html,body,[data-testid="stAppViewContainer"]{background:var(--bg)!important;font-family:var(--font)}
.stApp{background:var(--bg)}
/* shift main content right of the fixed rail */
.block-container,[data-testid="stMainBlockContainer"]{
  margin-left:var(--rail-w)!important;max-width:1180px!important;
  padding:0 32px 64px 32px!important;
}
@media (max-width:980px){ .block-container,[data-testid="stMainBlockContainer"]{margin-left:0!important;padding:0 16px 48px!important} .m2-rail{display:none!important} }

/* fixed rail */
.m2-rail{position:fixed;left:0;top:0;width:var(--rail-w);height:100vh;overflow-y:auto;background:var(--rail);color:var(--rail-fg);
  display:flex;flex-direction:column;border-right:1px solid oklch(20% 0.03 264);z-index:100}
.m2-rail .brand{display:flex;align-items:center;gap:11px;padding:20px 20px 18px}
.m2-rail .logo{width:34px;height:34px;border-radius:9px;background:linear-gradient(140deg,var(--accent),oklch(62% 0.2 290));display:grid;place-items:center;flex:0 0 auto;box-shadow:0 4px 12px -4px oklch(55% 0.19 264 / .7)}
.m2-rail .logo svg{width:18px;height:18px}
.m2-rail .name{font-weight:700;font-size:16px;letter-spacing:-.02em}
.m2-rail .name b{color:oklch(78% 0.12 290)}
.m2-rail .tag{font-size:10.5px;color:var(--rail-muted);letter-spacing:.14em;text-transform:uppercase}
.m2-rail .label{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--rail-muted);padding:14px 24px 7px}
.m2-rail nav{display:flex;flex-direction:column;gap:2px;padding:0 12px}
.m2-rail nav a{display:flex;align-items:center;gap:11px;padding:9px 12px;border-radius:9px;color:var(--rail-muted);font-weight:500;font-size:13.5px;text-decoration:none;transition:background .14s,color .14s}
.m2-rail nav a svg{width:17px;height:17px;opacity:.9;flex:0 0 auto}
.m2-rail nav a:hover{background:oklch(33% 0.04 264);color:var(--rail-fg)}
.m2-rail nav a.active{background:oklch(34% 0.055 264);color:#fff;box-shadow:inset 2px 0 0 var(--accent)}
/* rail watchlist (mirrors the iframe rail) */
.m2-rail .wl{display:flex;flex-direction:column;gap:1px;padding:0 8px}
.m2-rail .wl__row{display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:10px;padding:7px 10px;border-radius:8px}
.m2-rail .wl__row:hover{background:oklch(31% 0.04 264)}
.m2-rail .wl__sym{font-weight:600;font-size:12.5px}
.m2-rail .wl__px{font-family:var(--mono);font-size:12px;color:var(--rail-fg)}
.m2-rail .wl__chg{font-family:var(--mono);font-size:11px;min-width:52px;text-align:right}
.m2-rail .wl__chg.up{color:var(--up)}.m2-rail .wl__chg.down{color:var(--down)}
/* keep the chat input bar clear of the fixed rail */
[data-testid="stBottom"]{margin-left:var(--rail-w)!important;width:calc(100% - var(--rail-w))!important}
@media (max-width:980px){ [data-testid="stBottom"]{margin-left:0!important;width:100%!important} }
.m2-rail .foot{margin-top:auto;padding:14px 16px;display:flex;align-items:center;gap:11px;border-top:1px solid oklch(32% 0.03 264)}
.m2-rail .avatar{width:32px;height:32px;border-radius:50%;background:linear-gradient(140deg,oklch(62% 0.13 200),oklch(55% 0.19 264));display:grid;place-items:center;font-weight:700;font-size:12px;color:#fff;flex:0 0 auto}
.m2-rail .user b{font-size:13px;display:block}.m2-rail .user span{font-size:11px;color:var(--rail-muted)}

/* topbar */
.m2-topbar{padding:18px 0 14px;border-bottom:1px solid var(--border);margin-bottom:22px}
.m2-topbar h1{font-size:22px;font-weight:700;letter-spacing:-.02em;color:var(--fg);margin:0}
.m2-topbar p{font-size:13px;color:var(--muted);margin:2px 0 0}

/* headings + text */
.block-container h2,.block-container h3{font-family:var(--font);color:var(--fg);letter-spacing:-.01em}
.block-container h3{font-size:16px;font-weight:600;margin-top:6px}
/* buttons: keep Streamlit's themed colors (primary = indigo via theme env),
   only align typography + radius so we don't override the primary fill */
.stButton>button,[data-testid="stFormSubmitButton"]>button{font-family:var(--font);font-weight:600;border-radius:9px}
/* inputs */
.stTextInput input,.stNumberInput input,.stDateInput input,[data-baseweb="select"]>div{
  font-family:var(--font);border-radius:9px!important;border-color:var(--border-2)!important;background:var(--surface)!important}
.stTextInput input:focus,.stNumberInput input:focus{border-color:var(--accent)!important;box-shadow:0 0 0 3px var(--accent-soft)!important}
/* widget labels: Streamlit dims these — restore readable contrast */
[data-testid="stWidgetLabel"] p,[data-testid="stWidgetLabel"] label,.stCheckbox label p{
  color:var(--fg-soft)!important;font-weight:600!important;font-size:12.5px!important;opacity:1!important}
.stMarkdown p{color:var(--fg-soft)}
/* number-input steppers: tone down Streamlit's heavy dark buttons */
.stNumberInput button{background:var(--surface-2)!important;color:var(--fg-soft)!important;border-color:var(--border-2)!important}
.stNumberInput button:hover{color:var(--accent)!important;border-color:var(--accent)!important}
/* checkbox accent to indigo */
.stCheckbox [data-baseweb="checkbox"] span[aria-checked="true"],[data-testid="stCheckbox"] [aria-checked="true"]{
  background-color:var(--accent)!important;border-color:var(--accent)!important}
/* form container as a card */
[data-testid="stForm"]{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);box-shadow:var(--shadow);padding:18px}
/* dataframes + chat */
[data-testid="stDataFrame"]{border-radius:var(--r);overflow:hidden;border:1px solid var(--border)}
[data-testid="stChatMessage"]{background:var(--surface);border:1px solid var(--border);border-radius:var(--r)}
hr{border-color:var(--border)}
/* markdown body text (lists, captions) */
.stMarkdown li{color:var(--fg-soft)}
[data-testid="stCaptionContainer"] p,.stMarkdown small{color:var(--muted)!important}
/* inline code chips */
.stMarkdown code,[data-testid="stMarkdownContainer"] code{
  background:var(--surface-3)!important;color:var(--fg-soft)!important;font-family:var(--mono);
  border-radius:5px;padding:1px 5px}
/* secondary buttons: Streamlit leaves these white in dark mode */
.stButton>button[kind="secondary"],[data-testid="stBaseButton-secondary"]{
  background:var(--surface)!important;color:var(--fg)!important;border-color:var(--border-2)!important}
.stButton>button[kind="secondary"]:hover,[data-testid="stBaseButton-secondary"]:hover{
  border-color:var(--accent)!important;color:var(--accent)!important}
/* expanders */
[data-testid="stExpander"] details,details[data-testid="stExpander"]{
  background:var(--surface)!important;border:1px solid var(--border)!important;border-radius:var(--r)}
[data-testid="stExpander"] summary{color:var(--fg-soft)!important;font-weight:600}
[data-testid="stExpander"] summary:hover{color:var(--accent)!important}
[data-testid="stExpander"] summary svg{fill:var(--fg-soft)}
/* chat input bar: keep the bottom strip + textarea on theme (Streamlit nests
   several wrappers here, each with its own background) */
[data-testid="stBottom"],[data-testid="stBottom"]>div,
[data-testid="stBottomBlockContainer"]{background:var(--bg)!important}
[data-testid="stChatInput"]{background:transparent!important}
/* the visible input surface is stChatInput's direct child div (emotion-styled
   with the theme's secondaryBg); the textarea inherits its color from it */
[data-testid="stChatInput"]>div{
  background:var(--surface)!important;border:1px solid var(--border-2)!important;
  border-radius:var(--r)!important;color:var(--fg)!important}
[data-testid="stChatInput"] textarea{background:transparent!important;color:var(--fg)!important;caret-color:var(--fg)}
[data-testid="stChatInput"] textarea::placeholder{color:var(--muted)!important}
#stChatInputInstructions,[data-testid="stChatInput"] .stChatInputInstructions{color:var(--muted)!important}
[data-testid="stChatInputSubmitButton"]{background:transparent!important;color:var(--muted)!important}
/* alerts (info/success/warning/error): re-tint for dark so they don't glare */
[data-testid="stAlert"]{border-radius:var(--r)}
html[data-theme="dark"] [data-testid="stAlert"]{background:var(--surface-2)!important;color:var(--fg)!important}
html[data-theme="dark"] [data-testid="stAlert"]:has([data-testid="stAlertContentInfo"]){background:oklch(28% 0.05 250 / .6)!important}
html[data-theme="dark"] [data-testid="stAlert"]:has([data-testid="stAlertContentSuccess"]){background:oklch(28% 0.06 152 / .6)!important}
html[data-theme="dark"] [data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]){background:oklch(30% 0.06 80 / .6)!important}
html[data-theme="dark"] [data-testid="stAlert"]:has([data-testid="stAlertContentError"]){background:oklch(28% 0.07 26 / .6)!important}
html[data-theme="dark"] [data-testid="stAlert"] p{color:var(--fg)!important}
/* select dropdown popovers (rendered in a body-level portal) */
[data-baseweb="popover"] [data-baseweb="menu"],ul[data-baseweb="menu"]{background:var(--surface)!important}
ul[data-baseweb="menu"] li{color:var(--fg)!important}
ul[data-baseweb="menu"] li:hover,ul[data-baseweb="menu"] li[aria-selected="true"]{background:var(--surface-2)!important}
/* spinner label */
[data-testid="stSpinner"] p{color:var(--muted)!important}
/* theme-shim iframe: kept off-screen rather than display:none so its script runs */
.st-key-m2_theme_shim{position:fixed;left:-9999px;top:0;width:1px;height:1px;overflow:hidden}
"""


def _watchlist_html(watchlist) -> str:
    if not watchlist:
        return ""
    rows = []
    for w in watchlist:
        up = (w.get("dayPct") or 0) >= 0
        rows.append(
            '<div class="wl__row"><span class="wl__sym">' + html.escape(str(w.get("sym", ""))) + '</span>'
            '<span class="wl__px">$' + f"{float(w.get('price') or 0):,.2f}" + '</span>'
            '<span class="wl__chg ' + ("up" if up else "down") + '">'
            + f"{float(w.get('dayPct') or 0):+.2f}%" + '</span></div>'
        )
    return '<div class="label">Watchlist</div><div class="wl">' + "".join(rows) + '</div>'


def _rail_html(active: str, watchlist=None) -> str:
    workspace = {"portfolio", "movers", "stats", "alerts", "fundamentals", "history"}
    account = {"manage", "advisor", "health"}

    def link(key, label):
        cls = "active" if key == active else ""
        return (f'<a class="{cls}" href="?view={key}" target="_self">'
                f'{_ICONS.get(key, "")}{label}</a>')

    nav_ws = "".join(link(k, lbl) for k, lbl in _NAV if k in workspace)
    nav_mg = "".join(link(k, lbl) for k, lbl in _NAV if k in account)
    return (
        '<aside class="m2-rail">'
        '<div class="brand"><span class="logo">' + _ICONS["logo"] + '</span>'
        '<span><div class="name">Portfolio<b>DB</b></div><div class="tag">Live markets</div></span></div>'
        '<div class="label">Workspace</div><nav>' + nav_ws + '</nav>'
        '<div class="label">Account</div><nav>' + nav_mg + '</nav>'
        + _watchlist_html(watchlist) +
        '<div class="foot"><span class="avatar">' + html.escape(branding.display_initials()) + '</span>'
        '<span class="user"><b>' + html.escape(branding.display_name()) + '</b><span>Growth portfolio</span></span></div>'
        '</aside>'
    )


def inject_shell(active: str, title: str, subtitle: str, watchlist=None) -> None:
    st.markdown(f"<style>{NATIVE_CSS}</style>", unsafe_allow_html=True)
    # Apply the dashboard's saved light/dark theme to this native page. Scripts
    # in st.markdown are stripped, so a tiny same-origin iframe sets data-theme
    # on the parent document instead. st.iframe (the components.html successor)
    # rejects height=0, so the keyed container parks it off-screen via CSS.
    with st.container(key="m2_theme_shim"):
        st.iframe(
            """<script>
            try {
              var t = localStorage.getItem('pdb_theme') ||
                (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
              window.parent.document.documentElement.setAttribute('data-theme', t);
            } catch (e) {}
            </script>""",
            height=1,
        )
    st.markdown(_rail_html(active, watchlist), unsafe_allow_html=True)
    st.markdown(
        f'<div class="m2-topbar"><h1>{title}</h1><p>{subtitle}</p></div>',
        unsafe_allow_html=True,
    )


# ── write helpers (only the native surface mutates the DB) ─────────────────


def _add_lot(get_conn, put_conn, symbol, account, side, trade_date, qty, price, fees, notes):
    conn = get_conn()
    try:
        execute(conn, "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING", (symbol,))
        execute(
            conn,
            """
            INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees, notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """,
            (symbol, account or None, side, trade_date, qty, price, fees, notes),
        )
    finally:
        put_conn(conn)


def _delete_lot(get_conn, put_conn, lot_id: int):
    conn = get_conn()
    try:
        execute(conn, "DELETE FROM lots WHERE id = %s", (lot_id,))
    finally:
        put_conn(conn)


def _set_watchlist(get_conn, put_conn, symbol: str, on: bool):
    conn = get_conn()
    try:
        execute(conn, "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING", (symbol,))
        execute(conn, "UPDATE instruments SET watchlist=%s, updated_at=now() WHERE symbol=%s", (on, symbol))
    finally:
        put_conn(conn)


# ── Manage page ────────────────────────────────────────────────────────────


def render_manage(get_conn, put_conn, watchlist=None) -> None:
    inject_shell("manage", "Manage", "Record trades, cash, and watchlist — writes straight to the ledger", watchlist)

    conn = get_conn()
    try:
        acct_rows = fetch_all(conn, "SELECT DISTINCT COALESCE(account,'(none)') AS account FROM lots ORDER BY account")
        form_rows = fetch_all(
            conn,
            """
            SELECT DISTINCT account FROM lots WHERE account IS NOT NULL
            UNION SELECT DISTINCT account FROM cash_snapshots ORDER BY 1
            """,
        )
    finally:
        put_conn(conn)
    accounts = [r["account"] for r in acct_rows] or ["(none)"]
    form_accounts = [r["account"] for r in form_rows] or ["(merged)"]

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("➕ Add Lot")
        # symbol/account/side/qty are keyed so the Recent Lots "Sell" button can
        # prefill them; their defaults live in session state (a value=/index=
        # param here would conflict with the prefill).
        st.session_state.setdefault("m2_lot_qty", 1.0)
        with st.form("m2_add_lot"):
            symbol = st.text_input("Symbol", key="m2_lot_sym").strip().upper()
            account_options = ["(none)"] + [a for a in accounts if a != "(none)"]
            account = st.selectbox("Account", options=account_options, key="m2_lot_acct")
            new_account = st.text_input("…or new account (overrides selection)", value="", key="m2_lot_new_acct").strip()
            side = st.selectbox("Side", options=["BUY", "SELL"], key="m2_lot_side")
            tdate = st.date_input("Trade date", value=date.today())
            qty = st.number_input("Quantity", min_value=0.000001, step=1.0, format="%.6f", key="m2_lot_qty")
            price = st.number_input("Price", min_value=0.0, value=0.0, step=0.01, format="%.4f")
            fees = st.number_input("Fees", min_value=0.0, value=0.0, step=0.01, format="%.4f")
            notes = st.text_input("Notes", value="")
            if st.form_submit_button("Add lot", type="primary"):
                if not symbol:
                    st.error("Symbol is required")
                elif not _SYMBOL_RE.match(symbol):
                    st.error("Symbol may only contain A–Z, 0–9, '.' and '-'")
                elif price <= 0:
                    st.error("Price must be > 0")
                else:
                    chosen = new_account if new_account else (None if account == "(none)" else account)
                    _add_lot(get_conn, put_conn, symbol, chosen, side, tdate, float(qty), float(price), float(fees), notes or None)
                    st.success(f"✅ Added {side} lot: {qty:.4f} × {symbol} @ ${price:.2f}")
                    st.cache_data.clear()
                    st.rerun()

    with col2:
        st.subheader("⭐ Watchlist")
        with st.form("m2_watchlist"):
            w_symbol = st.text_input("Symbol", value="", key="m2_wl_sym").strip().upper()
            w_on = st.checkbox("Track even if sold (watchlist)", value=True)
            if st.form_submit_button("Add to watchlist", type="primary"):
                if not w_symbol:
                    st.error("Symbol is required")
                elif not _SYMBOL_RE.match(w_symbol):
                    st.error("Symbol may only contain A–Z, 0–9, '.' and '-'")
                else:
                    _set_watchlist(get_conn, put_conn, w_symbol, w_on)
                    st.success(f"⭐ Watchlist: {w_symbol} = {'ON' if w_on else 'OFF'}")
                    st.cache_data.clear()
                    st.rerun()

        # current watchlist with per-symbol remove
        conn = get_conn()
        try:
            wl_syms = [r["symbol"] for r in fetch_all(
                conn, "SELECT symbol FROM instruments WHERE watchlist = TRUE ORDER BY symbol")]
        finally:
            put_conn(conn)

        st.markdown("**Current watchlist**")
        if not wl_syms:
            st.caption("No symbols on the watchlist yet.")
        else:
            for sym in wl_syms:
                rc1, rc2 = st.columns([3, 1])
                rc1.markdown(
                    f"<div style='font-family:var(--mono);font-weight:600;padding-top:8px'>{html.escape(sym)}</div>",
                    unsafe_allow_html=True,
                )
                if rc2.button("✕ Remove", key=f"m2_wl_rm_{sym}"):
                    _set_watchlist(get_conn, put_conn, sym, False)
                    st.success(f"Removed {sym} from watchlist")
                    st.cache_data.clear()
                    st.rerun()

    st.divider()
    st.subheader("💰 Update Cash Balance")
    with st.form("m2_cash"):
        cf1, cf2 = st.columns(2)
        cash_account = cf1.selectbox("Account", options=form_accounts, key="m2_cash_acct")
        cash_amount = cf2.number_input("Cash balance ($)", min_value=0.0, value=0.0, step=0.01, format="%.2f")
        new_cash_account = st.text_input("…or new account (overrides selection)", value="", key="m2_cash_new").strip()
        cash_note = st.text_input("Note (optional)", value="", key="m2_cash_note")
        if st.form_submit_button("💾 Save cash balance", type="primary"):
            chosen = new_cash_account or cash_account
            conn = get_conn()
            try:
                execute(conn, "INSERT INTO cash_snapshots(account, cash, note) VALUES (%s,%s,%s)",
                        (chosen, cash_amount, cash_note or None))
            finally:
                put_conn(conn)
            st.success(f"Cash updated: {chosen} = ${cash_amount:,.2f}")
            st.cache_data.clear()
            st.rerun()

    conn = get_conn()
    try:
        recent_cash = fetch_all(conn, "SELECT account, cash, ts, note FROM cash_snapshots ORDER BY ts DESC LIMIT 6")
    finally:
        put_conn(conn)
    if recent_cash:
        st.caption("Recent cash entries:")
        st.dataframe(pd.DataFrame(recent_cash), width="stretch", hide_index=True)

    st.divider()
    st.subheader("📒 Recent Lots")
    st.caption("Latest 30 trades. **Sell** prefills the Add Lot form; **Delete** asks for confirmation below.")

    conn = get_conn()
    try:
        recent_lots = fetch_all(
            conn,
            """
            SELECT id, symbol, account, side, trade_date, quantity, price, fees
            FROM lots ORDER BY trade_date DESC, id DESC LIMIT 30
            """,
        )
    finally:
        put_conn(conn)

    if not recent_lots:
        st.caption("No lots recorded yet.")
    else:
        lots_df = pd.DataFrame(recent_lots)
        for col in ("quantity", "price", "fees"):  # Decimal → float so NumberColumn formats apply
            lots_df[col] = lots_df[col].astype(float)
        lots_df["sell"] = "Sell"
        lots_df["delete"] = "Delete"

        def _clicked_lot(click_key: str) -> dict:
            row = lots_df.iloc[st.session_state[click_key]["row"]]
            return {
                "id": int(row["id"]), "symbol": row["symbol"], "account": row["account"],
                "side": row["side"], "trade_date": row["trade_date"],
                "quantity": row["quantity"], "price": row["price"],
            }

        def _sell_clicked():
            lot = _clicked_lot("m2_lot_sell_click")
            st.session_state["m2_lot_sym"] = lot["symbol"]
            st.session_state["m2_lot_side"] = "SELL"
            st.session_state["m2_lot_qty"] = float(lot["quantity"])
            acct = lot["account"] or "(none)"
            if acct in account_options:
                st.session_state["m2_lot_acct"] = acct

        def _delete_clicked():
            st.session_state["m2_pending_delete"] = _clicked_lot("m2_lot_del_click")

        st.dataframe(
            lots_df,
            width="stretch",
            hide_index=True,
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "trade_date": st.column_config.DateColumn("Trade date"),
                "quantity": st.column_config.NumberColumn("Qty", format="%.4f"),
                "price": st.column_config.NumberColumn("Price", format="$%.2f"),
                "fees": st.column_config.NumberColumn("Fees", format="$%.2f"),
                "sell": st.column_config.ButtonColumn(
                    "", type="tertiary", width="small",
                    help="Prefill the Add Lot form as a SELL of this lot",
                    on_click=_sell_clicked, key="m2_lot_sell_click",
                ),
                "delete": st.column_config.ButtonColumn(
                    "", type="tertiary", width="small",
                    help="Delete this lot (asks for confirmation)",
                    on_click=_delete_clicked, key="m2_lot_del_click",
                ),
            },
        )

    if st.session_state.get("m2_pending_delete") is not None:
        lot = st.session_state["m2_pending_delete"]
        st.warning(
            f"**Confirm delete lot #{lot['id']}:** {lot['side']} {lot['quantity']} × {lot['symbol']} "
            f"@ ${float(lot['price']):,.2f} on {lot['trade_date']} (account: {lot['account'] or '(none)'})"
        )
        c1, c2 = st.columns(2)
        if c1.button("✅ Confirm delete", type="primary", key="m2_confirm_del"):
            _delete_lot(get_conn, put_conn, int(lot["id"]))
            st.success(f"Deleted lot #{lot['id']}")
            st.session_state["m2_pending_delete"] = None
            st.cache_data.clear()
            st.rerun()
        if c2.button("Cancel", key="m2_cancel_del"):
            st.session_state["m2_pending_delete"] = None
            st.rerun()

    with st.expander("🗑 Delete by ID (older lots not shown above)"):
        del_id = st.number_input("Lot ID to delete", min_value=1, step=1, value=1, key="m2_del_id")
        if st.button("Delete lot", key="m2_del_by_id"):
            conn = get_conn()
            try:
                rows = fetch_all(
                    conn,
                    "SELECT id, symbol, account, side, trade_date, quantity, price FROM lots WHERE id = %s",
                    (int(del_id),),
                )
            finally:
                put_conn(conn)
            if not rows:
                st.error(f"Lot #{int(del_id)} not found")
            else:
                st.session_state["m2_pending_delete"] = rows[0]
                st.rerun()

    st.divider()
    st.subheader("📤 Exports")
    ex1, ex2 = st.columns(2)

    with ex1:
        st.caption("All recorded lots as CSV.")
        conn = get_conn()
        try:
            lot_rows = fetch_all(
                conn,
                """
                SELECT id, symbol, account, side, trade_date, quantity, price, fees, notes
                FROM lots ORDER BY trade_date DESC, id DESC
                """,
            )
        finally:
            put_conn(conn)
        lots_csv = pd.DataFrame(lot_rows).to_csv(index=False).encode("utf-8") if lot_rows else b""
        st.download_button(
            "📥 Lots CSV", data=lots_csv, file_name=f"lots_{date.today().isoformat()}.csv",
            mime="text/csv", disabled=not lot_rows,
        )

    with ex2:
        st.caption("Full executive report (HTML).")
        if st.button("🧾 Build executive report"):
            with st.spinner("Building report…"):
                from exec_report import build_html as build_exec_html
                conn = get_conn()
                try:
                    st.session_state["m2_exec_html"] = build_exec_html(conn)
                    st.session_state["m2_exec_date"] = date.today().isoformat()
                finally:
                    put_conn(conn)
        if st.session_state.get("m2_exec_html"):
            st.download_button(
                "📥 Download report", data=st.session_state["m2_exec_html"],
                file_name=f"portfolio_report_{st.session_state.get('m2_exec_date', date.today().isoformat())}.html",
                mime="text/html",
            )

    _render_settings_section()


# ── Settings section (Manage page) ───────────────────────────────────────


_SOURCE_LABEL = {"db": "set here", "env": "from .env", "default": "default"}


def _render_settings_section() -> None:
    """Runtime settings — DB-backed via app/settings.py (DB → env → default).

    Secrets are never shown or written here: the dashboard has no auth, so
    keys live in .env and this section only reports set / missing.
    """
    st.divider()
    st.subheader("⚙️ Settings")

    if not settings.db_available():
        st.warning(
            "The `settings` table is unreachable (missing migration or DB down) — "
            "values below come from `.env` / defaults and cannot be saved. "
            "Apply `sql/migrations/001_settings.sql` to enable saving."
        )

    def _src(key: str, env) -> str:
        return _SOURCE_LABEL[settings.source_of(key, env=env)]

    with st.form("m2_settings"):
        sc1, sc2 = st.columns(2)

        with sc1:
            name_val = sc1.text_input(
                "Display name",
                value=settings.get("display_name", env="PORTFOLIODB_DISPLAY_NAME", default="") or "",
                help="Shown in the dashboard rail footer. Empty = \"Operator\".",
            )
            sc1.caption(f"currently: {_src('display_name', 'PORTFOLIODB_DISPLAY_NAME')}")

            tz_val = sc1.text_input(
                "Reporting timezone (IANA name)",
                value=settings.get("reporting_tz", env="PORTFOLIODB_TZ", default="") or "",
                help="The calendar that buckets snapshots into days. Empty = default "
                     f"({reporting_tz.DEFAULT_TZ}). Applies on restart of the dashboard, "
                     "MCP server and scheduled jobs.",
            )
            sc1.caption(f"currently: {_src('reporting_tz', 'PORTFOLIODB_TZ')} · applies on restart")

            mw1, mw2 = sc1.columns(2)
            start_val = mw1.text_input(
                "Collector window start (HH:MM)",
                value=settings.get("market_window_start", env="PORTFOLIODB_MARKET_START", default="") or "",
                help=f"Empty = default ({market_window.DEFAULT_START}).",
            )
            end_val = mw2.text_input(
                "…end (HH:MM)",
                value=settings.get("market_window_end", env="PORTFOLIODB_MARKET_END", default="") or "",
                help=f"Empty = default ({market_window.DEFAULT_END}). May wrap past midnight.",
            )
            week_val = sc1.text_input(
                "Collector days (cron style, 1=Mon)",
                value=settings.get("market_week", env="PORTFOLIODB_MARKET_WEEK", default="") or "",
                help=f"e.g. 1-5 or 1,2,5. Empty = default ({market_window.DEFAULT_WEEK}).",
            )
            sc1.caption(f"snapshots collected {market_window.describe()}")

            mkt_val = sc1.text_input(
                "Market overview symbols",
                value=settings.get(market_overview.SETTING_KEY,
                                   env=market_overview.ENV_VAR, default="") or "",
                help="Comma-separated SYMBOL:Label, e.g. "
                     "ES=F:S&P Futures,^TA125.TA:TA-125. Any yfinance symbol works. "
                     "These are collected round the clock, ignoring the window above — "
                     "futures quote when your market is shut, which is the point. "
                     "Empty = the default US set; clear it entirely to hide the strip.",
            )
            sc1.caption(f"currently: {_src(market_overview.SETTING_KEY, market_overview.ENV_VAR)}")

        with sc2:
            providers = list(llm.PROVIDERS)
            current_provider = llm.provider()
            provider_val = sc2.selectbox(
                "Advisor LLM provider",
                options=providers,
                index=providers.index(current_provider),
                help="anthropic = native Claude SDK. The rest use the OpenAI-compatible "
                     "path (openai / openrouter / ollama / custom base URL).",
            )
            sc2.caption(f"currently: {_src('llm_provider', 'LLM_PROVIDER')}")

            model_val = sc2.text_input(
                "Advisor model",
                value=settings.get("llm_model", env=("LLM_MODEL", "PORTFOLIODB_ADVISOR_MODEL"), default="") or "",
                help="Empty = provider default.",
            )
            base_url_val = sc2.text_input(
                "Base URL (openrouter / ollama / custom only)",
                value=settings.get("llm_base_url", env="LLM_BASE_URL", default="") or "",
                help="Empty = provider default. From Docker, host Ollama is "
                     "http://host.docker.internal:11434/v1",
            )

        st.caption("Saving an empty field clears the override so `.env` / defaults apply again.")
        if st.form_submit_button("💾 Save settings", type="primary"):
            if tz_val.strip():
                try:
                    ZoneInfo(tz_val.strip())
                except Exception:
                    st.error(f"Unknown timezone: {tz_val!r} — use an IANA name like Europe/Berlin")
                    st.stop()
            for label, raw in (("start", start_val), ("end", end_val)):
                if raw.strip() and not re.match(r"^\d{1,2}:\d{2}$", raw.strip()):
                    st.error(f"Collector window {label} must look like 15:15 (got {raw!r})")
                    st.stop()
            # Only store a value that actually differs from what `.env` or the
            # default would give. The fields arrive pre-filled with the resolved
            # value, so writing them all would create overrides for settings the
            # operator never touched — and a DB row outranks `.env` forever
            # after, making a later `.env` edit look broken.
            for key, raw, env_names, dflt in (
                ("display_name", name_val, "PORTFOLIODB_DISPLAY_NAME", "Operator"),
                ("reporting_tz", tz_val, "PORTFOLIODB_TZ", reporting_tz.DEFAULT_TZ),
                (market_overview.SETTING_KEY, mkt_val, market_overview.ENV_VAR,
                 market_overview.DEFAULT_SYMBOLS),
                ("market_window_start", start_val, "PORTFOLIODB_MARKET_START", market_window.DEFAULT_START),
                ("market_window_end", end_val, "PORTFOLIODB_MARKET_END", market_window.DEFAULT_END),
                ("market_week", week_val, "PORTFOLIODB_MARKET_WEEK", market_window.DEFAULT_WEEK),
                ("llm_provider", provider_val, "LLM_PROVIDER", "anthropic"),
                ("llm_model", model_val, ("LLM_MODEL", "PORTFOLIODB_ADVISOR_MODEL"), None),
                ("llm_base_url", base_url_val, "LLM_BASE_URL", None),
            ):
                val = (raw or "").strip()
                if val and val != (settings.fallback(key, env=env_names, default=dflt) or ""):
                    settings.set_value(key, val)
                else:
                    settings.unset(key)
            st.success("Settings saved.")
            st.cache_data.clear()
            st.rerun()

    # Secret status — read-only by design; keys are edited in .env by hand.
    st.markdown("**Secrets** (status only — edit in the repo-root `.env`, then restart)")
    ks = llm.key_status()
    if ks["set"]:
        st.markdown(f"- Advisor API key ({ks['provider']}): ✅ set via `{ks['env_var']}`")
    elif ks["optional"]:
        st.markdown(f"- Advisor API key ({ks['provider']}): ⚪ not set (optional for this provider)")
    else:
        st.markdown(f"- Advisor API key ({ks['provider']}): ❌ missing — set `{ks['env_var']}` in `.env`")
    fd_set = bool(os.environ.get("FINANCIAL_DATASETS_API_KEY", "").strip())
    st.markdown(f"- Financial Datasets key: {'✅ set' if fd_set else '⚪ not set (enrichment disabled)'} ")


# ── Advisor page ─────────────────────────────────────────────────────────


def _esc_md(text: str) -> str:
    """Escape $ so st.markdown doesn't render dollar amounts as LaTeX math."""
    return (text or "").replace("$", "\\$")


_PHILOSOPHY_SOURCE_LABEL = {
    "database": "saved here",
    "file": "read from the mounted philosophy.md",
    "template": "unedited template",
    "none": "not written yet",
}


def _render_philosophy_editor(source: str) -> None:
    """Paste-and-save surface for the investor one-pager.

    This is the documented way in: a bind-mounted philosophy.md is only re-read
    when the container restarts if the editor replaced the file rather than
    writing into it — which most editors do — so a file-only workflow silently
    serves stale text.
    """
    with st.expander(f"📝 Investor one-pager · {_PHILOSOPHY_SOURCE_LABEL[source]}", expanded=source in ("none", "template")):
        st.caption(
            "Run the interview in `docs/investor-interview.md` against any LLM, "
            "then paste the result here. Saved to the database, included in "
            "`make backup`, and used from the next message onward — no restart. "
            "A mounted `philosophy.md` still works and is used when nothing is "
            "saved here."
        )
        current = settings.get(advisor.PHILOSOPHY_KEY) or ""
        if not current and source in ("file", "template"):
            # Pre-fill from the mounted file — including the template, so its
            # bracketed sections can be edited right here instead of forcing a
            # trip to the filesystem.
            st.info(
                "Showing the mounted file's contents. Saving stores a copy in the "
                "database, which then takes precedence over the file."
            )
            current = advisor._philosophy_from_file() or ""
        text = st.text_area(
            "Markdown", value=current, height=320, key="m2_phil_text",
            label_visibility="collapsed",
            placeholder="# Investor One-Pager — Your Name\n\n## North Star\n…",
            help="Paste the document your interview produced, then Save. "
                 "Takes effect on the next brief or message — no restart.",
        )
        pc1, pc2 = st.columns([1, 4])
        if pc1.button("💾 Save one-pager", type="primary", key="m2_phil_save"):
            advisor.save_philosophy(text)
            st.success("Saved." if text.strip() else "Cleared — the mounted file (if any) applies again.")
            st.cache_data.clear()
            st.rerun()
        if settings.get(advisor.PHILOSOPHY_KEY):
            if pc2.button("🗑 Clear saved copy", key="m2_phil_clear"):
                advisor.save_philosophy("")
                st.success("Cleared.")
                st.rerun()


def render_advisor(get_conn, put_conn, watchlist=None) -> None:
    import json

    inject_shell("advisor", "Advisor", "Your model, reading your own rules against your live ledger", watchlist)

    # Stated on the page itself, not only in the README: this is the one screen
    # whose output can read like a recommendation, and it is the screen a user is
    # looking at when it does.
    st.caption(
        "Generated by your model against rules you wrote. Not investment advice, "
        "and no substitute for a licensed adviser. Verify anything that would "
        "move money."
    )

    # The one-pager editor comes FIRST, before the provider gate: writing your
    # investing rules is a sensible thing to do before (or while) sorting out an
    # API key, and gating it behind the key left a keyless install with a page
    # that only knew how to complain.
    source = advisor.philosophy_source()
    phil_exists = source in ("database", "file")
    if source == "none":
        st.warning(
            "No investor one-pager yet — advice will be generic. "
            "Open **📝 Investor one-pager** below; `docs/investor-interview.md` "
            "has a prompt that interviews you and writes it for you."
        )
    elif source == "template":
        st.warning(
            "The one-pager is still the unedited template, so it carries no real "
            "preferences. Fill it in under **📝 Investor one-pager** below."
        )

    _render_philosophy_editor(source)

    # Ask the LLM layer, not the environment: the advisor has been
    # provider-agnostic since the settings work, and a local provider (Ollama)
    # needs no key at all. Checking ANTHROPIC_API_KEY directly made this tab
    # unusable for every provider except Anthropic.
    reason = advisor._advisor_disabled_reason()
    if reason:
        st.info(
            f"Briefs and chat need a model: {reason}. Your one-pager is saved "
            "either way — set the key in `.env` and restart to enable them."
        )
        return

    c1, c2, c3 = st.columns([1, 1, 3])
    if c1.button("🎯 Generate Brief", type="primary", help="Calls your configured LLM — usually a few cents"):
        with st.spinner("Asking the advisor..."):
            try:
                advisor.morning_brief()
                st.success("Brief generated.")
                st.rerun()
            except Exception as exc:
                st.error(f"Brief failed: {exc}")
    if c2.button("🗑 Reset chat", help="Clears chat_log — keeps briefs"):
        conn = get_conn()
        try:
            execute(conn, "DELETE FROM chat_log WHERE conversation_id = %s", ("default",))
        finally:
            put_conn(conn)
        st.rerun()
    # advisor.DEFAULT_MODEL was removed when the provider layer landed; reading
    # it here raised AttributeError for anyone who *had* a key configured, so
    # this whole tab crashed while a keyless install (which returns above)
    # looked fine.
    c3.markdown(
        f"<span style='opacity:.7;font-size:.85rem'>Model: "
        f"<code>{html.escape(llm.provider())}:{html.escape(llm.model())}</code> · "
        f"Philosophy: {'✓ loaded' if phil_exists else '✗ missing'}</span>",
        unsafe_allow_html=True,
    )

    st.divider()

    conn = get_conn()
    try:
        brief_rows = fetch_all(
            conn,
            "SELECT id, ts, kind, total_value, payload FROM advisor_briefs ORDER BY id DESC LIMIT 5",
        )
    finally:
        put_conn(conn)

    if brief_rows:
        latest = brief_rows[0]
        payload = latest["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        ts_local = latest["ts"].astimezone() if latest["ts"] else None
        ts_str = ts_local.strftime("%Y-%m-%d %H:%M") if ts_local else "—"
        nav = float(latest["total_value"]) if latest["total_value"] is not None else 0.0

        st.markdown(f"### Latest brief — {ts_str} · NAV \\${nav:,.0f}")
        st.markdown(_esc_md(payload.get("markdown", "_No markdown in payload._")))

        insights = payload.get("insights") or []
        suggestions = payload.get("suggestions") or []
        if insights:
            with st.expander(f"💡 Insights ({len(insights)})", expanded=False):
                for ins in insights:
                    st.markdown(f"**{_esc_md(ins.get('title','(no title)'))}** &nbsp; `{ins.get('tag','')}`")
                    st.markdown(_esc_md(ins.get("body", "")))
                    st.markdown("---")
        if suggestions:
            with st.expander(f"🎯 Suggestions ({len(suggestions)})", expanded=False):
                for s in suggestions:
                    st.markdown(f"**{_esc_md(s.get('action','(no action)'))}**")
                    st.markdown(_esc_md(s.get("rationale", "")))
                    st.caption(f"Rule invoked: {s.get('rule_invoked') or '—'}")
                    st.markdown("---")
        if len(brief_rows) > 1:
            with st.expander(f"📚 Previous briefs ({len(brief_rows) - 1})", expanded=False):
                for r in brief_rows[1:]:
                    prev_ts = r["ts"].astimezone().strftime("%Y-%m-%d %H:%M") if r["ts"] else "—"
                    prev_payload = r["payload"]
                    if isinstance(prev_payload, str):
                        prev_payload = json.loads(prev_payload)
                    prev_nav = float(r["total_value"]) if r["total_value"] is not None else 0.0
                    st.markdown(f"**{prev_ts} · NAV \\${prev_nav:,.0f}** — {_esc_md(prev_payload.get('summary',''))}")
    else:
        st.info("No briefs yet. Click **Generate Brief** to create one.")

    st.divider()
    st.subheader("💬 Chat")

    conn = get_conn()
    try:
        chat_rows = fetch_all(
            conn,
            "SELECT role, content FROM chat_log WHERE conversation_id = %s ORDER BY id ASC LIMIT 100",
            ("default",),
        )
    finally:
        put_conn(conn)
    for msg in chat_rows:
        with st.chat_message(msg["role"]):
            st.markdown(_esc_md(msg["content"]))

    if question := st.chat_input("Ask the advisor about your portfolio..."):
        with st.chat_message("user"):
            st.markdown(_esc_md(question))
        with st.chat_message("assistant"):
            try:
                st.write_stream(_esc_md(tok) for tok in advisor.chat(question))
            except Exception as exc:
                st.error(f"Chat failed: {exc}")


# ── Data Health page ─────────────────────────────────────────────────────


# Streamlit status colours for each data-quality severity. Green is reserved
# for "complete" so a glance at the page answers the only question that matters.
_STATUS_STYLE = {
    "complete": ("✅", "success"),
    "partial": ("🟡", "warning"),
    "stale": ("🟠", "warning"),
    "unavailable": ("⚪", "warning"),
    "inconsistent": ("🔴", "error"),
}


def _status_chip(status: str) -> str:
    icon, _ = _STATUS_STYLE.get(status, ("•", "info"))
    return f"{icon} {status}"


def render_health(get_conn, put_conn, watchlist=None) -> None:
    """Portfolio data-health panel — the dashboard face of get_data_quality.

    Answers "can I trust what the other views are showing me": which symbols
    have stale, missing or self-contradictory data, and whether the price
    collector is alive. Read-only.
    """
    inject_shell(
        "health",
        "Data Health",
        "Which numbers you can trust, and exactly why not when you cannot",
        watchlist,
    )

    # run_dashboard.ps1 launches streamlit from app/, so the repo root is not on
    # sys.path and `app.mcp...` will not resolve. Add it here rather than
    # globally: this is the only view that reaches into the MCP services, and
    # putting app/ ahead of the root would shadow the official `mcp` SDK with
    # the local app/mcp package.
    import sys
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    from app.mcp.services import data_quality as dq_service

    materiality = st.session_state.get("dq_materiality", 2.0)

    c1, c2, c3 = st.columns([1, 1, 3])
    with c1:
        method = st.selectbox("Cost basis", ["fifo", "avg"], key="dq_method")
    with c2:
        materiality = st.number_input(
            "Material at ≥ (%)", min_value=0.0, max_value=100.0,
            value=float(materiality), step=0.5, key="dq_materiality",
            help=(
                "Freshness and completeness issues escalate above this weight. "
                "Correctness issues — orphaned sells, missing cost basis, "
                "suspected splits — are always material, because a wrong "
                "number is wrong at any position size."
            ),
        )

    try:
        report = dq_service.portfolio_data_quality(
            method=method, materiality_pct=float(materiality)
        )
    except Exception as e:  # pragma: no cover - surfaced in the UI
        from app.mcp.deps import explain_db_error

        st.error(f"Could not build the data-quality report. {explain_db_error(e)}")
        return

    overall = report["overall_status"]
    _, kind = _STATUS_STYLE.get(overall, ("•", "info"))
    banner = getattr(st, kind, st.info)
    banner(f"**{_status_chip(overall)}** — {report['overall_explanation']}")

    counts = report["counts"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Symbols checked", counts["symbols_checked"])
    m2.metric("Fully clean", counts["symbols_complete"])
    m3.metric("Material issues", counts["material_issues"])
    m4.metric("Minor issues", counts["minor_issues"])

    st.caption(
        f"As of {report['meta']['as_of_local']} ({report['meta']['timezone']}) · "
        f"coverage {report['meta']['coverage_start']} → {report['meta']['coverage_end']} · "
        f"{report['meta']['reporting_currency']} · build {report['meta']['app_version']}"
    )

    st.divider()

    # ── collector ──
    collector = report["collector"]
    st.subheader(f"Collector — {_status_chip(collector['status'])}")
    st.write(collector["message"])
    for issue in collector["issues"]:
        st.warning(f"**{issue['code']}** — {issue['message']}")
    if not collector["issues"]:
        st.caption(
            "Staleness is judged against the collector's own runs rather than a "
            "clock threshold: the weekend gap is 64.2h, so any hour-based rule "
            "either fires every Monday or misses a mid-week outage."
        )

    # ── issues ──
    if report["material_issues"]:
        st.subheader("Material issues")
        for issue in report["material_issues"]:
            icon, _kind = _STATUS_STYLE.get(issue["severity"], ("•", "info"))
            with st.expander(
                f"{icon} {issue['symbol']} — {issue['code']} "
                f"({issue['weight_pct']:.1f}% of market value)",
                expanded=True,
            ):
                st.write(_esc_md(issue["message"]))
                extra = {
                    k: v for k, v in issue.items()
                    if k not in ("code", "severity", "message", "symbol", "weight_pct")
                }
                if extra:
                    st.json(extra, expanded=False)

    if report["minor_issues"]:
        with st.expander(f"Minor issues ({len(report['minor_issues'])})"):
            for issue in report["minor_issues"]:
                st.markdown(
                    f"- **{issue['symbol']}** · `{issue['code']}` "
                    f"({issue['severity']}, {issue['weight_pct']:.1f}%) — "
                    f"{_esc_md(issue['message'])}"
                )

    # ── per-symbol table ──
    st.subheader("Per-symbol status")
    rows = [
        {
            "Symbol": s["symbol"],
            "Status": _status_chip(s["status"]),
            "Held": "yes" if s["held"] else "no",
            "Weight %": round(s["weight_pct"], 2),
            "Market value": s["market_value"],
            "Issues": ", ".join(i["code"] for i in s["issues"]) or "—",
        }
        for s in report["symbols"]
    ]
    st.dataframe(rows, hide_index=True, width="stretch")

    st.caption(
        "Scope: symbols the collector targets — held or watchlisted. Closed, "
        "unwatched positions are excluded; their prices are stale by design."
    )
