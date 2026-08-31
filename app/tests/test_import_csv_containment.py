"""Path containment for the CSV history importer.

`--dir` and `--pattern` are both caller-supplied and used to be joined straight
into a glob, so `--pattern '../../*.csv'` read files outside the directory the
operator named. This pins the containment that replaced it.

The threat here is modest — a local CLI run by the person who owns the files —
but the failure is silent: the importer would have read whatever it matched and
written it into the ledger, and nothing in the output would have said the data
came from somewhere unintended.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from import_csv_history import contained_matches


@pytest.fixture
def tree(tmp_path):
    """A --dir with two CSVs, plus decoys one and two levels above it."""
    inside = tmp_path / "nested" / "data"
    inside.mkdir(parents=True)
    (inside / "trades.csv").write_text("x", encoding="utf-8")
    (inside / "more.csv").write_text("x", encoding="utf-8")
    (tmp_path / "nested" / "SIBLING.csv").write_text("secret", encoding="utf-8")
    (tmp_path / "OUTSIDE.csv").write_text("secret", encoding="utf-8")
    return inside


def _names(paths):
    return sorted(os.path.basename(p) for p in paths)


def test_normal_pattern_finds_the_directory_contents(tree):
    files, escaped = contained_matches(tree.resolve(), "*.csv")
    assert _names(files) == ["more.csv", "trades.csv"]
    assert escaped == 0


@pytest.mark.parametrize(
    "pattern",
    ["../*.csv", "../../*.csv", "../**/*.csv", r"..\*.csv", "a/../../*.csv"],
)
def test_traversal_patterns_are_refused_before_the_glob(tree, pattern):
    """Rejected up front, not filtered afterwards.

    Filtering the results was the first attempt and it is not equivalent: glob
    still walks `../..` to build the match list, so the process reads
    directories the operator never named even though nothing outside comes
    back. Sonar's S8707 asks for validation *before* the filesystem access, and
    on that it is right.

    Backslashes are checked too: they are a separator on Windows, and on Linux
    a literal backslash-dot-dot filename should not be quietly accepted either.
    """
    with pytest.raises(ValueError, match="must stay inside"):
        contained_matches(tree.resolve(), pattern)


def test_absolute_patterns_are_refused(tree, tmp_path):
    with pytest.raises(ValueError, match="must stay inside"):
        contained_matches(tree.resolve(), str(tmp_path / "*.csv"))


def test_a_sibling_directory_is_not_reachable(tree):
    files, _ = contained_matches(tree.resolve(), "*.csv")
    assert "SIBLING.csv" not in _names(files)
    assert "OUTSIDE.csv" not in _names(files)


def test_directories_matching_the_pattern_are_skipped(tree):
    (tree / "notafile.csv").mkdir()
    files, _ = contained_matches(tree.resolve(), "*.csv")
    assert "notafile.csv" not in _names(files)


def test_subdirectories_are_still_reachable_by_pattern(tree):
    """Containment must not break a legitimate recursive glob."""
    sub = tree / "2026"
    sub.mkdir()
    (sub / "jan.csv").write_text("x", encoding="utf-8")
    files, escaped = contained_matches(tree.resolve(), "**/*.csv")
    assert "jan.csv" in _names(files)
    assert escaped == 0


@pytest.mark.skipif(
    not hasattr(os, "symlink"), reason="platform has no symlink support"
)
def test_symlink_out_of_the_tree_is_excluded(tree, tmp_path):
    """.resolve() collapses the link, so the target's real location decides."""
    target = tmp_path / "OUTSIDE.csv"
    try:
        (tree / "link.csv").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")
    files, escaped = contained_matches(tree.resolve(), "*.csv")
    assert "link.csv" not in _names(files)
    assert "OUTSIDE.csv" not in _names(files)
    assert escaped == 1


def test_missing_directory_matches_nothing(tmp_path):
    files, escaped = contained_matches((tmp_path / "nope").resolve(), "*.csv")
    assert files == []
    assert escaped == 0
