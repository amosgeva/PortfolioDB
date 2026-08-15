"""The drift guard's own adversarial set, written down.

`tools/check_public_surfaces.py` took six commits on 2026-08-15. Every one of
them fixed a way the guard *located* the thing to judge rather than a way it
judged, and every one was validated by cases that lived only in a chat log:

  1. fetched with `Accept: */*`        -> asserted on a page no browser receives
  2. only absolute off-site URLs       -> missed same-origin `/cdn-cgi/`
  3. substring-matched paths as hosts  -> cleared any vendor under `/static/`
  4. required quoted attributes        -> could not see `<script src=https://...>`
  5. compared every surface to a constant host list -> flagged a preview
     deploy's own bundle as third-party
  6. (the boundary, stated rather than patched toward)

The guard exists because humans forget. Its correctness was being held in place
by humans remembering. This file is the fix for that, and it is the *only* thing
in this file: **a transcription of cases that were already written and already
run.** Nothing here is new coverage.

That distinction is the whole scope boundary. Enumerating more vendor surfaces —
response headers, robots.txt, NEL — was deliberately ruled out as unbounded: the
adversary is a vendor's product roadmap and there is always one more surface.
Retaining coverage already paid for is finite and it ends here.

**So: if you are adding a case nobody has run against the live site or a real
finding, stop.** That is the second afternoon, and the answer to it was no.

No network. Every case below is a pure function over markup.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_GUARD = pathlib.Path(__file__).resolve().parents[1] / "check_public_surfaces.py"
_spec = importlib.util.spec_from_file_location("check_public_surfaces", _GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


# The two host sets the guard actually runs with. APEX/WWW are what the
# scheduled run produces; PREVIEW is the `workflow_dispatch` path, which exists
# precisely to aim the guard at a surface that is not in DEFAULT_URLS and which
# defect (5) got wrong.
APEX = guard.first_party_hosts("https://portfoliodb.app/")
WWW = guard.first_party_hosts("https://www.portfoliodb.app/")
PREVIEW = guard.first_party_hosts("https://portfoliodb-app.workers.dev/")


def tag(src: str) -> str:
    return f'<script src="{src}"></script>'


# (label, markup, first-party host set, expected findings)
#
# Labels are kept as they were reported so a case can be traced back to the run
# that found it. `BB` marks the ones review supplied — three of the first four
# defects were found by someone executing what the code does rather than reading
# what it was meant to do, which is why those labels are worth preserving.
CASES = [
    # ---- defect (5): the host set must follow the surface being checked ----
    (
        "BB preview serves its OWN bundle",
        tag("https://portfoliodb-app.workers.dev/_next/static/chunks/main.js"),
        PREVIEW,
        [],
    ),
    (
        "preview run: apex absolute still clean",
        tag("https://portfoliodb.app/_next/static/chunks/main.js"),
        PREVIEW,
        [],
    ),
    (
        "preview host seen on an APEX run",
        tag("https://portfoliodb-app.workers.dev/x.js"),
        APEX,
        ["portfoliodb-app.workers.dev"],
    ),
    (
        "REGRESSION real beacon on a preview run",
        tag("https://static.cloudflareinsights.com/beacon.min.js"),
        PREVIEW,
        ["static.cloudflareinsights.com"],
    ),
    (
        "userinfo pointing off the preview host",
        tag("https://portfoliodb-app.workers.dev@evil.test/t.js"),
        PREVIEW,
        ["evil.test"],
    ),
    ("apex absolute, checked from www", tag("https://portfoliodb.app/_next/x.js"), WWW, []),
    ("www absolute, checked from apex", tag("https://www.portfoliodb.app/_next/x.js"), APEX, []),
    # ---- the regression that matters: this week's actual beacon tag ----
    #
    # A rewrite that stops catching the thing it was written for is the standard
    # way this goes wrong. It survived the HTMLParser rewrite and the host-set
    # change; it stays here so it has to survive the next one too.
    (
        "REGRESSION real beacon tag as served",
        '<script defer src="https://static.cloudflareinsights.com/beacon.min.js/'
        'vcd15cbe7772f49c399c6a5babf22c1241717689176015" integrity="sha512-x"></script>',
        APEX,
        ["static.cloudflareinsights.com"],
    ),
    # ---- defect (3): hosts are parsed, never substring-matched ----
    #
    # The first version tested `"portfoliodb.app" in src` alongside path
    # fragments. This week's beacon tripped it only by luck: the URL has
    # `//static.`, not `/static/`. A vendor one path segment different walked
    # straight past it, and `/static/` is one of the most common CDN paths there
    # is.
    (
        "BB /static/ in a vendor path",
        tag("https://cdn.some-vendor.com/static/analytics.js"),
        APEX,
        ["cdn.some-vendor.com"],
    ),
    (
        "BB /_next/ in a vendor path",
        tag("https://anything.example/_next/chunk.js"),
        APEX,
        ["anything.example"],
    ),
    (
        "BB suffix lookalike host",
        tag("https://portfoliodb.app.evil.test/t.js"),
        APEX,
        ["portfoliodb.app.evil.test"],
    ),
    # Reads as first-party to a human skimming and resolves to evil.test. Nobody
    # wrote a rule for this — it falls out of parsing the host, which is the
    # general form of the correction.
    ("BB userinfo trick", tag("https://portfoliodb.app@evil.test/t.js"), APEX, ["evil.test"]),
    ("BB protocol-relative", tag("//evil.test/t.js"), APEX, ["evil.test"]),
    ("BB uppercase host", tag("https://EVIL.TEST/t.js"), APEX, ["evil.test"]),
    ("BB trailing-dot FQDN", tag("https://evil.test./t.js"), APEX, ["evil.test."]),
    ("host:port, ours", tag("https://portfoliodb.app:443/_next/x.js"), APEX, []),
    # ---- defect (4): the parser, not the regex ----
    #
    # Unquoted attribute values are valid HTML5. The risk was never that an
    # injector omits quotes on purpose — it is that any minifier legally strips
    # them, and a regex requiring quotes then stops seeing *every* script at once
    # while still exiting 0.
    ("BB unquoted src", "<script src=https://evil.test/t.js>", APEX, ["evil.test"]),
    ("BB unquoted src, trailing space", "<script src=https://evil.test/t.js >", APEX, ["evil.test"]),
    ("single quotes", "<script src='https://evil.test/t.js'></script>", APEX, ["evil.test"]),
    ("uppercase tag and attribute", '<SCRIPT SRC="https://evil.test/t.js"></SCRIPT>', APEX, ["evil.test"]),
    ("tab separator, unquoted", "<script\tdefer\tsrc=https://evil.test/t.js>", APEX, ["evil.test"]),
    ("newline before src", '<script\n  src="https://evil.test/t.js"></script>', APEX, ["evil.test"]),
    (
        "extra attributes before src",
        '<script defer async data-x="1" src="https://evil.test/t.js"></script>',
        APEX,
        ["evil.test"],
    ),
    (
        "HTML entity in the URL",
        '<script src="https://evil.test/t.js?a=1&amp;b=2"></script>',
        APEX,
        ["evil.test"],
    ),
    # ---- defect (2): same-origin, invisible to an off-site-URL check ----
    #
    # `/cdn-cgi/` is Cloudflare's reserved path and nothing our build serves from
    # it. Rocket Loader, Email Obfuscation and Bot Fight Mode all land here, and
    # all three are dashboard toggles rather than commits.
    (
        "same-origin /cdn-cgi/",
        tag("/cdn-cgi/scripts/7d0fa10a/cloudflare-static/rocket-loader.min.js"),
        APEX,
        [
            "/cdn-cgi/scripts/7d0fa10a/cloudflare-static/rocket-loader.min.js"
            " (Cloudflare edge injection, same-origin)"
        ],
    ),
    # Deliberately absent, and worth knowing rather than assuming covered: the
    # "unrecognised same-origin script path" branch — anything same-origin
    # outside `/_next/` and `/static/` — has no case here. It was added and
    # described on 2026-08-15 but no case output for it was ever reported, so
    # transcribing one would mean writing a case nobody ran, which is the line
    # this file was capped at. Untested branch, stated rather than papered over.
    # ---- no false positives on what we actually serve ----
    ("our own relative chunk", tag("/_next/static/chunks/main.js"), APEX, []),
    ("our own absolute URL", tag("https://portfoliodb.app/_next/static/chunks/main.js"), APEX, []),
    # ---- the parser removed a false-positive class too ----
    #
    # Markup, not execution. The regex flagged this; the parser correctly ignores
    # it. Traded a false-negative class for fewer false positives in one change.
    ("inside an HTML comment", f"<!-- {tag('https://evil.test/t.js')} -->", APEX, []),
    # ---- the boundary, asserted so it stays a known limit rather than a bug ----
    #
    # HTMLParser enters CDATA inside <script>, so markup written *by* a script is
    # invisible. The first of these genuinely executes and genuinely loads a
    # third-party script.
    #
    # These expect [] on purpose. A static fetch cannot observe what a script
    # builds at runtime, and no version of this file could cross that — covering
    # it needs a headless browser, not a wider pattern. Chasing it means scanning
    # script bodies for markup, which is the regex defect (4) removed, and would
    # reintroduce the HTML-comment false positive above.
    (
        "BOUNDARY document.write",
        "<script>document.write('<script src=\"https://evil.test/t.js\">')</script>",
        APEX,
        [],
    ),
    (
        "BOUNDARY inline RSC-style payload",
        '<script>self.__next_f.push([1,"<script src=\\"https://evil.test/t.js\\">"])</script>',
        APEX,
        [],
    ),
]


@pytest.mark.parametrize(
    "markup,first_party,expected",
    [pytest.param(m, fp, e, id=label) for label, m, fp, e in CASES],
)
def test_injected_scripts(markup, first_party, expected):
    assert guard.injected_scripts(markup, first_party) == expected


def test_first_party_follows_the_surface_being_checked():
    """Defect (5) at the level above the cases: the union, not a replacement.

    A preview surface vouches for its own bundle *and* still trusts the two
    hostnames we serve from — apex and www serve the identical document, so
    checking one must not report an absolute URL to the other as third-party.
    """
    assert APEX == guard.OUR_HOSTS
    assert WWW == guard.OUR_HOSTS
    assert set(PREVIEW) == set(guard.OUR_HOSTS) | {"portfoliodb-app.workers.dev"}


def test_first_party_is_required_not_defaulted():
    """A default host set is defect (5) in a form that cannot be seen.

    The caller would silently get a host set unrelated to the page it just
    fetched, and a right answer and a wrong one would look identical.
    """
    with pytest.raises(TypeError):
        guard.injected_scripts("<html></html>")


# Also deliberately absent, and the most consequential of the three, so read it
# before assuming this file protects the property it most looks like it does:
# **nothing here covers `main()` passing the URL that was *requested* rather than
# the one that answered.** `check(markup, first_party_hosts(url))` is what stops
# a hijacked surface that redirects from vouching for its own injected origin —
# and swapping `url` for `landed` reintroduces that hole with all 32 cases still
# green. Measured, not assumed:
#
#     baseline                                  32 passed
#     revert host parsing to substring matching  3 failed
#     drop the union, fixed known hosts only     2 failed
#     requested -> landed                       32 passed   <- silent
#
# Every case here is a pure function over markup; the wiring lives in `main()`,
# and reaching it means monkeypatching `fetch`. Covering it is invention rather
# than transcription, which is the same standard that removed the two below.
# So it is written down instead — because "the guard's adversarial set is in the
# repo" would otherwise read as covering this, and it does not.
#
# Also deliberately absent: the parse-failure path in `script_sources()`, which
# exits non-zero rather than returning an empty list so that "no scripts found"
# and "could not read the page" cannot look identical. It is real behaviour and
# it is untested — but no case for it was ever run, and covering it needs
# monkeypatching the parser, which is invention rather than transcription.
#
# Both of these gaps are decisions someone can take later with the run to back
# it. Neither is an oversight.
