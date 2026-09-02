"""Per-turn token and cost accounting.

Every backend CLI ends a turn with a stream-json `result` event carrying its
own usage numbers. turn_dispatch already parses those to build the DONE state
detail; this module persists them so the usage view can be computed locally.

Why this and not Claude Code's statusline: a statusline is a UI renderer and
does not run under `-p`, which is how every dispatched turn is launched. The
statusline source therefore reported only the host's interactive terminal use
and nothing Clarp itself ran — and it was unavailable in containers entirely.
The `result` event is part of the documented stream-json contract and arrives
for every turn, in every mode, on every backend that reports usage.

What this cannot do: express a percentage of a provider quota. No CLI exposes
the remaining allowance. This is spend, not headroom.
"""
from __future__ import annotations

from typing import Any

from .db import conn, now_ms

# Rolling windows the usage view reports on. Named after the provider windows
# they approximate so the UI language stays familiar, but they are plain
# rolling periods over locally recorded turns — not provider quota windows.
WINDOWS: dict[str, int] = {
    "five_hour": 5 * 60 * 60 * 1000,
    "seven_day": 7 * 24 * 60 * 60 * 1000,
}


def record(*, backend: str, agent_id: str, detail: dict[str, Any],
           trace_id: str = "") -> None:
    """Persist one turn's usage. Silently ignores a turn that reported none —
    agy sends an empty usage dict, and a failed turn may send nothing.
    """
    tokens_in = detail.get("tokens_in")
    tokens_out = detail.get("tokens_out")
    cost = detail.get("cost_usd")
    if tokens_in is None and tokens_out is None and cost is None:
        return
    conn().execute(
        """INSERT INTO turn_usage
               (backend, agent_id, trace_id, tokens_in, tokens_out,
                cost_usd, duration_ms, at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (backend, agent_id, trace_id or detail.get("trace_id") or "",
         int(tokens_in or 0), int(tokens_out or 0),
         float(cost) if cost is not None else None,
         int(detail["duration_ms"]) if detail.get("duration_ms") is not None else None,
         now_ms()),
    )


def totals(backend: str, *, now: int | None = None) -> dict[str, Any]:
    """Rolling token/cost totals for one backend, plus the newest turn's time.

    `last_turn_at` is what freshness is judged on: it is the age of the data,
    not the age of a poll.
    """
    now = now_ms() if now is None else now
    out: dict[str, Any] = {"windows": {}, "last_turn_at": None}
    for name, span_ms in WINDOWS.items():
        row = conn().execute(
            """SELECT COUNT(*) AS turns,
                      COALESCE(SUM(tokens_in), 0)  AS tokens_in,
                      COALESCE(SUM(tokens_out), 0) AS tokens_out,
                      SUM(cost_usd)                AS cost_usd
                 FROM turn_usage
                WHERE backend = ? AND at > ?""",
            (backend, now - span_ms),
        ).fetchone()
        out["windows"][name] = {
            "turns": int(row["turns"] or 0),
            "tokens_in": int(row["tokens_in"] or 0),
            "tokens_out": int(row["tokens_out"] or 0),
            "cost_usd": float(row["cost_usd"]) if row["cost_usd"] is not None else None,
            "window_minutes": span_ms // 60000,
        }
    newest = conn().execute(
        "SELECT MAX(at) AS at FROM turn_usage WHERE backend = ?", (backend,)
    ).fetchone()
    out["last_turn_at"] = int(newest["at"]) if newest and newest["at"] else None
    return out


def prune_old(*, max_age_ms: int) -> int:
    """Drop rows older than the longest window we report on. Called by the
    maintenance worker so the table stays bounded."""
    cur = conn().execute("DELETE FROM turn_usage WHERE at < ?",
                         (now_ms() - max_age_ms,))
    return int(cur.rowcount or 0)
