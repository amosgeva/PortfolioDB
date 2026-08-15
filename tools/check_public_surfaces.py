"""Assert the marketing site does not contradict this repo.

    python tools/check_public_surfaces.py [URL]

CI already guards `README.md` and `docs/*.md` against hand-maintained counts and
hand-written patch pins, because both went stale three times in three days. That
guard stops at the repo boundary. `portfoliodb.app` is a separate project, and
within a day of going live it published "494 tests" — a number that appears in no
tracked file and sits *below* the measured floor (276 + 279 = 555). The surface a
stranger reads first was the one surface nothing checked.

We cannot grep a source we do not have. We do not need to: **assert on the
published artefact instead.** Fetching the live URL checks what strangers
actually see, which is strictly better than checking a source we would then have
to keep in sync with the deploy.

Two things make this work rather than merely look like it works:

* **Strip tags before matching.** Today the site renders `<strong>494 tests</strong>`,
  so even a naive grep over raw HTML happens to match. That is luck. Write it as
  `<strong>494</strong> tests` and the same grep silently passes while the claim
  is still on the page — a guard that reports green for a defect it cannot see is
  worse than no guard. We match against rendered text, so markup cannot hide a
  claim. `<script>` blocks are dropped first: Next.js inlines the whole RSC
  payload, which repeats every string and would otherwise double every hit.

* **This never runs on a pull request.** It is scheduled. The site is a network
  resource on someone else's deploy cadence, and a Cloudflare blip must not
  redden a build for a code change that had nothing to do with it. A red run here
  means the *site* drifted, and the fix is on the site.

Exit 0 clean, 1 on drift, 2 if the site could not be fetched (an outage is not a
drift finding and should not be reported as one).
"""

from __future__ import annotations

import html
import re
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://portfoliodb.app/"
TIMEOUT = 30

# The rule is not "no numbers" — it is "no *precise* count of something that
# grows". A stated floor stays true through the next merge, so "500+ tests" and
# "over 500 tests" pass and "494 tests" fails. Both floor forms are spelled out
# rather than left to luck: `500+` would slip past a bare `\d+\s+tests` pattern
# only because the `+` happens to break it, and "over 500 tests" would be failed
# by that same pattern for no reason. A guard that cries wolf gets switched off.
#
# Two digits or more, so "3 tests" inside a worked example is not swept up.
TEST_COUNT = re.compile(
    r"(?P<floor>\b(?:over|more than|at least|upwards of|north of|nearly|almost)\s+)?"
    r"\b(?P<count>\d{2,})(?P<plus>\+)?\s+tests?\b",
    re.I,
)

# Same failure as the counts, one release later: `:1.0.1` sat in the README as
# the "pin harder" example while 1.0.3 shipped the reviewed disclaimer. The docs
# name the floating `:1`; a patch pin on the site would rot the same way.
#
# The minor component rots too, and slower, which is worse: `:1.0` outlived two
# minor releases in compose before anyone noticed, because nothing was *wrong* on
# the page — the install just quietly delivered a line behind the feature list.
# So this rejects `:1.0` as well as `:1.0.3`, and only the bare major passes.
PATCH_PIN = re.compile(r"portfoliodb:\d+\.\d+(?:\.\d+)?")

# The launch plan is "post a link". A link with no card previews as grey text in
# every feed it lands in, so the card is not cosmetic — it is the first frame of
# the funnel. Guarded permanently so it cannot silently disappear on a redeploy.
OG_IMAGE = re.compile(r"""<meta[^>]+(?:property|name)=["'](?:og:image|twitter:image)["']""", re.I)

# `summary` renders a small square thumbnail; `summary_large_image` renders the
# card. Pinned separately from the image because nothing in the site's source
# sets it — Next infers it from the presence of `twitter-image.png`, so it is
# one file rename away from silently reverting to a thumbnail with the card
# still sitting in the repo, present and unused.
LARGE_CARD = re.compile(
    r"""<meta[^>]+name=["']twitter:card["'][^>]+content=["']summary_large_image["']""", re.I
)

# The site sells the MCP server as its differentiator and hands the reader an
# install that never names the variable that server refuses to start without
# (app/mcp/auth.py raises on an empty token). The highest-intent visitor is the
# one who dead-ends. If the site names the feature, it must name the variable.
MCP_FEATURE = re.compile(r"\bMCP\b")
MCP_TOKEN = re.compile(r"PORTFOLIODB_MCP_TOKEN")


def rendered_text(markup: str) -> str:
    """HTML to the text a reader actually sees, so markup cannot hide a claim."""
    without_code = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    detagged = re.sub(r"<[^>]+>", " ", without_code)
    return re.sub(r"\s+", " ", html.unescape(detagged))


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "portfoliodb-drift-guard"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def check(markup: str) -> list[str]:
    text = rendered_text(markup)
    failures = []

    precise = {
        m.group(0).strip()
        for m in TEST_COUNT.finditer(text)
        if not m.group("floor") and not m.group("plus")
    }
    for claim in sorted(precise):
        failures.append(
            f'site says "{claim}" — a precise count of something that grows. '
            'Say "500+ tests across two suites" (a floor, which stays true through the next merge).'
        )

    for match in dict.fromkeys(PATCH_PIN.findall(text) + PATCH_PIN.findall(markup)):
        failures.append(f'site pins "{match}" — name the floating `:1` and link the releases page.')

    if not OG_IMAGE.search(markup):
        failures.append(
            "site has no og:image/twitter:image — a posted link previews as grey text in every feed."
        )
    elif not LARGE_CARD.search(markup):
        failures.append(
            "site has a card image but twitter:card is not summary_large_image — it previews as a "
            "small square thumbnail instead of the card."
        )

    if MCP_FEATURE.search(text) and not MCP_TOKEN.search(text):
        failures.append(
            "site sells MCP but never names PORTFOLIODB_MCP_TOKEN — its install cannot reach the "
            "feature it advertises (the server raises on an empty token)."
        )

    return failures


def main(argv: list[str]) -> int:
    url = argv[1] if len(argv) > 1 else DEFAULT_URL
    try:
        markup = fetch(url)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"::warning::could not fetch {url} ({exc}) — not a drift finding")
        return 2

    failures = check(markup)
    for failure in failures:
        print(f"::error::{failure}")
    if failures:
        print(f"\n{len(failures)} drift finding(s) against {url}")
        return 1

    print(f"{url}: no drift against the repo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
