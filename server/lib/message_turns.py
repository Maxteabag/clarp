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
    "call_opening_unconfirmed": "failed",
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
        self._set_rows(self.read_rows(database, agent_id))

    @staticmethod
    def read_rows(database, agent_id: str):
        return database.execute(
            """SELECT trace_id, started_at, settled_at, outcome FROM turns
                WHERE agent_id = ? ORDER BY started_at""", (agent_id,)).fetchall()

    @classmethod
    def from_rows(cls, rows):
        """Rebuild from the raw rows already read during import validation."""
        index = cls.__new__(cls)
        index._set_rows(rows)
        return index

    def _set_rows(self, rows):
        self._starts = [int(r["started_at"]) for r in rows]
        self._rows = rows
        self.outcomes = {str(r["trace_id"]): r["outcome"] for r in rows if r["trace_id"]}
        self._spans = None

    def ended_before(self, trace: str, timestamp: Any) -> bool:
        """Whether the turn of ``trace`` had ended before ``timestamp``: at the
        latest settle among its rows, or, when none of its rows settled, when
        the agent's next turn of another trace began (an agent runs one turn at
        a time); never later than that next start. False for a trace with no
        turn (a message steered into another turn), a turn still running, or a
        time not known."""
        if self._spans is None:
            self._spans = self._turn_spans()
        span = self._spans.get(trace)
        ms = iso_ms(timestamp)
        return span is not None and ms is not None and span[1] is not None and ms > span[1]

    def began_after(self, trace: str, timestamp: Any) -> bool:
        """Whether the first turn of ``trace`` began more than _START_SLACK_MS
        after ``timestamp``; False for a trace with no turn or a time not known."""
        if self._spans is None:
            self._spans = self._turn_spans()
        span = self._spans.get(trace)
        ms = iso_ms(timestamp)
        return span is not None and ms is not None and span[0] > ms + _START_SLACK_MS

    def _turn_spans(self) -> dict[str, tuple[int, int | None]]:
        first: dict[str, int] = {}
        last: dict[str, int] = {}
        settled: dict[str, int | None] = {}
        for row in self._rows:
            trace = str(row["trace_id"] or "")
            if not trace:
                continue
            start = int(row["started_at"])
            first[trace] = min(first.get(trace, start), start)
            last[trace] = max(last.get(trace, start), start)
            if row["settled_at"] is not None:
                # The latest settle among the trace's rows ends it: a twin row that never
                # settled does not keep a turn running that a settle says finished.
                done = int(row["settled_at"])
                settled[trace] = max(settled.get(trace) or done, done)
            else:
                settled.setdefault(trace, None)
        spans = {}
        for trace, began in first.items():
            later = bisect.bisect_right(self._starts, last[trace])
            cut = next((int(row["started_at"]) for row in self._rows[later:]
                        if str(row["trace_id"] or "") != trace), None)
            end = settled[trace]
            if cut is not None:
                end = cut if end is None else min(end, cut)
            spans[trace] = (began, end)
        return spans

    def trace_at(self, timestamp: Any) -> str:
        return self.running_at(timestamp)[0]

    def running_at(self, timestamp: Any) -> tuple[str, int | None]:
        """The turn that was running at ``timestamp``: its trace and when it
        began (ms); ("", None) when there was none."""
        ms = iso_ms(timestamp)
        if ms is None or not self._starts:
            return "", None
        index = bisect.bisect_right(self._starts, ms + _START_SLACK_MS) - 1
        if index < 0:
            return "", None
        row = self._rows[index]
        settled = row["settled_at"]
        reach = (int(settled) + _SETTLED_REACH_MS) if settled else \
            (int(row["started_at"]) + _UNSETTLED_REACH_MS)
        if ms > reach or not row["trace_id"]:
            return "", None
        return str(row["trace_id"]), int(row["started_at"])


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
