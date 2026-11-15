"""Which account names the ledger may store, and the one rule every writer uses.

A lot recorded without an account has ``account IS NULL``. Every report, the
dashboard and the MCP server show that bucket as ``(none)``, and the MCP server
accepts ``(none)`` back as the filter that selects it. Nothing used to stop an
account from actually being *named* ``(none)``: the Manage page, the CLIs, the
CSV importer and the schema all accepted it. Such an account could not be
selected (the filter means ``account IS NULL``), merged into the unassigned
bucket in the account listing, vanished from allocation by account, and lent
its cash to the unassigned bucket (rescore NEW-1, audit X-3/X-8).

So the label is reserved. Every writer refuses it through this module, the MCP
readers recognise it through this module, and the database refuses it too
(``*_account_not_reserved`` CHECKs, migration 007), so a writer that does not
come through here cannot store it either.

The comparison is on a *folded* name, and the fold uses no Unicode data at
all, so Python and PostgreSQL compute it identically by construction:

1. delete every character that is not an ASCII letter or ``(`` / ``)`` —
   by code point, no locale, no character tables;
2. lowercase A-Z, and nothing else.

A name is reserved when it folds to ``(none)``. Every spacing, case and
invisible-character variant of the ASCII label is therefore reserved — ``(NONE)``,
``( none )``, ``(none)`` padded with no-break spaces or holding a zero-width
space — and so are a few odd names that only add digits or punctuation, such as
``(none 2)``. A name built from *other* letters — fullwidth ``(`` ``n`` ..., or
mathematical / outlined Latin letters — folds to something else on both sides
and is simply an ordinary, distinct account name everywhere.

The database implements the same two steps in ``pdb_account_fold(text)``
(sql/schema.sql, repeated in migration 007):

    translate(regexp_replace(account COLLATE "C", '[^A-Za-z()]', '', 'g'),
              'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')

Two earlier drafts compared differently in the two places: PostgreSQL's
locale-dependent ``[[:space:]]`` against Python's whitespace (QA of PR #133,
B1), then NFKC normalisation, whose tables differ between Python 3.14 (Unicode
16) and PostgreSQL 16 (B2). Both let a name through the database that the
readers took for the label. tests/test_account_names.py runs both
implementations over every code point.
"""

from __future__ import annotations

import argparse
import re
import string

# The label for lots recorded without an account (account IS NULL).
UNASSIGNED_LABEL = "(none)"

# Folded forms (see fold) of every name no account may have.
RESERVED = frozenset({UNASSIGNED_LABEL})

# Step 1 of the fold: what is deleted. The SQL function carries this exact
# bracket expression; a test holds the two together.
FOLD_DELETE = "[^A-Za-z()]"
_DELETE = re.compile(FOLD_DELETE)

# Step 2: ASCII-only lowercasing, as SQL's translate() spells it.
ASCII_UPPER = string.ascii_uppercase
ASCII_LOWER = string.ascii_lowercase
_TO_LOWER = str.maketrans(ASCII_UPPER, ASCII_LOWER)


def fold(name: str) -> str:
    """Every character but A-Z, a-z, ``(`` and ``)`` deleted; then A-Z lowercased."""
    return _DELETE.sub("", name).translate(_TO_LOWER)


def is_reserved(name: object) -> bool:
    """True when ``name`` is a string that folds to a reserved label."""
    return isinstance(name, str) and fold(name) in RESERVED


def refusal(name: str) -> str:
    return (
        f"{name!r} cannot be used as an account name: '{UNASSIGNED_LABEL}' is the label every "
        "report and the MCP server use for lots recorded without an account, so an account "
        "whose name reads as it (in any case, spacing or punctuation) could not be told apart "
        "from them. Leave the account empty for unassigned lots, or choose another name."
    )


def refuse_reserved(name: str | None) -> str | None:
    """``name`` unchanged, or ValueError when it is a reserved label."""
    if is_reserved(name):
        raise ValueError(refusal(name))
    return name


def normalize_account(raw: object) -> str | None:
    """The account a writer stores for ``raw``: stripped, ``None`` when blank.

    ValueError (with a message fit to show) when it is a reserved label.
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        return None
    return refuse_reserved(text)


def account_arg(raw: str) -> str | None:
    """argparse ``type=`` for an ``--account`` flag: a reserved name is a usage error."""
    try:
        return normalize_account(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
