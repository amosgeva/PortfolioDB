"""The native half of the navigation contract (audit R-3).

``dashboard/static/nav.json`` is the one description of the rail, the phone tab
bars, the page headings and every rail glyph. The custom front end draws its
rail and tab bar from it (``src/ui/nav.js``, checked by
``app/dashboard/static/tests/nav.test.mjs``); this file checks the other half:
that ``dashboard.nav`` refuses what a renderer could not draw, that the native
rail, tab bar and icon masks ``modern2_native`` renders are exactly what the
file says, and that ``streamlit_app`` routes and wires the hidden navigation
buttons from it. A navigation or icon change therefore cannot land on one half
only: whichever half it misses fails its contract test.

The first group needs nothing; the last test drives ``streamlit_app.py`` itself
and needs the database, like the other AppTests (skipped without one, a
failure when PORTFOLIODB_TESTS_REQUIRE_DB is set).
"""

from __future__ import annotations

import copy
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

import pytest
from streamlit.testing.v1 import AppTest

from dashboard import nav

APP = Path(__file__).resolve().parent.parent
SVG_NS = "{http://www.w3.org/2000/svg}"
NATIVE = {g["id"] for g in nav.NAV["groups"] if g["renderer"] == "native"}


def _glyph(markup: str) -> dict:
    """An SVG string as nav.json writes a glyph."""
    root = ET.fromstring(markup)
    shapes = []
    for el in root:
        tag = el.tag.replace(SVG_NS, "")
        shapes.append({"el": tag, **{a: el.get(a) for a in nav.SHAPE_ATTRS[tag]}})
    return {
        "stroke": root.get("stroke"), "width": root.get("stroke-width"),
        "cap": root.get("stroke-linecap"), "join": root.get("stroke-linejoin"), "shapes": shapes,
    }


# ── the file and its validator ─────────────────────────────────────────────


def test_every_glyph_renders_exactly_what_nav_json_says():
    for name, icon in nav.NAV["icons"].items():
        for markup in (nav.svg(name), nav.svg(name, xmlns=True)):
            assert _glyph(markup) == icon, name


def _broken(edit) -> dict:
    doc = copy.deepcopy(json.loads(nav.NAV_JSON.read_text(encoding="utf-8")))
    edit(doc)
    return doc


@pytest.mark.parametrize("edit, fragment", [
    (lambda d: d["views"][0].update(icon="nope"), "is not in icons"),
    (lambda d: d["icons"]["grid"]["shapes"].append({"el": "polygon", "points": "1 2"}), "polygon"),
    (lambda d: d["icons"]["trend"]["shapes"][0].update(onload="x"), "exactly"),
    (lambda d: d["icons"]["trend"]["shapes"][0].update(d='M0 0" onload="x'), "trend"),
    (lambda d: d["icons"]["trend"].update(stroke="url(#x)"), "stroke"),
    (lambda d: d["tabbar"]["frame"].pop(), "5 entries"),
    (lambda d: d["tabbar"]["native"].__setitem__(0, "elsewhere"), "unknown view"),
    (lambda d: d["views"].append(dict(d["views"][0])), "duplicate view id"),
    (lambda d: d["views"].insert(0, d["views"].pop()), "group by group"),
    (lambda d: d["groups"][1].update(renderer="iframe"), "renderer"),
    (lambda d: d["views"][2].update(label=" "), "label"),
])
def test_validation_refuses_what_a_renderer_could_not_draw(edit, fragment):
    doc = _broken(edit)
    with pytest.raises(nav.NavError, match=re.escape(fragment)):
        nav.validate(doc)


def test_the_two_halves_are_the_two_renderers():
    assert set(nav.ids_for("frame")) | set(nav.ids_for("native")) == {v["id"] for v in nav.views()}
    assert not set(nav.ids_for("frame")) & set(nav.ids_for("native"))
    assert nav.ids_for("native"), "no native pages: the hidden pdb_nav buttons would have nothing to open"


# ── the native rail, tab bar and icon masks ────────────────────────────────


def test_native_icon_masks_are_the_nav_glyphs():
    import modern2_native

    css = modern2_native._nav_css("manage")
    for v in nav.views():
        m = re.search(rf'\.st-key-m2_nav_{v["id"]},\.st-key-m2_tab_{v["id"]}\{{--ico:url\("data:image/svg\+xml,([^"]+)"\)\}}', css)
        assert m, f"no icon mask for {v['id']}"
        assert _glyph(unquote(m.group(1))) == nav.NAV["icons"][v["icon"]], v["id"]


def _rail_and_tabbar():
    import modern2_native

    modern2_native._render_rail("manage", [])
    modern2_native._render_tabbar()


def test_native_rail_and_tab_bar_are_nav_json_in_order():
    at = AppTest.from_function(_rail_and_tabbar, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    buttons = [(b.key, b.label) for b in at.button]
    rail = [(f"m2_nav_{v['id']}", v["label"]) for v in nav.views()]
    tabbar = [(f"m2_tab_{v['id']}", v["short"]) for v in nav.tabbar("native")]
    assert buttons == rail + tabbar
    labels = re.findall(r'class="m2-rail-label">([^<]+)<', "\n".join(h.proto.body for h in at.get("html")))
    assert labels[: len(nav.groups())] == [g["label"] for g in nav.groups()]


def test_no_view_list_or_glyph_is_hard_coded_on_the_python_side():
    for name in ("modern2_native.py", "streamlit_app.py"):
        src = (APP / name).read_text(encoding="utf-8")
        assert "<svg" not in src, f"{name} carries its own SVG; add the glyph to nav.json"
        for v in nav.views():
            if v["label"] != v["id"].title():  # "Market Movers", "Alerts & News", "Data Health" …
                assert f'"{v["label"]}"' not in src, f"{name} hard-codes the {v['id']} label"
        assert not re.search(r'\("manage",\s*"advisor"', src), f"{name} hard-codes the native view list"


# ── streamlit_app.py: routing and the hidden navigation buttons ─────────────


@pytest.fixture(scope="module")
def db():
    from db import connect_for_tests

    conn = connect_for_tests(skip=pytest.skip, fail=pytest.fail)
    yield conn
    conn.close()


def _run(view: str) -> AppTest:
    at = AppTest.from_file(str(APP / "streamlit_app.py"), default_timeout=120)
    at.query_params["view"] = view
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_streamlit_app_routes_and_wires_every_view_from_nav_json(db):
    frame = _run(nav.ids_for("frame")[0])
    # The front end's native entries click these (core/host.js nativeNav).
    hidden = [b.key for b in frame.button if (b.key or "").startswith("pdb_nav_")]
    assert hidden == [f"pdb_nav_{vid}" for vid in nav.ids_for("native")]
    srcdoc = "\n".join(f.proto.srcdoc for f in frame.get("iframe"))
    assert "data-rail-nav" in srcdoc
    assert "data-tabbar" in srcdoc
    assert not re.search(r"__[A-Z_]+__", srcdoc.split("const DATA")[0]), "a shell placeholder was left unfilled"

    for vid in nav.ids_for("native"):
        at = _run(vid)
        meta = nav.view(vid)
        heading = "\n".join(m.value for m in at.markdown)
        assert f"<h1>{meta['label']}</h1>" in heading.replace("&amp;", "&"), vid
        assert at.button(key=f"m2_nav_{vid}"), vid
