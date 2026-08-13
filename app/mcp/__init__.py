"""PortfolioDB MCP server.

Exposes portfolio data and analytics over the Model Context Protocol via
Streamable HTTP, with Bearer-token authentication. Runs parallel to the
Streamlit dashboard and reuses the same engine modules (portfolio, fifo,
avg_cost, fd_store) without modifying them.
"""

import sys
from pathlib import Path

# Make the sibling app/ modules importable, here in the package __init__ so it
# happens before ANY app.mcp.* submodule body runs. Several services import
# bare siblings (portfolio, corporate_actions, advisor, db) above their own
# `from app.mcp.deps import ...` line, and used to work only because something
# else had already imported deps and mutated sys.path as a side effect —
# importing such a module first, as a single-test pytest run does, failed with
# "No module named 'portfolio'".
_APP_DIR = Path(__file__).resolve().parent.parent
if str(_APP_DIR) not in sys.path:
    # Append, not insert(0): app/ must be reachable for the sibling modules but
    # must NOT take priority over site-packages, or this very package (app/mcp)
    # shadows the installed `mcp` SDK that fastmcp imports (`mcp.types`) — which
    # surfaces as the misleading "FastMCP server support is not installed".
    sys.path.append(str(_APP_DIR))
