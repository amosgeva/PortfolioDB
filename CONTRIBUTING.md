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

## Commits and PRs

Explain *why* in the message, not just what — the diff already says what. Keep
one concern per PR where you can. CI runs both suites on Linux; a green run and
a description of how you verified the change by hand is plenty.

### How your PR actually gets merged

Worth saying plainly, because it affects what you see happen to your commits.
This repository is **published from a private development repo**, and every
commit here says so in its body. That has one consequence for contributors:

**Your PR is applied to the private repo and arrives back here in the next
publish, rather than being merged with a green merge button.** Your commits keep
your name and email — they are cherry-picked, not retyped — but the PR itself is
closed with a note pointing at the commit that carries your change, instead of
showing as merged. GitHub will not credit you as a merged contributor on the
graph, which is a real cost and the reason this section exists rather than
leaving you to work it out.

Nothing else changes: review happens in the PR, CI runs on it, and a change that
is accepted ships. If that arrangement is a dealbreaker for you, an issue
describing the fix is just as welcome as the patch.
