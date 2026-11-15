"""The benchmark every surface compares the portfolio against — one rule.

Before 1.8.0 the MCP read `PORTFOLIODB_BENCHMARK_SYMBOL` and the dashboard
computed against a hard-coded SPY, so an operator who set the variable got one
benchmark from the dashboard and another from the MCP (audit U-3). Both now
resolve it here.

Resolution: an explicit argument, then `PORTFOLIODB_BENCHMARK_SYMBOL`, then
SPY. Blank values fall through (an empty variable in `.env` is "unset", not
"compare against nothing"), and the result is upper-cased like every stored
symbol. The label is `PORTFOLIODB_BENCHMARK_LABEL` when set, else the symbol.

Pure: environment only, no database, safe to import anywhere.
"""

from __future__ import annotations

import os

DEFAULT_BENCHMARK = "SPY"
SYMBOL_ENV = "PORTFOLIODB_BENCHMARK_SYMBOL"
LABEL_ENV = "PORTFOLIODB_BENCHMARK_LABEL"


def benchmark_symbol(override: str | None = None) -> str:
    """The benchmark symbol: `override`, else the env var, else SPY."""
    for candidate in (override, os.getenv(SYMBOL_ENV)):
        if candidate is not None and candidate.strip():
            return candidate.strip().upper()
    return DEFAULT_BENCHMARK


def benchmark_label(symbol: str | None = None) -> str:
    """How the benchmark is named on screen: the label env var, else the symbol.

    The label names the *configured* benchmark, so an explicit `symbol` that
    differs from it is labelled by its own symbol.
    """
    configured = benchmark_symbol()
    resolved = benchmark_symbol(symbol)
    label = os.getenv(LABEL_ENV)
    if resolved == configured and label is not None and label.strip():
        return label.strip()
    return resolved
