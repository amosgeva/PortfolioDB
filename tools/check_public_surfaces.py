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
import html.parser
import re
import sys
import urllib.error
import urllib.parse
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

# "No telemetry" is on the page, on the social card, and in the repo
# description. It is the claim this audience checks first and the one it is
# least forgiving about, because checking costs ten seconds of devtools.
#
# It is also the claim we were worst at verifying. Three of us cleared it
# independently — empty console, no vendor in 591 KB of built chunks, repeated
# curls of the live page — and all three were structurally incapable of finding
# what was there. The console stays silent because a beacon is a Network event.
# The chunks are clean because Cloudflare injects *after* our build, at the
# edge. The curls were clean because they did not send `Accept: text/html`.
#
# So this does not look for one vendor. Any third-party script origin on a page
# claiming no telemetry is a finding, whether we put it there or a dashboard
# toggle did. The site is self-hosted fonts and its own bundle; it has no
# legitimate reason to load executable code from anywhere else.
NO_TELEMETRY = re.compile(r"no telemetry", re.I)

# Matched against the parsed **host**, never as a substring of the whole URL.
# The first version of this check tested `"portfoliodb.app" in src` alongside
# path fragments, which cleared far more than it meant to:
#
#   https://cdn.some-vendor.com/static/analytics.js   -> contains "/static/"
#   https://portfoliodb.app.evil.test/t.js            -> contains "portfoliodb.app"
#
# Both would have passed as first-party. This week's beacon only tripped it by
# luck — `https://static.cloudflareinsights.com/` has `//static.`, not
# `/static/` — and `/static/` is one of the most common CDN paths there is. A
# vendor one path segment different would have been waved through.
FIRST_PARTY_HOSTS = ("portfoliodb.app", "www.portfoliodb.app")

# Path prefixes belong to *relative* sources only, where there is no host to
# parse and the path is genuinely ours. Applying them to absolute URLs is what
# created the hole above.
FIRST_PARTY_PATHS = ("/_next/", "/static/")


