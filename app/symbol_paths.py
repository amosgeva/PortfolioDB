"""Symbols that are allowed to become file names.

A ticker symbol is operator-entered text: the CSV importer, `add-lot` and the
Manage page take whatever string arrives and uppercase it. Two places turned
that string into a path under the logo cache — the fetcher writing
``<sym>.png`` and the dashboard reading it back as a data URI — so a symbol
containing a path separator would have written outside the cache directory,
or read a file from outside it into the page (audit follow-up to F12's
"constrain symbol-derived filesystem paths").

The allowlist below is what real symbols look like across the venues yfinance
serves: ``BRK.B``, ``BRK-B``, ``^GSPC``, ``ES=F``, ``BTC-USD``, ``0700.HK``,
``TA35.TA``. No separators, no whitespace, no control characters, a sane
length. Anything else gets no path at all rather than a sanitised one — a
symbol that fails this is not a symbol.
"""

from __future__ import annotations

import re
from pathlib import Path

# Uppercase because every writer uppercases; the fetcher and the payload both
# pass symbols that already went through .upper(). A leading `^` is an index
# (^GSPC); a leading `.` or `-` is not a symbol and would name a hidden file.
SYMBOL_FILENAME_RE = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=^_]{0,31}$")


def is_safe_symbol(symbol: object) -> bool:
    return isinstance(symbol, str) and bool(SYMBOL_FILENAME_RE.fullmatch(symbol))


SYMBOL_RULE = (
    "1-32 characters: A-Z, 0-9 and . - = ^ _, starting with a letter, a digit "
    "or ^ (e.g. NVDA, BRK.B, BRK-B, ^GSPC, ES=F, 0700.HK)"
)


def normalize_symbol(raw: object) -> str:
    """The one way a symbol enters the ledger: strip, uppercase, validate.

    Every writer calls this — the add/sell/watchlist/income CLIs, the CSV
    importer, the enrichment scripts and the Manage page — and the database
    enforces the same pattern (``instruments_symbol_format``, migration 004).
    A symbol is rendered into the dashboard and used as a file name, so
    anything that is not shaped like one is refused rather than cleaned up
    (audit A-3/D-2): ``<img src=x>`` is not a ticker, and HTML numeric entities
    survive upper-casing.

    Raises ValueError with a message that says what a symbol may contain.
    """
    if not isinstance(raw, str):
        raise ValueError(f"symbol must be text, got {type(raw).__name__}")
    symbol = raw.strip().upper()
    if not symbol:
        raise ValueError("symbol is empty")
    if not is_safe_symbol(symbol):
        raise ValueError(f"{raw.strip()!r} is not a valid symbol: use {SYMBOL_RULE}")
    return symbol


def symbol_arg(text: str) -> str:
    """``argparse`` type: ``normalize_symbol`` reported as a usage error."""
    import argparse

    try:
        return normalize_symbol(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


# Yahoo-style exchange suffixes of venues that do not quote in US dollars. The
# ledger is USD-only (README "Scope", docs/methodology.md): a .TA price is in
# agorot and an .L price in pence, and summing either as dollars is wrong by a
# factor of hundreds. Share-class suffixes (BRK.B, BF.A) are deliberately not
# here — they are single letters this list never contains.
FOREIGN_LISTING_SUFFIXES = frozenset({
    "AS", "AT", "AX", "BA", "BK", "BO", "BR", "CN", "CO", "DE", "F", "HE", "HK",
    "IL", "IR", "JK", "JO", "KL", "KQ", "KS", "L", "LS", "MC", "MI", "MX", "NE",
    "NS", "NZ", "OL", "PA", "SA", "SG", "SI", "SN", "SR", "SS", "ST", "SW", "SZ",
    "T", "TA", "TO", "TW", "TWO", "V", "VI", "WA",
})


def foreign_listing_suffix(symbol: str) -> str | None:
    """The exchange suffix when ``symbol`` names a non-US listing, else None.

    A heuristic for warnings and refusals at write time, not a currency
    lookup: ``.TA``, ``.L`` or ``.HK`` are venue suffixes, while the share
    classes written with a dot (``BRK.B``, ``BF.B``) use letters this list
    does not contain. Indices (``^TA125.TA``) are benchmarks, never held, so
    they are not reported.
    """
    if not isinstance(symbol, str) or symbol.startswith("^") or "." not in symbol:
        return None
    suffix = symbol.rsplit(".", 1)[1].upper()
    return suffix if suffix in FOREIGN_LISTING_SUFFIXES else None


def logo_path(logo_dir: Path, symbol: str) -> Path | None:
    """``logo_dir/<symbol>.png``, or None when the symbol may not name a file."""
    if not is_safe_symbol(symbol):
        return None
    return logo_dir / f"{symbol}.png"
