"""Where the investor one-pager comes from, and how staleness is avoided.

Precedence is database → mounted file → nothing, matching every other setting.
The template is deliberately treated as *absent*: an unedited template loads
fine, so without this the UI would report a loaded philosophy and the advisor
would reason about bracketed placeholders.
"""

from __future__ import annotations

import time

import pytest

import advisor
import settings


@pytest.fixture
def fake_db(monkeypatch):
    def seed(values: dict[str, str]):
        monkeypatch.setattr(settings, "_cache", dict(values))
        monkeypatch.setattr(settings, "_db_ok", True)
        monkeypatch.setattr(settings, "_cache_at", time.monotonic())
    return seed


@pytest.fixture
def phil_file(tmp_path, monkeypatch):
    """Point PHILOSOPHY_PATH at a temp file (or a directory, or nothing)."""
    def place(content: str | None, as_directory: bool = False):
        target = tmp_path / "philosophy.md"
        if as_directory:
            target.mkdir()
        elif content is not None:
            target.write_text(content, encoding="utf-8")
        monkeypatch.setattr(advisor, "PHILOSOPHY_PATH", target)
        return target
    return place


REAL = "# Investor One-Pager — Priya\n\n## North Star\nCompound then preserve."
TEMPLATE = "# Investor One-Pager — [Your Name]\n\n> **STATUS: TEMPLATE.** Fill me in."


class TestPrecedence:
    def test_database_wins_over_file(self, fake_db, phil_file):
        phil_file("# from the file\n\n## North Star\nfile version")
        fake_db({advisor.PHILOSOPHY_KEY: REAL})
        assert "Priya" in advisor.load_philosophy()
        assert advisor.philosophy_source() == "database"

    def test_file_used_when_nothing_saved(self, fake_db, phil_file):
        phil_file(REAL)
        fake_db({})
        assert "Priya" in advisor.load_philosophy()
        assert advisor.philosophy_source() == "file"

    def test_nothing_anywhere(self, fake_db, phil_file):
        phil_file(None)
        fake_db({})
        assert advisor.philosophy_source() == "none"
        assert "has not written one" in advisor.load_philosophy()

    def test_blank_saved_value_falls_through_to_file(self, fake_db, phil_file):
        phil_file(REAL)
        fake_db({advisor.PHILOSOPHY_KEY: "   \n  "})
        assert advisor.philosophy_source() == "file"
        assert "Priya" in advisor.load_philosophy()


class TestTemplateDetection:
    def test_template_in_database_is_flagged(self, fake_db, phil_file):
        phil_file(None)
        fake_db({advisor.PHILOSOPHY_KEY: TEMPLATE})
        assert advisor.philosophy_source() == "template"
        out = advisor.load_philosophy()
        assert "still the unedited template" in out
        # The text is still handed over, so the model can see the structure.
        assert "[Your Name]" in out

    def test_template_in_file_is_flagged(self, fake_db, phil_file):
        phil_file(TEMPLATE)
        fake_db({})
        assert advisor.philosophy_source() == "template"
        assert "still the unedited template" in advisor.load_philosophy()

    def test_filled_document_is_not_flagged(self, fake_db, phil_file):
        phil_file(None)
        fake_db({advisor.PHILOSOPHY_KEY: REAL})
        assert "unedited template" not in advisor.load_philosophy()


class TestDockerDirectoryTrap:
    def test_a_directory_at_the_mount_point_is_not_a_philosophy(self, fake_db, phil_file):
        """Compose creates a *directory* when the host file is missing."""
        phil_file(None, as_directory=True)
        fake_db({})
        assert advisor.philosophy_source() == "none"
        assert "has not written one" in advisor.load_philosophy()


class TestSaving:
    def test_save_then_clear(self, fake_db, phil_file, monkeypatch):
        phil_file(None)
        fake_db({})
        stored: dict[str, str] = {}
        monkeypatch.setattr(settings, "set_value", lambda k, v: stored.__setitem__(k, v))
        monkeypatch.setattr(settings, "unset", lambda k: stored.pop(k, None))

        advisor.save_philosophy("  " + REAL + "  ")
        assert stored[advisor.PHILOSOPHY_KEY] == REAL      # trimmed

        advisor.save_philosophy("   ")
        assert advisor.PHILOSOPHY_KEY not in stored        # blank clears it
