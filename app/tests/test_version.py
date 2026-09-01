"""Which build this is, for the dashboard footer and the MCP provenance block.

`release_version()` and `build_stamp()` answer different questions on purpose.
The first is what a person reads in the sidebar — a bare `1.1.5`, taken from
server.json so that release bumps stay in one place. The second is what you
need when a number looks wrong: the exact build, including the commit.

The git-reading cases below moved here with the code from
app/mcp/services/cutoff.py, where they were written. They are unchanged in
substance — only the fake module path differs, because version.py sits one
level below the repo root rather than three.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import version


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """Point the module at a scratch tree shaped like the repo."""
    fake_module = tmp_path / "app" / "version.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("", encoding="utf-8")
    monkeypatch.setattr(version, "_ROOT", tmp_path)
    monkeypatch.setattr(version, "_SERVER_JSON", tmp_path / "server.json")
    return tmp_path


# ───────────────────────────── release_version ─────────────────────────────

def test_release_version_reads_server_json(fake_root):
    (fake_root / "server.json").write_text(json.dumps({"version": "9.9.9"}), encoding="utf-8")
    assert version.release_version() == "9.9.9"


def test_release_version_is_bare_with_no_prefix_or_commit(fake_root):
    """What the sidebar shows: a release number, not a build stamp."""
    (fake_root / "server.json").write_text(json.dumps({"version": "1.1.5"}), encoding="utf-8")
    out = version.release_version()
    assert out == "1.1.5"
    assert not out.startswith("v")
    assert "@" not in out


@pytest.mark.parametrize(
    "contents",
    [
        None,                      # no file at all
        "{ not json",              # unparseable
        json.dumps({}),            # no version key
        json.dumps({"version": ""}),
    ],
    ids=["missing", "malformed", "no-key", "empty"],
)
def test_release_version_falls_back_to_the_build_stamp(fake_root, monkeypatch, contents):
    """An exact commit is a worse answer than a release, and a far better one
    than a blank footer."""
    if contents is not None:
        (fake_root / "server.json").write_text(contents, encoding="utf-8")
    monkeypatch.setenv("PORTFOLIODB_APP_VERSION", "v9.9.9-test")
    assert version.release_version() == "v9.9.9-test"


def test_the_real_server_json_is_readable():
    """Guards the wiring, not the number — a rename or a bad COPY shows here."""
    out = version.release_version()
    assert out and out != "unknown"


# ─────────────────────────────── build_stamp ───────────────────────────────

def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("PORTFOLIODB_APP_VERSION", "v9.9.9-test")
    assert version.build_stamp() == "v9.9.9-test"


def test_falls_back_to_unknown_without_env_or_git(monkeypatch):
    """A deployed tree with no .git reports honestly rather than failing."""
    monkeypatch.delenv("PORTFOLIODB_APP_VERSION", raising=False)
    monkeypatch.setattr(version, "git_head_sha", lambda: None)
    assert version.build_stamp() == "unknown"


def test_always_returns_something(monkeypatch):
    monkeypatch.delenv("PORTFOLIODB_APP_VERSION", raising=False)
    out = version.build_stamp()
    assert isinstance(out, str) and out


# ─────────────────────────────── git_head_sha ───────────────────────────────

class TestGitHeadSha:
    """Read the commit from .git directly — no subprocess on a request path."""

    def test_reads_a_symbolic_ref(self, fake_root):
        git_dir = fake_root / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        ref = git_dir / "refs" / "heads"
        ref.mkdir(parents=True)
        (ref / "main").write_text("abcdef1234567890\n", encoding="utf-8")

        assert version.git_head_sha() == "abcdef1"

    def test_reads_a_detached_head(self, fake_root):
        git_dir = fake_root / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("abcdef1234567890\n", encoding="utf-8")

        assert version.git_head_sha() == "abcdef1"

    def test_falls_back_to_packed_refs(self, fake_root):
        """`git gc` moves refs into packed-refs and deletes the loose file."""
        git_dir = fake_root / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            "1111111111111111 refs/heads/other\n"
            "9999999999999999 refs/heads/main\n"
            "^aaaaaaaaaaaaaaaa\n",
            encoding="utf-8",
        )

        assert version.git_head_sha() == "9999999"

    def test_returns_none_without_a_git_directory(self, fake_root):
        assert version.git_head_sha() is None

    def test_returns_none_when_the_ref_cannot_be_resolved(self, fake_root):
        git_dir = fake_root / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/gone\n", encoding="utf-8")
        assert version.git_head_sha() is None
