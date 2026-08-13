# Security

## Reporting a vulnerability

Use GitHub's private reporting: **Security → Report a vulnerability** on this
repository. That opens a channel only you and the maintainer can see. Please
don't open a public issue for anything exploitable.

Include what you'd want to receive: what you did, what happened, and why it
matters. A proof of concept helps.

This is a spare-time project, not a funded one. Expect a first reply within a
week or so, and no bug bounty. Fixes for anything that lets an attacker read or
alter someone's ledger take priority over everything else.

## What this software assumes about its environment

PortfolioDB is built to run on a machine you control, reachable only from your
own network. Two consequences worth stating plainly rather than treating as
findings:

- **The dashboard has no authentication.** Anyone who can reach port 8501 can
  see and change the ledger. Keep it off the public internet, or put an
  authenticating reverse proxy in front of it — see
  [docs/exposure.md](docs/exposure.md).
- **The MCP (Model Context Protocol) server's bearer token is the main thing
  protecting it.** It binds to `127.0.0.1` by default and its database role is
  read-only
  (`sql/create_ro_role.sql`), so the blast radius is limited to reading, but the
  token is a real credential: treat it like a password.

Reports of the form "the dashboard has no login" are already documented above.
Reports that it can be reached *without* being deliberately exposed, or that the
MCP surface can write, or that the token can be bypassed, are exactly what we
want to hear about.

## Secrets

Secrets live in `.env` at the repo root, which is gitignored, excluded from the
build context, and read only by server-side processes — the browser is served
rendered output, not the file. Runtime settings live in the database and are
editable from the dashboard; API keys and passwords deliberately are not,
because the dashboard has no authentication. The Settings page reports whether a
key is set, not what it is. If you find a path that leaks `.env` contents into a
response, a log line, or a container image, that is a vulnerability — please
report it.

## Supported versions

The latest `main` is what gets fixed. There are no maintained release branches.
