"""portfolio://schema — data dictionary in Markdown for ambient agent context."""

from __future__ import annotations

from fastmcp import FastMCP

from app.mcp.tools.meta_tools import _SCHEMA_DICT


def _render_markdown(d: dict) -> str:
    lines: list[str] = []
    lines.append("# PortfolioDB — Data Dictionary\n")
    lines.append(d["overview"] + "\n")

    lines.append("## Conventions\n")
    for k, v in d["conventions"].items():
        lines.append(f"- **{k}** — {v}")
    lines.append("")

    lines.append("## Tables\n")
    for tbl, meta in d["tables"].items():
        lines.append(f"### `{tbl}`")
        lines.append(meta["purpose"])
        if "primary_key" in meta:
            lines.append(f"- Primary key: `{meta['primary_key']}`")
        if "unique_index" in meta:
            lines.append(f"- Unique index: `{meta['unique_index']}`")
        lines.append("")
        lines.append("| column | description |")
        lines.append("|---|---|")
        for col, desc in meta["columns"].items():
            lines.append(f"| `{col}` | {desc} |")
        lines.append("")

    fd = d["fd_tables"]
    lines.append("## Financial Datasets enrichment\n")
    lines.append(fd["purpose"])
    lines.append("")
    lines.append("Tables: " + ", ".join(f"`{n}`" for n in fd["names"]))
    lines.append("")
    lines.append(fd["freshness"])
    lines.append("")

    lines.append("## Engines\n")
    for name, desc in d["engines"].items():
        lines.append(f"- **{name}** — {desc}")
    lines.append("")

    return "\n".join(lines)


def register(mcp: FastMCP) -> None:
    @mcp.resource(
        "portfolio://schema",
        name="PortfolioDB schema",
        description="Data dictionary: tables, columns, conventions, engine contracts.",
        mime_type="text/markdown",
    )
    def schema_resource() -> str:
        return _render_markdown(_SCHEMA_DICT)
