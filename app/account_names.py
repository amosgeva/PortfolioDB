"""Which account names the ledger may store, and the one rule every writer uses.

A lot recorded without an account has ``account IS NULL``. Every report, the
dashboard and the MCP server show that bucket as ``(none)``, and the MCP server
accepts ``(none)`` back as the filter that selects it. Nothing used to stop an
account from actually being *named* ``(none)``: the Manage page, the CLIs, the
CSV importer and the schema all accepted it. Such an account could not be
selected (the filter means ``account IS NULL``), merged into the unassigned
bucket in the account listing, vanished from allocation by account, and lent
its cash to the unassigned bucket (rescore NEW-1, audit X-3/X-8).

So the label is reserved. Every writer refuses it through this module, and the
database refuses it too (``*_account_not_reserved`` CHECKs, migration 007), so
a writer that does not come through here cannot store it either. The
comparison ignores case and whitespace — ``(NONE)`` and ``( none )`` read as
the same label to a person, so they are refused as well. The database mirrors
that rule exactly:

    lower(regexp_replace(account, '[[:space:]]', '', 'g')) <> '(none)'

``tests/test_account_names.py`` holds the two together.
"""

from __future__ import annotations

import argparse

# The label for lots recorded without an account (account IS NULL).
UNASSIGNED_LABEL = "(none)"

# Folded forms (see fold) of every name no account may have.
RESERVED = frozenset({UNASSIGNED_LABEL})


def fold(name: str) -> str:
    """``name`` lowercased with every whitespace character removed."""
    return "".join(name.split()).lower()


def is_reserved(name: object) -> bool:
    """True when ``name`` is a string that folds to a reserved label."""
    return isinstance(name, str) and fold(name) in RESERVED


def refusal(name: str) -> str:
    return (
        f"{name!r} cannot be used as an account name: '{UNASSIGNED_LABEL}' is the label every "
        "report and the MCP server use for lots recorded without an account, so an account "
        "with that name could not be told apart from them. Leave the account empty for "
        "unassigned lots, or choose another name."
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