class ScriptCollector(html.parser.HTMLParser):
    """Collect every `<script src=...>` the page declares.

    A real parser rather than a regex, because all four blind spots found in
    this guard in one afternoon were in how it *located* the thing to judge,
    never in the judging:

      1. fetched with `Accept: */*`  -> asserted on a page no browser receives
      2. only absolute off-site URLs -> missed same-origin `/cdn-cgi/`
      3. substring-matched paths as hosts -> cleared any vendor under `/static/`
      4. required quoted attributes  -> could not see `<script src=https://...>`

    (4) is why this is a parser now. Unquoted attribute values are valid HTML5,
    and the risk is not that an injector omits quotes deliberately — it is that
    **any minifier legally strips quotes** around values with no spaces. If that
    is ever switched on, at build time or by an edge toggle, a regex requiring
    quotes stops seeing *every* script on the page at once and keeps exiting 0
    while blind. Today's incident was a dashboard toggle changing what the edge
    serves with no commit; a toggle that reformats attributes is the same event.

    `HTMLParser` handles unquoted values, newlines inside the tag, uppercase
    tags and attributes, and duplicate attributes, without a fifth patch.

    **The boundary, stated rather than patched toward.** This asserts on markup
    as served. It cannot see a script the page builds at runtime —
    `document.write('<script src=...>')` executes and is invisible here, because
    `HTMLParser` treats everything inside `<script>` as CDATA.

    That is not a shape nobody guessed; it is the edge of what a static fetch
    can do, and no version of this file could cross it. Chasing it means
    scanning script bodies for markup, which is the regex that was just removed,
    and it would reintroduce false positives the parser fixed for free: a script
    tag inside an HTML comment is markup, not execution, and is correctly
    ignored now where a regex flagged it.

    A known limit written down beats a check contorted to cover it. If runtime
    injection ever needs covering, it needs a headless browser, not a wider
    pattern here.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        for name, value in attrs:
            if name.lower() == "src" and value and value.strip():
                self.sources.append(value.strip())


def script_sources(markup: str) -> list[str]:
    collector = ScriptCollector()
    try:
        collector.feed(markup)
    except Exception:  # a parse error must not read as "no scripts found"
        raise SystemExit("::error::could not parse the page HTML — guard cannot assert anything")
    return collector.sources

# `/cdn-cgi/` is Cloudflare's reserved path. Nothing our build produces is
# served from it, so a script there is edge-injected by definition: Rocket
# Loader, Email Obfuscation and Bot Fight Mode all land here, and all three are
# dashboard toggles rather than commits.
#
# It is checked separately because it is **same-origin**, and the third-party
# check below cannot see it — that check keys on an absolute off-site URL, which
# is the one shape this week's beacon happened to take. Catching only the shape
# that already bit us is how the previous version of this guard passed for a
# day. Not currently triggered; added while the reason is still legible.
EDGE_INJECTED = "/cdn-cgi/"


def injected_scripts(markup: str) -> list[str]:
    """Scripts the page executes that our build did not put there.

    Two shapes, because they fail differently: an absolute URL to someone
    else's origin, and a same-origin `/cdn-cgi/` path. Both arrive through a
    vendor toggle rather than a merge, which is why neither is visible to any
    check that reads the repo.
    """
    found = []
    for src in script_sources(markup):
        parts = urllib.parse.urlsplit(src)
        if parts.scheme or parts.netloc:
            # Absolute (or protocol-relative). Judge it on the host alone.
            host = parts.netloc.split("@")[-1].split(":")[0].lower()
            if host not in FIRST_PARTY_HOSTS:
                found.append(host or src)
        elif EDGE_INJECTED in parts.path:
            found.append(f"{parts.path} (Cloudflare edge injection, same-origin)")
        elif not parts.path.startswith(FIRST_PARTY_PATHS):
            found.append(f"{parts.path} (unrecognised same-origin script path)")
    return sorted(dict.fromkeys(found))


def rendered_text(markup: str) -> str:
    """HTML to the text a reader actually sees, so markup cannot hide a claim."""
    without_code = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    detagged = re.sub(r"<[^>]+>", " ", without_code)
    return re.sub(r"\s+", " ", html.unescape(detagged))


def fetch(url: str) -> str:
    # `Accept: text/html` is load-bearing, not politeness. Cloudflare injects
    # edge-side HTML — the Web Analytics RUM beacon among it — only when the
    # request accepts HTML. It keys on this header, not on User-Agent: measured
    # 2026-08-15 against the live site, varying one header at a time.
    #
    #   UA alone                    -> 0 matches
    #   UA + Accept: text/html      -> 1
    #   UA + Sec-Fetch-Mode         -> 0
    #   Accept: text/html, no UA    -> 1
    #
    # urllib defaults to `Accept: */*`, so this guard spent its whole life
    # fetching a document no browser is ever served. It reported the site clean
    # for a day while a third-party analytics script was live on it, and so did
    # every hand-run `curl -A "Mozilla/5.0"`. A guard that asserts on a variant
    # of the page nobody receives is worse than no guard, because it is believed.
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "portfoliodb-drift-guard",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
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

    # Reported whether or not the page says "no telemetry", because a third
    # party executing on this site is worth knowing about either way — but the
    # message names the contradiction when the claim is present, since that is
    # what turns a dependency into a credibility problem.
    for origin in injected_scripts(markup):
        claim = (
            'site says "no telemetry" and '
            if NO_TELEMETRY.search(text)
            else "site "
        )
        failures.append(
            f"{claim}executes a script our build did not produce: {origin}. This is served to "
            "browsers (Accept: text/html) and is invisible to a source grep, because it is "
            "injected at the edge after the build. Disable it at the dashboard, or change the copy."
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
