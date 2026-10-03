"""Turn summaries on /log rows (docs/live-items.md §9, feature `log_turn_summary`).

Every row a turn writes carries the turn's `trace_id`; when the turn settles,
its last row carries `turn` (the shape GET /live reports) and tools the turn
left unfinished read `interrupted`. Outcomes are recorded on the `turns` row
by the first terminal state change (turn_lifecycle), from whichever process
records it; message_writes re-stamps the rows then and after every import,
with revision bumps so delta clients see the change.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
from typing import Any


OUTCOME_BY_EVENT = {
    "hook_stop": "completed", "process_exited_ok": "completed",
    "stop_requested": "interrupted", "restart_interrupted": "interrupted",
    "reconcile_repair": "interrupted", "agent_deleted": "interrupted",
    "process_exited_failed": "failed", "turn_failed_unclassified": "failed",
    "unlaunched": "failed",
}
OUTCOME_BY_KIND = {"done": "completed", "interrupted": "interrupted", "stopped": "interrupted"}
UNFINISHED = frozenset({"running", "recorded", "pending", "in_progress"})
TOOL_CELL_KINDS = frozenset({"command", "patch", "mcp", "web_search"})
# A row is attributed to the latest turn started before it, at most this long
# after the turn started (turns recorded before outcomes were kept).
_UNSETTLED_REACH_MS = 6 * 3600 * 1000
_SETTLED_REACH_MS = 120_000
_START_SLACK_MS = 2_000


def outcome_for(event: str, kind: str) -> str | None:
    if event == "recorded":
        return OUTCOME_BY_KIND.get(kind)
    return OUTCOME_BY_EVENT.get(event)


def iso_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.isdigit():
            number = int(text)
            return number if number > 10**11 else number * 1000
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


class TurnIndex:
    """An agent's turns by start time, to attribute rows that no prompt names."""

    def __init__(self, database, agent_id: str):
        rows = database.execute(
            """SELECT trace_id, started_at, settled_at, outcome FROM turns
                WHERE agent_id = ? ORDER BY started_at""", (agent_id,)).fetchall()
        self._starts = [int(r["started_at"]) for r in rows]
        self._rows = rows
        self.outcomes = {str(r["trace_id"]): r["outcome"] for r in rows if r["trace_id"]}

    def trace_at(self, timestamp: Any) -> str:
        ms = iso_ms(timestamp)
        if ms is None or not self._starts:
            return ""
        index = bisect.bisect_right(self._starts, ms + _START_SLACK_MS) - 1
        if index < 0:
            return ""
        row = self._rows[index]
        settled = row["settled_at"]
        reach = (int(settled) + _SETTLED_REACH_MS) if settled else \
            (int(row["started_at"]) + _UNSETTLED_REACH_MS)
        return str(row["trace_id"] or "") if ms <= reach else ""


def settle_statuses(items: list, outcome: str | None) -> list:
    """Tools and cells an interrupted turn left unfinished read `interrupted`."""
    if outcome != "interrupted" or not isinstance(items, list):
        return items
    out = []
    for item in items:
        if isinstance(item, dict) and str(item.get("status") or "") in UNFINISHED:
            item = {**item, "status": "interrupted"}
        out.append(item)
    return out


def tool_count(rows) -> int:
    count = 0
    for row in rows:
        try:
            count += len(json.loads(row["tools_json"] or "[]"))
            count += sum(1 for cell in json.loads(row["display_cells_json"] or "[]")
                         if isinstance(cell, dict) and cell.get("kind") in TOOL_CELL_KINDS)
        except (ValueError, TypeError):
            continue
    return count
