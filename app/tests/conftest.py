"""Environment pinning for the bare-module suite.

Two things every test in this tree needs, both established at *import* time
because the modules under test capture their `LOCAL_TZ` at import:

1. **A fixed reporting timezone.** The shipped default is now UTC, but many
   tests here encode genuinely non-UTC semantics — a split taking effect at
   16:30 local (the US open), a trade at 23:30 local belonging to the local
   day rather than the UTC one. Pinning Asia/Jerusalem keeps those cases
   meaningful: they are the regression tests for "a reporting timezone that
   is not UTC", which is the case most likely to break.

2. **No database.** `settings.get()` consults the `settings` table first, so a
   row written from the Settings page would otherwise change what the suite
   resolves. Freezing the cache empty with an effectively infinite TTL makes
   every lookup fall through to env → default on any machine, with or without
   a live Postgres.

app/mcp/tests/conftest.py does the same for the MCP suite.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Make the bare app/ modules importable however pytest was invoked.
_APP_DIR = Path(__file__).resolve().parent.parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

# Forced, not setdefault: an exported PORTFOLIODB_TZ must not change results.
os.environ["PORTFOLIODB_TZ"] = "Asia/Jerusalem"

import settings  # noqa: E402  — must follow the sys.path fix above

settings.TTL_SECONDS = float("inf")
settings._cache = {}
settings._cache_at = time.monotonic()
settings._db_ok = False
