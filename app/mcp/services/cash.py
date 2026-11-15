"""Cash at a cutoff — the one reader every MCP surface uses.

Cash is entered by hand, one balance per account; the latest row per account
at or before the cutoff is that account's cash. Three services used to carry
their own copy of this query (kpis, review, activity).

USD only (audit Q-8, docs/methodology.md §14): a balance entered in another
currency is not added to the dollar total. It is still listed per account, with
``included: false``, and described in ``excluded_non_usd`` — the warning shape
app/portfolio_kpis.currency_warnings uses on the dashboard.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import portfolio_kpis

from app.mcp.deps import get_conn


def latest_cash(
    as_of_ts: datetime | None = None, *, account: str | None = None
) -> dict[str, Any]:
    """{total, by_account, excluded_non_usd} at ``as_of_ts`` (None = now).

    ``account`` keeps one account; '(none)' matches nothing, because a cash
    balance always names an account (cash_snapshots.account is NOT NULL).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Latest row per account — a global ORDER BY ts LIMIT N would drop
            # accounts whose newest snapshot falls outside the N rows.
            cur.execute(
                """
                SELECT DISTINCT ON (account) account, cash, ts, currency
                FROM cash_snapshots
                WHERE (%s::timestamptz IS NULL OR ts <= %s)
                ORDER BY account, ts DESC
                """,
                (as_of_ts, as_of_ts),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    by_account: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    total = 0.0
    seen: set[str] = set()
    for r in rows:
        acct = r.get("account") or "(merged)"
        if account is not None and acct != account:
            continue
        if acct in seen:  # first (newest) row per account wins
            continue
        seen.add(acct)
        currency = portfolio_kpis.currency_of(r)
        included = currency == portfolio_kpis.BASE_CURRENCY
        amount = float(r["cash"])
        ts = r.get("ts")
        by_account.append({
            "account": acct,
            "cash": amount,
            "currency": currency,
            "included": included,
            "ts": ts.isoformat() if ts else None,
        })
        if included:
            total += amount
        else:
            other.append({"account": acct, "currency": currency, "cash": amount})
    return {
        "total": total,
        "by_account": by_account,
        "excluded_non_usd": portfolio_kpis.currency_warnings(cash=other),
    }
