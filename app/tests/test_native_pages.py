"""The native pages' pure parts: tokens, theme, rail, exports, parsing.

No database and no browser. The pages themselves run end to end in
test_native_pages_apptest.py.
"""

from __future__ import annotations

import math
import re
import tomllib
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import modern2_native as m2
import native_writes as nw

APP = Path(__file__).resolve().parents[1]


# ── R-3: one source for the design tokens ────────────────────────────────


def test_native_css_defines_no_design_token_of_its_own():
    # The hand-copied token set drifted from app.css; the page now carries the
    # front end's blocks instead. Only tokens app.css lacks may be defined here.
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", m2.NATIVE_CSS))
    assert defined <= {"--warn-soft", "--tabbar-h"}, defined


def test_page_css_carries_the_front_ends_token_blocks_verbatim():
    app_css = (APP / "dashboard" / "static" / "app.css").read_text(encoding="utf-8")
    css = m2.page_css("manage")
    tokens = m2.design_tokens_css()
    assert tokens in css
    assert tokens.split("\n", 1)[0].startswith(":root{")
    assert '[data-theme="dark"]{' in tokens
    for line in tokens.splitlines():
        assert line in app_css


# ── N-4: the theme ships in config.toml ──────────────────────────────────


def _oklch_to_hex(L, C, h):
    L /= 100
    a, b = C * math.cos(math.radians(h)), C * math.sin(math.radians(h))
    l_, m_, s_ = (L + 0.3963377774 * a + 0.2158037573 * b,
                  L - 0.1055613458 * a - 0.0638541728 * b,
                  L - 0.0894841775 * a - 1.2914855480 * b)
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    rgb = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
           -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
           -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)

    def f(x):
        x = max(0.0, min(1.0, x))
        return round(255 * (12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055))

    return "#%02x%02x%02x" % tuple(f(c) for c in rgb)


def _token(block: str, name: str):
    m = re.search(rf"{name}:oklch\(([\d.]+)% ([\d.]+) ([\d.]+)\)", block)
    return _oklch_to_hex(*map(float, m.groups()))


