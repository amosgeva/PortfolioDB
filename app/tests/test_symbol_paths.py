"""Symbols become file names only when they look like symbols.

Both directions of the logo cache — the fetcher writing, the dashboard
reading — go through one allowlist. A symbol with a path separator in it is
operator-entered text that could otherwise write outside the cache directory
or read a file from outside it into the page.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import symbol_paths  # noqa: E402

REAL = ["NVDA", "BRK.B", "BRK-B", "^GSPC", "ES=F", "BTC-USD", "0700.HK", "TA35.TA", "^TA125.TA", "A"]
NOT_SYMBOLS = ["../x", "..\\x", "a/b", "NVDA/", "/etc/passwd", "", " NVDA", "NVDA ", "nvda",
               "NV\x00DA", "X" * 33, ".hidden", "-", None, 42]


@pytest.mark.parametrize("sym", REAL)
def test_real_symbols_get_a_path(sym, tmp_path):
    assert symbol_paths.is_safe_symbol(sym)
    assert symbol_paths.logo_path(tmp_path, sym) == tmp_path / f"{sym}.png"


@pytest.mark.parametrize("sym", NOT_SYMBOLS)
def test_anything_else_gets_none(sym, tmp_path):
    assert not symbol_paths.is_safe_symbol(sym)
    assert symbol_paths.logo_path(tmp_path, sym) is None


def test_payload_never_reads_outside_the_logo_dir(tmp_path, monkeypatch):
    """A symbol of `../secret` must not turn a file beside the cache into a
    data URI in the page."""
    from dashboard import payload

    logos = tmp_path / "logos"
    logos.mkdir()
    (logos / "NVDA.png").write_bytes(b"\x89PNG-nvda")
    (tmp_path / "secret.png").write_bytes(b"do-not-serve")
    monkeypatch.setattr(payload, "_LOGO_DIR", logos)

    out = payload._logo_data_uris(["NVDA", "../secret", "NVDA/../../secret"])
    assert set(out) == {"NVDA"}
    assert "do-not-serve".encode() not in b"".join(v.encode() for v in out.values())


def test_fetcher_never_writes_outside_the_logo_dir(tmp_path, monkeypatch, capsys):
    import fetch_ticker_logos as ftl

    logos = tmp_path / "logos"
    monkeypatch.setattr(ftl, "LOGO_DIR", logos)
    monkeypatch.setattr(ftl, "fetch_logo", lambda sym: b"png-bytes")
    monkeypatch.setattr(ftl.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["fetch_ticker_logos.py", "--symbols", "NVDA", "../escaped", "BRK.B"])
    ftl.main()

    written = sorted(p.name for p in logos.iterdir())
    assert written == ["BRK.B.png", "NVDA.png"]
    assert not (tmp_path / "escaped.png").exists()
    assert "not a symbol" in capsys.readouterr().out


def test_dashboard_builds_symbol_options_as_dom_nodes():
    """The two <select>s that list symbols used to interpolate them into
    innerHTML unescaped. They must use the Option constructor, and no
    `'<option value="' + x` interpolation may come back anywhere in app.js."""
    src = (Path(__file__).resolve().parents[1] / "dashboard" / "static" / "app.js").read_text(encoding="utf-8")
    assert "'<option value=\"' +" not in src
    assert src.count("new Option(s, s)") >= 2
