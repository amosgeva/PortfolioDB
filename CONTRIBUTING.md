# Contributing

Issues are welcome — bug reports, "this was confusing", and questions about how
something works are all useful. For code, **open an issue first** if the change
is more than a fix: this is one person's ledger as much as it is a project, and
some things are deliberate that look like omissions (no positions table, no
broker sync, manual cash entry).

## Getting a dev environment

```bash
cp .env.template .env          # fill in a Postgres password
make up                        # postgres + dashboard + scheduler
make schema
make demo-seed                 # fictional data — never use your real ledger to develop
make test
```

`make` on its own lists every target. `make shell` drops you into the app
container.

Enable the pre-commit hook once per clone (hooks aren't cloned):

```bash
git config core.hooksPath .githooks
```

It rejects unused imports in staged Python files and runs both suites.
`git commit --no-verify` bypasses it for work in progress.

## Tests

Two suites, and **they need different working directories** — this trips
everyone up once:

```bash
cd app && pytest tests/        # engines, settings, market window: bare imports
pytest app/mcp/tests/ -m "not slow"   # MCP: from the REPO ROOT
```

Running the MCP suite from `app/` puts the local `app/mcp/` package on the path
as top-level `mcp`, shadowing the official `mcp` SDK that fastmcp needs. It
fails as a confusing *"FastMCP server support is not installed"*.

The `slow` marker covers tests that run the real services against a live
database (~30s). Run them separately, not in the same process as the fast suite:

```bash
pytest app/mcp/tests/ -m slow
```

Or just `make test`, which runs the fast suites inside the container.

## What the code expects of you

- **Money is `Decimal`** end to end inside the engines. Convert to `float` only
  when handing data to a DataFrame for display.
- **The ledger is append-only.** Corrections are new rows, and split adjustment
  happens at read time (`app/corporate_actions.py`) — nothing rewrites `lots` or
  `price_snapshots`.
- **There is no positions table.** Open quantity, cost basis and realized P&L are
  recomputed from `lots` on every read, through
  `app/portfolio.py::compute_fifo_merged`. Keep its output contract stable.
- **Symbols are uppercase** on the way in.
- **Credentials come from `app/db.py::load_config`.** Never read a password
  directly, never hardcode one, not even in a throwaway script.
- **Settings go through `app/settings.py`** (DB → env → default), so a value can
  be changed from the Settings page. Secrets stay in `.env` only.
- Match the surrounding style, including comment density. Comments here explain
  *why* something is the way it is — especially where the obvious approach was
  tried and abandoned.

## Schema changes

Add a numbered file under `sql/migrations/` **and** make the same change in
`sql/schema.sql` in the same commit, then bump the version header there and
`SCHEMA_VERSION` in `app/mcp/services/cutoff.py`. Fresh installs read
`schema.sql`; existing ones apply migrations in order. See
`sql/migrations/README.md`.

## Cutting a release

Write the entry into `[Unreleased]` as you go. When you cut it into a numbered
section, **re-read what you are freezing for present-tense claims about anything
that floats.**

A changelog entry may name a version — that is the file's whole job, which is why
`CHANGELOG.md` is exempt from CI's pin grep. **It may not say what a floating tag
*currently* resolves to.** The tag moves; the frozen entry cannot follow it.

This is not hypothetical. The 1.1.3 entry said `:1` "resolves to the current
1.1.2". It was true when written into `[Unreleased]`, and the release cut that
froze it was the release that falsified it — `:1` resolves to 1.1.3, the version
that sentence is printed inside. Name the line (`the current 1.1 line`) or the
release, not the resolution.

Two conventions for fixing an entry after it ships:

- **A stale factual aside is edited in place.** Nobody acted on it, and leaving a
  wrong number to preserve a record of a wrong number helps no one.
- **An instruction a reader may already have followed is annotated, not
  rewritten** — a dated `> Correction, YYYY-MM-DD` note underneath. Quietly
  editing a step someone has already run is the failure this file exists to
  prevent. Where an instruction contains a stale version, bound the version and
  leave the action alone.

None of this is enforced. A guard that could catch it would have to forbid the
file from naming versions, which would break it. This is documentation, and
documentation is weaker than CI — the test counts and the image pins are guarded
because they could be.

## Commits and PRs

Explain *why* in the message, not just what — the diff already says what. Keep
one concern per PR where you can. CI runs both suites on Linux; a green run and
a description of how you verified the change by hand is plenty.

### One note on this repository's history

The history starts at a single squashed commit in August 2026, and commits before
that date do not exist here. That is not a rewrite: development happened in a
private repository whose history carries real trade dates and holdings, so this
one was started clean rather than by publishing that. The private repo is now an
archive and holds nothing but that history and planning notes.

**Development happens here.** Pull requests are reviewed and merged here, and your
commits stay yours.
