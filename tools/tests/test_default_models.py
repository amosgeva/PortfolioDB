"""The default LLM model IDs carry a retirement watch, and it works.

app/llm.py's `_DEFAULT_MODEL` is what an install uses until LLM_MODEL is set,
and a vendor retires model IDs on its own schedule (re-score, section 12: the
OpenAI default was already listed as deprecated). `DEFAULT_MODEL_REVIEW` gives
each default a review-by date and a public listing, and
tools/check_default_models.py fails the weekly security scan when a date passes
or a listing drops (or schedules the expiry of) an ID.

As with .trivyignore, the real registry falling due is deliberately *not* a
failure here: these tests run on every pull request, and a vendor's calendar
must not redden an unrelated change. The checker is exercised with an explicit
`--today` and stub listings instead; no test touches the network.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import check_default_models as checker  # noqa: E402

DEFAULTS, REVIEW = checker.load()


def test_every_default_has_exactly_one_review_entry_for_that_id():
    wanted = {p: m for p, m in DEFAULTS.items() if m}
    assert wanted, "app/llm.py has no default models left to watch"
    assert set(REVIEW) == set(wanted), (
        f"DEFAULT_MODEL_REVIEW covers {sorted(REVIEW)}, the defaults are {sorted(wanted)}"
    )
    for provider, model in wanted.items():
        assert REVIEW[provider]["model"] == model, (
            f"{provider}: change _DEFAULT_MODEL and its DEFAULT_MODEL_REVIEW entry together"
        )


@pytest.mark.parametrize("provider", sorted(REVIEW))
def test_every_entry_is_dated_sourced_and_checkable(provider):
    entry = REVIEW[provider]
    reviewed, due = date.fromisoformat(entry["reviewed"]), date.fromisoformat(entry["review_by"])
    assert reviewed < due <= reviewed + timedelta(days=180), f"{provider}: review_by within 180 days"
    assert entry["source"].startswith("https://"), f"{provider}: link the catalogue it was checked against"
    kind, ident = entry["listing"]
    assert kind in ("openrouter", "ollama"), f"{provider}: no public listing can check {kind!r}"
    assert ident


def test_the_registry_in_app_llm_is_the_one_the_app_imports(monkeypatch):
    """The checker reads the file with ast; the module must agree with it."""
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    import llm

    assert llm._DEFAULT_MODEL == DEFAULTS
    assert llm.DEFAULT_MODEL_REVIEW == REVIEW


def test_checker_passes_the_real_registry_on_the_day_it_was_reviewed():
    assert checker.offline_problems(DEFAULTS, REVIEW, date(2026, 11, 15)) == []


# ── the offline half: drift and dates ────────────────────────────────────────

_ENTRY = {"model": "m-1", "reviewed": "2026-11-15", "review_by": "2027-02-15",
          "source": "https://example.invalid", "listing": ("openrouter", "v/m-1")}


@pytest.mark.parametrize("today,expected", [("2027-02-14", 0), ("2027-02-15", 0), ("2027-02-16", 1)])
def test_a_passed_review_date_fails(today, expected):
    found = checker.offline_problems({"p": "m-1"}, {"p": _ENTRY}, date.fromisoformat(today))
    assert len(found) == expected, found


def test_a_changed_default_without_its_entry_fails():
    found = checker.offline_problems({"p": "m-2"}, {"p": _ENTRY}, date(2026, 11, 15))
    assert any("'m-2' but the review entry is for 'm-1'" in f for f in found)


def test_a_default_with_no_entry_and_an_orphan_entry_both_fail():
    found = checker.offline_problems({"p": "m-1", "q": "x"}, {"p": _ENTRY, "gone": _ENTRY}, date(2026, 11, 15))
    assert any(f.startswith("q:") and "no DEFAULT_MODEL_REVIEW entry" in f for f in found)
    assert any(f.startswith("gone:") for f in found)


def test_an_empty_default_needs_no_entry():
    assert checker.offline_problems({"p": "m-1", "custom": ""}, {"p": _ENTRY}, date(2026, 11, 15)) == []


def test_a_review_interval_over_180_days_fails():
    entry = {**_ENTRY, "review_by": "2027-06-01"}
    assert checker.offline_problems({"p": "m-1"}, {"p": entry}, date(2026, 11, 15))


# ── the online half, against stub listings ───────────────────────────────────


def _online(catalogue: dict, ollama_has: set[str] = frozenset(), review=None, today="2026-11-15"):
    review = review or {"p": _ENTRY}
    return checker.online_problems(
        review, date.fromisoformat(today),
        openrouter=lambda: catalogue, ollama=lambda name: name in ollama_has,
    )


def test_a_listed_model_with_no_expiry_passes():
    assert _online({"v/m-1": {"id": "v/m-1", "expiration_date": None}}) == []


def test_a_model_openrouter_dropped_fails():
    assert any("no longer lists 'v/m-1'" in f for f in _online({}))


@pytest.mark.parametrize("expires,fails", [("2026-12-11", True), ("2026-12-30", True), ("2027-03-01", False)])
def test_a_scheduled_expiry_inside_the_horizon_fails(expires, fails):
    """2026-12-11 is the date OpenRouter listed for openai/gpt-5 on 2026-11-15."""
    found = _online({"v/m-1": {"id": "v/m-1", "expiration_date": expires}})
    assert bool(found) is fails, found


def test_an_ollama_model_missing_from_the_registry_fails():
    review = {"ollama": {**_ENTRY, "listing": ("ollama", "llama3.3")}}
    assert _online({}, {"llama3.3"}, review) == []
    assert any("has no 'llama3.3'" in f for f in _online({}, set(), review))


def test_the_openrouter_listing_is_fetched_once_for_all_entries():
    calls = []

    def fetch():
        calls.append(1)
        return {"v/m-1": {"id": "v/m-1"}}

    review = {"a": _ENTRY, "b": _ENTRY}
    checker.online_problems(review, date(2026, 11, 15), openrouter=fetch, ollama=lambda _: True)
    assert len(calls) == 1


def test_main_reports_a_listing_it_cannot_read_as_a_failure(monkeypatch, capsys):
    def boom():
        raise OSError("network unreachable")

    monkeypatch.setattr(checker, "fetch_openrouter", boom)
    monkeypatch.setattr(checker, "fetch_ollama", lambda _name: True)
    assert checker.main(["--online", "--today", "2026-11-15"]) == 1
    assert "could not read a public model listing" in capsys.readouterr().out


# ── the schedule that runs it ────────────────────────────────────────────────


def test_the_weekly_scan_runs_the_online_check_and_tracks_its_failure():
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "security-scan.yml").read_text(encoding="utf-8"))
    on = doc.get("on", doc.get(True))
    assert on.get("schedule"), "the security scan lost its schedule"
    job = doc["jobs"]["llm-models"]
    run = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "tools/check_default_models.py --online" in run
    assert "pull_request" in job.get("if", ""), "a vendor's calendar must not redden pull requests"
    assert "llm-models" in doc["jobs"]["track"]["needs"], "a red check must open the tracking issue"


def test_the_provider_doc_describes_the_check():
    text = (ROOT / "docs" / "llm-providers.md").read_text(encoding="utf-8")
    assert "check_default_models.py" in text
    assert "DEFAULT_MODEL_REVIEW" in text
