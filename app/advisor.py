"""LLM-backed advisor layer over PortfolioDB.

Three layers injected into every model call:
  1. Operator philosophy (philosophy.md at repo root) — static, cached
  2. Live portfolio snapshot (FIFO merge + latest prices + cash) — computed from DB
  3. Recent chat history (chat_log table) — last 30 messages

Entry points:
  - morning_brief(kind='morning') -> dict          # structured JSON, persisted
  - chat(question) -> Iterator[str]                 # streaming tokens, history saved

CLI:
  python advisor.py brief
  python advisor.py ask "what's our concentration risk right now?"

The provider/model come from app/llm.py (Settings page → LLM_* env vars →
Anthropic default); the API key is env-only — see docs/llm-providers.md.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import pathlib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator

import llm
import settings
from db import connect, execute, fetch_all, load_config
from portfolio import compute_fifo_merged

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PHILOSOPHY_PATH = REPO_ROOT / "philosophy.md"

# Where the pasted one-pager is stored. It lives in `settings` rather than its
# own table so existing installs need no migration; it is one small document.
PHILOSOPHY_KEY = "philosophy_md"

# The shipped template says so in its own header. Detecting it matters because
# an unedited template *loads fine* — the UI would report "✓ loaded" and the
# advisor would dutifully reason about bracketed placeholders.
_TEMPLATE_MARKER = "STATUS: TEMPLATE"


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — philosophy
# ─────────────────────────────────────────────────────────────────────────────

def _philosophy_from_file() -> str | None:
    # is_file(), not exists(): compose bind-mounts ./philosophy.md into the
    # container, and Docker creates an empty *directory* at the source path
    # when the host file is missing. exists() is True for that directory and
    # the read then explodes.
    if not PHILOSOPHY_PATH.is_file():
        return None
    try:
        text = PHILOSOPHY_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def philosophy_source() -> str:
    """Where the one-pager comes from: 'database' | 'file' | 'template' | 'none'.

    The UI shows this because the two sources are easy to confuse — and because
    a bind-mounted file that an editor saved atomically goes stale inside the
    container until it restarts, which is invisible otherwise.
    """
    stored = settings.get(PHILOSOPHY_KEY)
    if stored and stored.strip():
        return "template" if _TEMPLATE_MARKER in stored else "database"
    text = _philosophy_from_file()
    if text:
        return "template" if _TEMPLATE_MARKER in text else "file"
    return "none"


def load_philosophy() -> str:
    """The operator's one-pager: pasted copy first, then a mounted file.

    Same precedence as every other setting (database over environment/file), so
    there is one rule to remember. Pasting is the documented path because a
    bind-mounted file is only re-read when the container restarts if the editor
    replaced it rather than writing in place.
    """
    stored = settings.get(PHILOSOPHY_KEY)
    text = stored.strip() if stored and stored.strip() else _philosophy_from_file()

    if not text:
        return (
            "(No investor one-pager yet — the operator has not written one. "
            "Give cautious, generic advice and prompt them to write one.)"
        )
    if _TEMPLATE_MARKER in text:
        return (
            "(The one-pager is still the unedited template: every section is a "
            "bracketed placeholder, so it carries no real preferences. Treat it "
            "as absent, give cautious generic advice, and prompt the operator to "
            "fill it in.)\n\n" + text
        )
    return text


def save_philosophy(text: str) -> None:
    """Store a pasted one-pager, or clear it when handed nothing."""
    if text and text.strip():
        settings.set_value(PHILOSOPHY_KEY, text.strip())
    else:
        settings.unset(PHILOSOPHY_KEY)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — live portfolio snapshot
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PositionView:
    symbol: str
    qty: Decimal
    open_cost: Decimal
    avg_cost: Decimal
    realized: Decimal
    last_price: Decimal | None
    last_ts: datetime | None
    market_value: Decimal | None
    unrealized: Decimal | None


def _latest_prices(conn) -> dict[str, dict]:
    rows = fetch_all(
        conn,
        """
        SELECT DISTINCT ON (symbol) symbol, ts, last_price, bid, ask, source
        FROM price_snapshots
        ORDER BY symbol, ts DESC
        """,
    )
    return {r["symbol"]: r for r in rows}


def _latest_cash(conn) -> list[dict]:
    return fetch_all(
        conn,
        """
        SELECT DISTINCT ON (account) account, cash, ts, note
        FROM cash_snapshots
        ORDER BY account, ts DESC
        """,
    )


def snapshot_portfolio(conn) -> dict:
    """Single source of truth: FIFO-merged positions + latest prices + cash."""
    lot_rows = fetch_all(
        conn,
        """
        SELECT id, symbol, account, side, trade_date, quantity, price, fees
        FROM lots
        ORDER BY trade_date, id
        """,
    )
    fifo_df = compute_fifo_merged(lot_rows)
    prices = _latest_prices(conn)
    cash_rows = _latest_cash(conn)

    positions: list[PositionView] = []
    total_mv = Decimal("0")
    total_cost = Decimal("0")

    for _, row in fifo_df.iterrows():
        sym = row["symbol"]
        qty = Decimal(str(row["qty"]))
        open_cost = Decimal(str(row["open_cost"]))
        avg_cost = Decimal(str(row["avg_cost"]))
        realized = Decimal(str(row["realized_pnl"]))

        snap = prices.get(sym)
        last = Decimal(str(snap["last_price"])) if snap and snap["last_price"] is not None else None
        last_ts = snap["ts"] if snap else None

        mv = (last * qty) if last is not None else None
        unrealized = (mv - open_cost) if mv is not None else None

        if mv is not None:
            total_mv += mv
        total_cost += open_cost

        positions.append(
            PositionView(
                symbol=sym,
                qty=qty,
                open_cost=open_cost,
                avg_cost=avg_cost,
                realized=realized,
                last_price=last,
                last_ts=last_ts,
                market_value=mv,
                unrealized=unrealized,
            )
        )

    cash_total = sum((Decimal(str(r["cash"])) for r in cash_rows), Decimal("0"))
    nav = total_mv + cash_total

    def _pct(p: PositionView) -> float | None:
        if p.market_value is None or nav == 0:
            return None
        return float(p.market_value / nav * 100)

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "nav": float(nav),
        "total_market_value": float(total_mv),
        "total_cost": float(total_cost),
        "total_unrealized": float(total_mv - total_cost) if total_mv else 0.0,
        "total_realized": float(sum((p.realized for p in positions), Decimal("0"))),
        "cash_total": float(cash_total),
        "cash_by_account": [
            {
                "account": r["account"],
                "cash": float(r["cash"]),
                "ts": r["ts"].isoformat() if r["ts"] else None,
                "note": r["note"],
            }
            for r in cash_rows
        ],
        "positions": [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_cost": float(p.avg_cost),
                "open_cost": float(p.open_cost),
                "last_price": float(p.last_price) if p.last_price is not None else None,
                "last_ts": p.last_ts.isoformat() if p.last_ts else None,
                "market_value": float(p.market_value) if p.market_value is not None else None,
                "unrealized": float(p.unrealized) if p.unrealized is not None else None,
                "realized": float(p.realized),
                "weight_pct": _pct(p),
            }
            for p in positions
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — chat history
# ─────────────────────────────────────────────────────────────────────────────

def recent_chat(conn, n: int = 30, conversation_id: str = "default") -> list[dict]:
    rows = fetch_all(
        conn,
        """
        SELECT role, content FROM chat_log
        WHERE conversation_id = %s
        ORDER BY ts DESC, id DESC
        LIMIT %s
        """,
        (conversation_id, n),
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def save_chat(conn, role: str, content: str, conversation_id: str = "default") -> None:
    execute(
        conn,
        "INSERT INTO chat_log(role, content, conversation_id) VALUES (%s, %s, %s)",
        (role, content, conversation_id),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt assembly + Claude call
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_HEADER = """You are the operator's personal portfolio advisor speaking directly to them.
You are NOT a licensed financial advisor — frame trade-offs, never make blanket recommendations.

