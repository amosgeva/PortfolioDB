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

The comparison is on a *folded* name, so that spellings a person reads as the
same label are refused as well — ``(NONE)``, ``( none )``, ``(\\u00a0none\\u00a0)``
pasted with no-break spaces, fullwidth ``（ｎｏｎｅ）``. The fold is defined
here once, exactly, and the database implements the same steps in
``pdb_account_fold(text)`` (sql/schema.sql, repeated in migration 007):

1. Unicode NFKC normalisation (fullwidth forms, compatibility spaces);
2. removal of every character in ``IGNORED_RANGES``: the Unicode White_Space
   characters, the four separators Python's ``str.split`` also treats as
   whitespace (U+001C–U+001F), and the invisible format characters a pasted
   name can carry (soft hyphen, zero-width space/joiners, word joiner, BOM);
3. ASCII-only lowercasing (A–Z → a–z, nothing else), which is what
   PostgreSQL's ``lower(... COLLATE "C")`` does — so no locale, and no
   collation, decides the answer on either side.

PostgreSQL's ``[[:space:]]`` and Python's ``str.split()`` disagree (NBSP, figure
space and narrow NBSP are whitespace to one and not the other, and the
database's answer depends on its locale), which let a no-break-spaced
``(none)`` through migration 007 while the readers still treated it as the
label (QA of PR #133, B1). Hence an explicit list rendered into the SQL, and
tests that run both implementations over the same corpus.
"""

from __future__ import annotations

import argparse
import string
import unicodedata

# The label for lots recorded without an account (account IS NULL).
UNASSIGNED_LABEL = "(none)"

# Folded forms (see fold) of every name no account may have.
RESERVED = frozenset({UNASSIGNED_LABEL})

# Characters the fold removes, as inclusive code-point ranges. The database
# function's regular-expression class is rendered from this list
# (sql_char_class); tests/test_account_names.py holds the SQL to it.
IGNORED_RANGES: tuple[tuple[int, int], ...] = (
    (0x0009, 0x000D),  # TAB, LF, VT, FF, CR                       White_Space
    (0x001C, 0x0020),  # FS GS RS US (str.split whitespace), SPACE  White_Space: 0020
    (0x0085, 0x0085),  # NEXT LINE                                 White_Space
    (0x00A0, 0x00A0),  # NO-BREAK SPACE                            White_Space
    (0x00AD, 0x00AD),  # SOFT HYPHEN                               invisible format
    (0x1680, 0x1680),  # OGHAM SPACE MARK                          White_Space
    (0x180E, 0x180E),  # MONGOLIAN VOWEL SEPARATOR                 invisible format
    (0x2000, 0x200D),  # EN QUAD … HAIR SPACE (White_Space), ZWSP, ZWNJ, ZWJ
    (0x2028, 0x2029),  # LINE / PARAGRAPH SEPARATOR                White_Space
    (0x202F, 0x202F),  # NARROW NO-BREAK SPACE                     White_Space
    (0x205F, 0x2060),  # MEDIUM MATHEMATICAL SPACE (White_Space), WORD JOINER
    (0x3000, 0x3000),  # IDEOGRAPHIC SPACE                         White_Space
    (0xFEFF, 0xFEFF),  # ZERO WIDTH NO-BREAK SPACE (BOM)           invisible format
)

IGNORED = frozenset(chr(c) for lo, hi in IGNORED_RANGES for c in range(lo, hi + 1))

_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def fold(name: str) -> str:
    """NFKC, then every IGNORED character removed, then A–Z lowercased.

    The same three steps, in the same order, as ``pdb_account_fold`` in SQL.
    """
    text = unicodedata.normalize("NFKC", name)
    text = "".join(ch for ch in text if ch not in IGNORED)
    return text.translate(_ASCII_LOWER)


def sql_char_class() -> str:
    """IGNORED_RANGES as the bracket expression the SQL function uses, written
    with ``\\uXXXX`` escapes so the SQL file stays ASCII."""
    parts = []
    for lo, hi in IGNORED_RANGES:
        parts.append(f"\\u{lo:04X}" if lo == hi else f"\\u{lo:04X}-\\u{hi:04X}")
    return "[" + "".join(parts) + "]"


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
