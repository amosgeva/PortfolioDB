"""The dashboard's navigation and glyphs, read from the one file both halves use.

``dashboard/static/nav.json`` names every view (id, group, label, short label,
subtitle, icon), the two phone tab bars, and each glyph as data. The custom
front end bundles it (``src/ui/nav.js``) and draws its rail and tab bar from
it; the native pages (``modern2_native``) and the iframe host
(``streamlit_app``) read it through this module. Before 1.8.0 each half carried
its own copy of the labels and SVG paths, and a change landed on one of them
only (audit R-3).

The file is validated on import: an unknown icon, an unknown view in a tab
bar, a shape or attribute neither renderer knows, or a duplicate id raises
here, so a bad edit fails the first test that imports the dashboard instead of
rendering a rail with a hole in it. The front end applies the same shape rules
(``src/ui/nav.js``); app/tests/test_nav_contract.py and tests/nav.test.mjs
check that both halves render what this file says.
"""

from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path
from types import MappingProxyType

NAV_JSON = Path(__file__).resolve().parent / "static" / "nav.json"

# Every attribute a shape may carry, in the order both renderers write them.
SHAPE_ATTRS = MappingProxyType({
    "path": ("d",),
    "rect": ("x", "y", "width", "height", "rx"),
    "circle": ("cx", "cy", "r"),
    "line": ("x1", "y1", "x2", "y2"),
})
ICON_KEYS = ("stroke", "width", "cap", "join", "shapes")
RENDERERS = ("frame", "native")
# Both stylesheets lay the phone tab bar out as repeat(5, 1fr).
TABBAR_LEN = 5

_ID = re.compile(r"^[a-z_]+$")
# Attribute values are numbers, path data or a colour: nothing that needs, or
# could survive, escaping.
_VALUE = re.compile(r"^[A-Za-z0-9#.,\- ]+$")


class NavError(ValueError):
    """nav.json does not describe something both renderers can draw."""


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise NavError(f"{NAV_JSON.name}: {msg}")


def validate(doc: dict) -> dict:
    """Return ``doc`` if both renderers can draw it; raise NavError if not."""
    icons = doc.get("icons")
    _check(isinstance(icons, dict) and icons, "no icons")
    for name, icon in icons.items():
        _check(bool(_ID.match(name)), f"icon name {name!r}")
        _check(isinstance(icon, dict) and tuple(sorted(icon)) == tuple(sorted(ICON_KEYS)),
               f"icon {name!r} must have exactly {ICON_KEYS}")
        for key in ICON_KEYS[:4]:
            _check(isinstance(icon[key], str) and bool(_VALUE.match(icon[key])), f"icon {name!r} {key}")
        _check(isinstance(icon["shapes"], list) and icon["shapes"], f"icon {name!r} has no shapes")
        for shape in icon["shapes"]:
            el = shape.get("el") if isinstance(shape, dict) else None
            _check(el in SHAPE_ATTRS, f"icon {name!r}: shape {el!r} is not one of {tuple(SHAPE_ATTRS)}")
            _check(set(shape) == {"el", *SHAPE_ATTRS[el]},
                   f"icon {name!r}: a {el} has exactly {SHAPE_ATTRS[el]}")
            for attr in SHAPE_ATTRS[el]:
                val = shape[attr]
                _check(isinstance(val, str) and bool(_VALUE.match(val)), f"icon {name!r}: {el} {attr}={val!r}")

    groups = doc.get("groups")
    _check(isinstance(groups, list) and groups, "no groups")
    group_ids = [g.get("id") for g in groups]
    _check(len(set(group_ids)) == len(group_ids), "duplicate group id")
    for g in groups:
        _check(bool(_ID.match(str(g.get("id")))) and isinstance(g.get("label"), str), f"group {g!r}")
        _check(g.get("renderer") in RENDERERS, f"group {g.get('id')!r} renderer must be one of {RENDERERS}")

    views = doc.get("views")
    _check(isinstance(views, list) and views, "no views")
    ids = [v.get("id") for v in views]
    _check(len(set(ids)) == len(ids), "duplicate view id")
    for v in views:
        vid = v.get("id")
        _check(isinstance(vid, str) and bool(_ID.match(vid)), f"view id {vid!r}")
        _check(v.get("group") in group_ids, f"view {vid!r} group {v.get('group')!r}")
        _check(v.get("icon") in icons, f"view {vid!r} icon {v.get('icon')!r} is not in icons")
        for key in ("label", "short", "subtitle"):
            _check(isinstance(v.get(key), str) and v[key].strip(), f"view {vid!r} {key}")
        _check(isinstance(v.get("badge", False), bool), f"view {vid!r} badge must be true/false")
    # The rail draws groups in file order; views must not interleave groups.
    order = [v["group"] for v in views]
    _check(order == sorted(order, key=group_ids.index), "views must be listed group by group, in group order")

    tabbar = doc.get("tabbar")
    _check(isinstance(tabbar, dict) and set(tabbar) == set(RENDERERS), f"tabbar needs exactly {RENDERERS}")
    for which, entries in tabbar.items():
        _check(isinstance(entries, list) and len(entries) == TABBAR_LEN,
               f"tabbar.{which} must have {TABBAR_LEN} entries (the CSS grid has {TABBAR_LEN} columns)")
        _check(len(set(entries)) == len(entries), f"tabbar.{which} repeats a view")
        for vid in entries:
            _check(vid in ids, f"tabbar.{which}: unknown view {vid!r}")
    return doc


NAV: dict = validate(json.loads(NAV_JSON.read_text(encoding="utf-8")))
_BY_ID = {v["id"]: v for v in NAV["views"]}
_RENDERER = {g["id"]: g["renderer"] for g in NAV["groups"]}


def views() -> list[dict]:
    """Every view, in rail order."""
    return list(NAV["views"])


def view(view_id: str) -> dict:
    return _BY_ID[view_id]


def groups() -> list[dict]:
    return list(NAV["groups"])


def group_views(group_id: str) -> list[dict]:
    return [v for v in NAV["views"] if v["group"] == group_id]


def ids_for(renderer: str) -> tuple[str, ...]:
    """View ids drawn by ``renderer``: "frame" (the custom front end's panes)
    or "native" (Streamlit pages)."""
    return tuple(v["id"] for v in NAV["views"] if _RENDERER[v["group"]] == renderer)


def tabbar(renderer: str) -> list[dict]:
    """The phone tab bar a renderer draws, as views."""
    return [_BY_ID[vid] for vid in NAV["tabbar"][renderer]]


def icon_names() -> tuple[str, ...]:
    return tuple(NAV["icons"])


def svg(name: str, *, xmlns: bool = False) -> str:
    """The glyph as inline SVG markup, attribute for attribute what the front
    end's icon() renders. ``xmlns`` for an SVG used as an image (a CSS mask)."""
    icon = NAV["icons"][name]
    shapes = "".join(
        "<" + s["el"] + "".join(f' {a}="{escape(s[a])}"' for a in SHAPE_ATTRS[s["el"]]) + "/>"
        for s in icon["shapes"]
    )
    ns = ' xmlns="http://www.w3.org/2000/svg"' if xmlns else ""
    return (
        f'<svg{ns} viewBox="0 0 24 24" fill="none" stroke="{escape(icon["stroke"])}" '
        f'stroke-width="{escape(icon["width"])}" stroke-linecap="{escape(icon["cap"])}" '
        f'stroke-linejoin="{escape(icon["join"])}" aria-hidden="true">{shapes}</svg>'
    )
