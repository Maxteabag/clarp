"""Live items: the open turn as items pushed to clients as ops.

docs/live-items.md is the contract. ``LiveView`` is the reference reducer a
client runs over ``live`` events and ``GET /live`` snapshots; the recorded
streams in contract/live/ are checked against it, and the Host keeps one per
conversation to answer ``GET /live``.
"""
from __future__ import annotations

import copy
from typing import Any

OUTPUT_TAIL_LINES = 50
TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})
FETCH_LIVE = "fetch_live"


def merge_patch(target: dict[str, Any], patch: dict[str, Any]) -> None:
    """Fields absent stay, ``None`` clears, objects merge, other values replace."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_patch(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def append_output(item: dict[str, Any], lines: list[str], total_lines: int) -> None:
    tool = item.setdefault("tool", {})
    output = tool.get("output")
    if not isinstance(output, dict):
        output = tool["output"] = {"tail": [], "total_lines": 0, "truncated": False, "exit_code": None}
    tail = (list(output.get("tail") or []) + list(lines))[-OUTPUT_TAIL_LINES:]
    output["tail"] = tail
    output["total_lines"] = total_lines
    output["truncated"] = total_lines > len(tail)


class LiveView:
    """One conversation's live state, built from snapshots and events."""

    def __init__(self) -> None:
        self.epoch: str | None = None
        self.lseq: int | None = None
        self.activity: dict[str, Any] = {"state": "idle"}
        self.turn: dict[str, Any] | None = None
        self._items: dict[str, dict[str, Any]] = {}
        self.awaiting_snapshot = False

    def items(self) -> list[dict[str, Any]]:
        return sorted(self._items.values(), key=lambda item: item.get("ordinal", 0))

    def apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.epoch = snapshot["epoch"]
        self.lseq = int(snapshot["lseq"])
        self.activity = copy.deepcopy(snapshot.get("activity") or {"state": "idle"})
        self.turn = copy.deepcopy(snapshot.get("turn"))
        self._items = {item["id"]: copy.deepcopy(item) for item in snapshot.get("items") or []}
        self.awaiting_snapshot = False

    def apply_event(self, event: dict[str, Any]) -> list[str]:
        """Apply one ``live`` event. Returns the effects the client must run."""
        if self.awaiting_snapshot:
            return []
        lseq = int(event["lseq"])
        if self.lseq is None or event.get("epoch") != self.epoch:
            return self._need_snapshot()
        if lseq <= self.lseq:
            return []
        if lseq != self.lseq + 1:
            return self._need_snapshot()
        staged = LiveView()
        staged.__dict__.update(copy.deepcopy(self.__dict__))
        for op in event.get("ops") or []:
            if not staged._apply_op(op):
                return self._need_snapshot()
        staged.lseq = lseq
        self.__dict__.update(staged.__dict__)
        return []

    def _need_snapshot(self) -> list[str]:
        self.awaiting_snapshot = True
        return [FETCH_LIVE]

    def _apply_op(self, op: dict[str, Any]) -> bool:
        name = op.get("op")
        if name == "status":
            self.activity = copy.deepcopy(op.get("activity") or {"state": "idle"})
            return True
        if name == "turn":
            self.turn = copy.deepcopy(op.get("turn"))
            return True
        if name not in {"upsert", "append", "done"}:
            return True
        item_id = op.get("id")
        rev = op.get("rev")
        item = self._items.get(item_id)
        if item is None:
            if name != "upsert":
                return False
            item = {"id": item_id, "kind": op.get("kind"), "rev": 0}
            self._items[item_id] = item
        elif rev != item.get("rev", 0) + 1:
            return False
        if name == "upsert":
            merge_patch(item, op.get("item") or {})
        elif name == "append":
            field = op.get("field")
            if field == "text":
                item["text"] = str(item.get("text") or "") + str(op.get("chunk") or "")
            elif field == "tool.output":
                append_output(item, list(op.get("lines") or []), int(op.get("total_lines") or 0))
        else:
            item["status"] = op.get("status")
            for key in ("started_at_ms", "ended_at_ms"):
                if key in op:
                    item[key] = op[key]
            merge_patch(item, op.get("item") or {})
        item["rev"] = rev if isinstance(rev, int) else item.get("rev", 0) + 1
        return True
