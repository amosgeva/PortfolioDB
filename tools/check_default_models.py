"""Fail when a default LLM model ID is due for review or is being retired.

app/llm.py's `_DEFAULT_MODEL` is what an install uses until someone sets
LLM_MODEL, and `DEFAULT_MODEL_REVIEW` records, for each non-empty default, when
it was last checked, a review-by date, and the public listing that can check it.
This script reads both with `ast` (it never imports the app) and reports:

  * a default with no review entry, or an entry for a different ID;
  * a review-by date that has passed;
  * with --online, an ID a public listing no longer has, or lists with an
    expiration date within --horizon-days (default 45).

The public listings need no credentials: OpenRouter's model list, which mirrors
the Anthropic and OpenAI catalogues and publishes `expiration_date` for a model
being withdrawn, and the Ollama registry's manifest for a library model.
Anthropic's and OpenAI's own model lists require an API key and are not used,
which is why every entry also carries a date that fails on its own.

    python3 tools/check_default_models.py [--online] [--today YYYY-MM-DD]

security-scan.yml runs it with --online every week; a failure opens the same
tracking issue as a failed scan. `--today` and the injectable fetchers exist for
tools/tests/test_default_models.py. Standard library only.
"""

from __future__ import annotations

import argparse
import ast
import json
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
LLM_PY = ROOT / "app" / "llm.py"
MAX_REVIEW_INTERVAL = timedelta(days=180)
DEFAULT_HORIZON_DAYS = 45

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OLLAMA_MANIFEST_URL = "https://registry.ollama.ai/v2/library/{name}/manifests/{tag}"
_UA = "portfoliodb-default-model-check"


def load(path: Path = LLM_PY) -> tuple[dict, dict]:
    """(_DEFAULT_MODEL, DEFAULT_MODEL_REVIEW) as literals from app/llm.py."""
    found: dict[str, object] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("_DEFAULT_MODEL", "DEFAULT_MODEL_REVIEW"):
                    found[target.id] = ast.literal_eval(node.value)
    missing = {"_DEFAULT_MODEL", "DEFAULT_MODEL_REVIEW"} - set(found)
    if missing:
        raise SystemExit(f"{path}: no literal {', '.join(sorted(missing))}")
    return found["_DEFAULT_MODEL"], found["DEFAULT_MODEL_REVIEW"]  # type: ignore[return-value]


def offline_problems(defaults: dict, review: dict, today: date) -> list[str]:
    """Registry drift and passed review dates; needs no network."""
    found = []
    wanted = {p: m for p, m in defaults.items() if m}
    for provider in sorted(set(wanted) | set(review)):
        entry = review.get(provider)
        if provider not in wanted:
            found.append(f"{provider}: review entry for a provider with no default model")
            continue
        if entry is None:
            found.append(f"{provider}: default {wanted[provider]!r} has no DEFAULT_MODEL_REVIEW entry")
            continue
        if entry.get("model") != wanted[provider]:
            found.append(
                f"{provider}: default is {wanted[provider]!r} but the review entry is for {entry.get('model')!r}"
            )
        try:
            reviewed = date.fromisoformat(entry["reviewed"])
            due = date.fromisoformat(entry["review_by"])
        except (KeyError, TypeError, ValueError):
            found.append(f"{provider}: needs ISO `reviewed` and `review_by` dates")
            continue
        if not reviewed < due or due - reviewed > MAX_REVIEW_INTERVAL:
            found.append(f"{provider}: review_by must be after reviewed and at most 180 days later")
        if due < today:
            found.append(
                f"{provider}: {wanted[provider]!r} was due for review on {due.isoformat()} "
                f"(check {entry.get('source', 'the vendor catalogue')})"
            )
    return found


# ── public listings ──────────────────────────────────────────────────────────


def fetch_openrouter() -> dict[str, dict]:
    """OpenRouter's model list, keyed by id. Public; no key."""
    req = urllib.request.Request(OPENROUTER_MODELS_URL, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https URL)
        data = json.load(resp)
    return {m["id"]: m for m in data.get("data", []) if isinstance(m, dict) and "id" in m}


def fetch_ollama(model: str) -> bool:
    """Whether the Ollama registry has a manifest for `name[:tag]`. Public; no key."""
    name, _, tag = model.partition(":")
    url = OLLAMA_MANIFEST_URL.format(name=name, tag=tag or "latest")
    req = urllib.request.Request(
        url, method="HEAD",
        headers={"User-Agent": _UA, "Accept": "application/vnd.docker.distribution.manifest.v2+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https host)
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def online_problems(
    review: dict,
    today: date,
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    openrouter: Callable[[], dict[str, dict]] = fetch_openrouter,
    ollama: Callable[[str], bool] = fetch_ollama,
) -> list[str]:
    """Each entry's `listing` checked against its public catalogue."""
    found = []
    horizon = today + timedelta(days=horizon_days)
    catalogue: dict[str, dict] | None = None
    for provider, entry in sorted(review.items()):
        kind, ident = entry.get("listing") or (None, None)
        if kind == "openrouter":
            if catalogue is None:
                catalogue = openrouter()
            model = catalogue.get(ident)
            if model is None:
                found.append(f"{provider}: OpenRouter no longer lists {ident!r}")
                continue
            expires = model.get("expiration_date")
            if expires:
                try:
                    when = date.fromisoformat(str(expires)[:10])
                except ValueError:
                    found.append(f"{provider}: OpenRouter lists {ident!r} with an unreadable expiration {expires!r}")
                    continue
                if when <= horizon:
                    found.append(
                        f"{provider}: OpenRouter lists {ident!r} as expiring on {when.isoformat()}; "
                        "choose a successor default in app/llm.py"
                    )
        elif kind == "ollama":
            if not ollama(ident):
                found.append(f"{provider}: the Ollama registry has no {ident!r}")
        else:
            found.append(f"{provider}: no public listing to check (set `listing`)")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--today", type=date.fromisoformat, default=None)
    ap.add_argument("--online", action="store_true", help="also check the public model listings")
    ap.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    args = ap.parse_args(argv)
    today = args.today or datetime.now(timezone.utc).date()

    defaults, review = load()
    found = offline_problems(defaults, review, today)
    if args.online:
        try:
            found += online_problems(
                review, today, horizon_days=args.horizon_days,
                openrouter=fetch_openrouter, ollama=fetch_ollama,
            )
        except (OSError, ValueError) as exc:  # URLError is an OSError; bad JSON a ValueError
            found.append(f"could not read a public model listing: {exc}")
    for p in found:
        print(f"::error file=app/llm.py::{p}")
    if found:
        print("Re-check each default against its vendor catalogue (docs/llm-providers.md), "
              "then update _DEFAULT_MODEL and DEFAULT_MODEL_REVIEW together.")
        return 1
    scope = "review dates and public listings" if args.online else "review dates"
    print(f"every default model is within its {scope} ({today.isoformat()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
