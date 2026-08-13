# The site

`portfoliodb.app` — one HTML file, one stylesheet, no framework and no build
step. A marketing page for a self-hosted tool that needed an npm pipeline would
be an argument against its own product.

## Preview it

```bash
cd site && python -m http.server 8899
# then open http://127.0.0.1:8899
```

The images resolve only in the assembled artifact (they live in `docs/images/`
and are copied at deploy time, so there is exactly one copy in git). To preview
with them:

```bash
mkdir -p /tmp/site && cp site/index.html site/style.css /tmp/site/
mkdir -p /tmp/site/images && cp docs/images/*.webp docs/images/social-preview.png /tmp/site/images/
cp app/static/icon.svg /tmp/site/images/icon.svg
cd /tmp/site && python -m http.server 8899
```

## Deploy it

`.github/workflows/pages.yml` assembles and publishes. It is **manual-only**
(`workflow_dispatch`) until Pages is switched on, because a workflow that fails
on every push trains you to ignore it.

1. **Settings → Pages → Source: GitHub Actions.**
2. Run **Deploy site** once from the Actions tab. It fails loudly if any local
   `href`/`src` in the page does not resolve — a 404 image on the launch page is
   the cheapest own-goal available.
3. **DNS for the apex domain.** GitHub Pages wants four `A` records and four
   `AAAA` records pointing at their servers, or an `ALIAS`/`ANAME` if your
   registrar supports it. Take the current addresses from GitHub's own
   "Managing a custom domain" page rather than from here — they change rarely,
   but a stale IP in a README is a broken site.
4. **Settings → Pages → Custom domain: `portfoliodb.app`**, then tick **Enforce
   HTTPS** once the certificate is issued (usually minutes).
5. Set the repository's `homepage` field to `https://portfoliodb.app` — and not
   before it resolves, since a link to a blank domain reads as abandoned.
6. Uncomment the `push` trigger in the workflow so the page follows `main`.

The workflow writes `CNAME` into the artifact on every run. That is not
decoration: without it Pages drops the custom domain on each deploy and the site
quietly reverts to `*.github.io`.

## Two deliberate omissions

**No web fonts.** The dashboard imports Space Grotesk from Google, which is
harmless inside your own LAN. This page argues that your data stays yours, and a
font request hands every visitor's IP to Google — the same reasoning that keeps
Google Analytics off the page. It uses the system stack, and looks like the
product because the *tokens* match, not the typeface.

**No analytics yet, and the footer says so.** When you add some, it must be
cookieless and self-hosted or first-party — Plausible, Umami, or Cloudflare Web
Analytics. Then change the footer line, which currently reads:

> This website is separate: it is static, sets no cookies, and currently runs no
> analytics of any kind.

That sentence is load-bearing. The *app* has no telemetry; a *site* with
analytics is a different claim, and letting the app's promise cover the site
would be the kind of quiet overclaim this project cannot survive. If you add
Plausible, say so there in the same breath.

Events worth firing when analytics exist (from the funnel spec): `copy_install_block`
as the primary conversion, `click_github`, `click_mcp_docs`, `view_mcp_section`
as its denominator, and `scroll_75`.

## What must never appear here

From the site spec, and these are approval conditions rather than style notes: no
star counts or borrowed credibility, no performance or return figures (including
from the demo seed), no "bank-grade"/"secure" security claims when there is no
authentication, no "first/only" MCP claim until somebody has actually surveyed
the field, no competitor comparison table, no pricing or business-model
discussion, no email capture, and no live hosted demo — the README tells users
not to expose an unauthenticated instance, so running one publicly would be us
doing the thing we warn against.