def test_config_toml_theme_matches_the_design_tokens():
    cfg = tomllib.loads((APP / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    theme = cfg["theme"]
    light, dark = m2.design_tokens_css().split('[data-theme="dark"]', 1)
    assert theme["primaryColor"] == _token(light, "--accent")
    assert theme["light"]["primaryColor"] == _token(light, "--accent")
    assert theme["dark"]["primaryColor"] == _token(dark, "--accent")
    assert theme["light"]["backgroundColor"] == _token(light, "--bg")
    assert theme["dark"]["backgroundColor"] == _token(dark, "--bg")
    assert theme["light"]["textColor"] == _token(light, "--fg")
    assert theme["dark"]["textColor"] == _token(dark, "--fg")
    assert theme["font"].startswith("Space Grotesk")
    assert cfg["server"]["enableStaticServing"] is True


# ── N-8: no emoji in the native pages ────────────────────────────────────

_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⬆↔-↪⌚-⏿]")


def test_no_emoji_anywhere_in_the_native_pages():
    src = (APP / "modern2_native.py").read_text(encoding="utf-8")
    hits = [(i + 1, line.strip()) for i, line in enumerate(src.splitlines()) if _EMOJI.search(line)]
    assert hits == [], hits


def test_status_is_a_word_in_a_tinted_well():
    out = m2._tag("unlocked", "ok")
    assert out == '<span class="m2-tag m2-tag--ok">unlocked</span>'
    assert "m2-tag--ok" in m2.NATIVE_CSS


# ── rail: a missing day change is "n/a", not a green +0.00% ──────────────


def test_rail_watchlist_shows_missing_day_change_as_na():
    out = m2._watchlist_html([{"sym": "AAA", "price": 10.0, "dayPct": None}])
    assert ">n/a<" in out
    assert "+0.00%" not in out
    assert 'wl__chg up' not in out


def test_rail_watchlist_signs_real_changes():
    out = m2._watchlist_html([
        {"sym": "UP", "price": 1.0, "dayPct": 0.0},
        {"sym": "DN", "price": 1.0, "dayPct": -1.25},
    ])
    assert 'class="wl__chg up">+0.00%' in out
    assert 'class="wl__chg down">-1.25%' in out


def test_rail_watchlist_escapes_symbols():
    out = m2._watchlist_html([{"sym": "<img src=x>", "price": 1, "dayPct": 1}])
    assert "<img" not in out


# ── N-9: the reporting timezone, not the process's ───────────────────────


def test_timestamps_render_in_the_reporting_timezone(monkeypatch):
    # conftest pins Asia/Jerusalem; 21:30 UTC on 1 March is 23:30 there.
    ts = datetime(2026, 3, 1, 21, 30, tzinfo=timezone.utc)
    assert m2._fmt_local(ts) == "2026-03-01 23:30"
    # A naive value is read as UTC (Postgres timestamptz always arrives aware).
    assert m2._fmt_local(datetime(2026, 3, 1, 21, 30)) == "2026-03-01 23:30"


def test_default_trade_date_is_today_in_the_reporting_timezone():
    assert m2.today_local() == datetime.now(ZoneInfo("Asia/Jerusalem")).date()


# ── A-4: CSV export neutralises formula prefixes ─────────────────────────


@pytest.mark.parametrize("cell", ["=1+1", "+SUM(A1)", "-2+3", "@cmd", "\tx", "\r=1"])
def test_csv_safe_neutralises_formula_text(cell):
    assert nw.csv_safe(cell) == "'" + cell


def test_csv_safe_leaves_numbers_and_plain_text_alone():
    assert nw.csv_safe(Decimal("-5")) == Decimal("-5")
    assert nw.csv_safe(-5.5) == -5.5
    assert nw.csv_safe("NVDA") == "NVDA"
    assert nw.csv_safe(None) is None


def test_lots_csv_writes_neutralised_notes_and_exact_numbers():
    rows = [{"id": 1, "symbol": "AAA", "account": "=cmd|' /C calc'!A0", "side": "SELL",
             "trade_date": date(2026, 1, 2), "quantity": Decimal("0.12345678"),
             "price": Decimal("-1"), "fees": Decimal("0"), "notes": '=HYPERLINK("http://x")'}]
    text = nw.lots_csv(rows).decode("utf-8")
    header, line = text.strip().split("\n")
    assert header == "id,symbol,account,side,trade_date,quantity,price,fees,notes"
    assert "'=cmd|' /C calc'!A0" in line
    assert "'=HYPERLINK" in line
    assert ",0.12345678,-1,0," in line
    assert nw.lots_csv([]) == b""


# ── N-6: parsing is Decimal, and refuses what the ledger cannot hold ─────


def test_parse_lot_is_exact_decimal():
    lot = nw.parse_lot(symbol=" nvda ", account="  ", side="BUY", trade_date=date(2026, 1, 2),
                       quantity="0.1", price="184.37", fees="", notes="  ")
    assert lot.symbol == "NVDA"
    assert lot.account is None and lot.notes is None
    assert lot.quantity == Decimal("0.1") and isinstance(lot.quantity, Decimal)
    assert lot.price == Decimal("184.37") and lot.fees == Decimal("0")


@pytest.mark.parametrize("field,value,msg", [
    ("quantity", "NaN", "NaN"),
    ("quantity", "0", "greater than 0"),
    ("quantity", "1e13", "too large"),
    ("price", "0", "greater than 0"),
    ("price", "abc", "not a number"),
    ("fees", "-1", "at least 0"),
    ("symbol", "<script>", "not a symbol"),
    ("symbol", "", "required"),
    ("symbol", "../../etc", "not a symbol"),
])
def test_parse_lot_refuses_bad_input(field, value, msg):
    args = dict(symbol="NVDA", account=None, side="BUY", trade_date=date(2026, 1, 2),
                quantity="1", price="10", fees="0", notes=None)
    args[field] = value
    with pytest.raises(ValueError, match=msg):
        nw.parse_lot(**args)


def test_normalize_symbol_accepts_real_venue_symbols():
    for sym in ("brk.b", "BRK-B", "^GSPC", "ES=F", "0700.HK", "TA35.TA"):
        assert nw.normalize_symbol(sym) == sym.upper()


@pytest.mark.parametrize("raw", ["0", "-5", "NaN", "inf", "x"])
def test_alert_level_must_be_finite_and_positive(raw):
    with pytest.raises(ValueError):
        nw.parse_level(raw)


def test_on_screen_tables_neutralise_text_too():
    # st.dataframe has its own "Download as CSV" in its toolbar, so the frames
    # it shows get the same treatment as the export.
    df = m2._display_frame(
        [{"symbol": "AAA", "account": "=cmd", "cash": Decimal("-1"), "note": "@x"}],
        ("symbol", "account", "note"),
    )
    assert df.loc[0, "account"] == "'=cmd"
    assert df.loc[0, "note"] == "'@x"
    assert df.loc[0, "symbol"] == "AAA"
    assert df.loc[0, "cash"] == Decimal("-1")
