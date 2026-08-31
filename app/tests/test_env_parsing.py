"""Tests for the shared .env line parser.

Four entry points used to carry their own copy of the regex
`^\\s*([^#][^=]+?)\\s*=\\s*(.+?)\\s*$` — db.py, llm.py, mcp/auth.py and
streamlit_app.py. It is now one function, so it is worth pinning the behaviour
they all inherit: this is the code path that loads database credentials and API
keys, and a parser that quietly drops a line fails in a way that looks like a
missing secret rather than a parsing bug.
"""

import sys, os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import parse_env_line


@pytest.mark.parametrize(
    "line, expected",
    [
        ("KEY=value", ("KEY", "value")),
        ("  KEY = value  ", ("KEY", "value")),
        ("\tKEY\t=\tvalue\t", ("KEY", "value")),
        # Only the first = splits: passwords and URLs routinely contain more.
        ("PORTFOLIODB_PASSWORD=p@ss=w0rd", ("PORTFOLIODB_PASSWORD", "p@ss=w0rd")),
        ("URL=https://x.example/a?b=c", ("URL", "https://x.example/a?b=c")),
        ("KEY==double", ("KEY", "=double")),
        # A # only comments out a line when it starts one.
        ("a#b=c", ("a#b", "c")),
        ("KEY=va lue", ("KEY", "va lue")),
    ],
)
def test_parses_key_value_lines(line, expected):
    assert parse_env_line(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "# comment",
        "#KEY=value",
        "   # indented comment",
        "KEY",          # no separator
        "KEY=",         # empty value is treated as absent
        "=value",       # no key
        "   =   ",
    ],
)
def test_rejects_non_assignments(line):
    assert parse_env_line(line) is None


def test_single_character_key_is_accepted():
    """The one deliberate change from the regex this replaced.

    `[^#][^=]+?` required at least two characters, so the old parser silently
    dropped `K=v`. Nothing in this repo uses a one-letter key, but a .env
    parser that ignores a line without saying so is a bug, not a feature.
    """
    assert parse_env_line("K=v") == ("K", "v")


def test_value_keeps_internal_whitespace_but_loses_the_edges():
    assert parse_env_line("  K =  a  b  ") == ("K", "a  b")


def test_all_four_loaders_share_this_parser():
    """Guards the consolidation: a fifth copy of the regex should not appear."""
    import pathlib

    app_dir = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in app_dir.rglob("*.py"):
        if path.name == "db.py" or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "[^#][^=]" in text:
            offenders.append(str(path.relative_to(app_dir)))
    assert offenders == [], f"inline .env regex is back in: {offenders}"
