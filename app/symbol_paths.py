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


def logo_path(logo_dir: Path, symbol: str) -> Path | None:
    """``logo_dir/<symbol>.png``, or None when the symbol may not name a file."""
    if not is_safe_symbol(symbol):
        return None
    return logo_dir / f"{symbol}.png"