Treat the OPERATOR PHILOSOPHY below as governing law. Every suggestion must be checked against it,
and you must name the rule invoked whenever you flag something.

Past conversations are reference, not commitments. Re-evaluate current state independently — do not
assume yesterday's plan is still active.

Style: direct, no preamble, no flattery. Cite specific numbers from the snapshot when relevant.
Output markdown unless a structured response is explicitly requested.""".strip()


BRIEF_INSTRUCTIONS = """Generate the operator's portfolio brief for today.

Return ONLY a JSON object matching this shape (no prose outside the JSON):

{
  "summary": "<2-3 sentences on portfolio state right now>",
  "insights": [
    {"title": "<short>", "body": "<1-3 sentences>", "tag": "concentration|tax|thesis|risk|cash|pnl"}
  ],
  "suggestions": [
    {"action": "<imperative>", "rationale": "<why>", "rule_invoked": "<philosophy rule or null>"}
  ],
  "markdown": "<full brief, ~10-20 lines, suitable for display or push notification>"
}

Aim for 3-5 insights and 0-3 suggestions. The suggestions list may be empty —
'boredom is alpha' applies. The markdown field is what the operator actually reads;
the rest is for the dashboard.
""".strip()


def _build_system_blocks(snapshot: dict) -> list[dict]:
    """System prompt as content blocks — philosophy cached, snapshot fresh each call."""
    return [
        {"type": "text", "text": SYSTEM_HEADER},
        {
            "type": "text",
            "text": "# OPERATOR PHILOSOPHY\n\n" + load_philosophy(),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": "# PORTFOLIO SNAPSHOT (live)\n```json\n"
            + json.dumps(snapshot, indent=2)
            + "\n```",
        },
    ]


def parse_brief_text(text: str) -> dict:
    """Model output → brief payload, degrading gracefully.

    Claude-quality models return the requested JSON; weaker/local models may
    wrap it in a code fence or produce prose. Never error: an unparseable
    brief is stored raw in the markdown field (which is what the operator
    reads anyway) and flagged with parse_error so the UI can say so.
    """
    text = text.strip()
    # Tolerate the model wrapping JSON in a code fence.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise json.JSONDecodeError("not an object", text, 0)
    except json.JSONDecodeError:
        return {
            "summary": f"Model ({llm.provider()}: {llm.model()}) returned non-JSON output; raw text kept below.",
            "insights": [],
            "suggestions": [],
            "markdown": text,
            "parse_error": True,
        }

    # Missing keys from a weaker model degrade to empty rather than KeyError
    # downstream (the dashboard indexes into these).
    payload.setdefault("summary", "")
    payload.setdefault("insights", [])
    payload.setdefault("suggestions", [])
    payload.setdefault("markdown", text)
    return payload


def morning_brief(kind: str = "morning", conversation_id: str = "default") -> dict:
    """One-shot structured brief. Persisted to advisor_briefs."""
    cfg = load_config()
    with connect(cfg) as conn:
        snap = snapshot_portfolio(conn)
        history = recent_chat(conn, n=10, conversation_id=conversation_id)

        # max_tokens also covers models with adaptive thinking (thinking +
        # response), so leave headroom for the JSON payload.
        text = llm.complete(
            _build_system_blocks(snap),
            history + [{"role": "user", "content": BRIEF_INSTRUCTIONS}],
            max_tokens=8000,
        )
        payload = parse_brief_text(text)

        execute(
            conn,
            """
            INSERT INTO advisor_briefs(kind, total_value, payload)
            VALUES (%s, %s, %s::jsonb)
            """,
            (kind, snap["nav"], json.dumps(payload)),
        )
        return payload


def chat(question: str, conversation_id: str = "default") -> Iterator[str]:
    """Stream a chat response, saving both sides to chat_log."""
    cfg = load_config()
    with connect(cfg) as conn:
        snap = snapshot_portfolio(conn)
        history = recent_chat(conn, n=30, conversation_id=conversation_id)
        save_chat(conn, "user", question, conversation_id)

        chunks: list[str] = []
        for text in llm.stream(
            _build_system_blocks(snap),
            history + [{"role": "user", "content": question}],
            max_tokens=8000,
        ):
            chunks.append(text)
            yield text

        save_chat(conn, "assistant", "".join(chunks), conversation_id)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _advisor_disabled_reason() -> str | None:
    """Why the advisor can't run, or None if it can.

    Checked in the CLI (not in morning_brief/chat) so the library still raises
    for callers that want the exception — the dashboard shows its own key
    status. The scheduled brief, though, must not fail a job every morning on
    an install that never configured an LLM.
    """
    status = llm.key_status()
    if status["set"] or status["optional"]:
        return None
    return (
        f"no API key for provider '{status['provider']}' — set "
        f"{status['env_var']} in .env (see docs/llm-providers.md)"
    )


def _cmd_brief(args) -> int:
    reason = _advisor_disabled_reason()
    if reason:
        print(f"Skipped: advisor {reason}.")
        return 0
    payload = morning_brief(kind=args.kind)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(payload.get("markdown") or json.dumps(payload, indent=2))
    return 0


def _cmd_ask(args) -> int:
    reason = _advisor_disabled_reason()
    if reason:
        print(f"Cannot ask: advisor {reason}.")
        return 1  # interactive use — a silent no-op would be confusing
    for tok in chat(args.question, conversation_id=args.conversation):
        sys.stdout.write(tok)
        sys.stdout.flush()
    sys.stdout.write("\n")
    return 0


def _cmd_snapshot(args) -> int:
    cfg = load_config()
    with connect(cfg) as conn:
        snap = snapshot_portfolio(conn)
    print(json.dumps(snap, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows console defaults to cp1252; force UTF-8 so model output prints cleanly.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="PortfolioDB advisor")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("brief", help="Generate a structured brief")
    b.add_argument("--kind", choices=("morning", "eod", "adhoc"), default="morning")
    b.add_argument("--json", action="store_true", help="Print full JSON payload")
    b.set_defaults(func=_cmd_brief)

    a = sub.add_parser("ask", help="Ask the advisor a question (streams)")
    a.add_argument("question")
    a.add_argument("--conversation", default="default")
    a.set_defaults(func=_cmd_ask)

    s = sub.add_parser("snapshot", help="Dump the portfolio snapshot the advisor sees")
    s.set_defaults(func=_cmd_snapshot)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
