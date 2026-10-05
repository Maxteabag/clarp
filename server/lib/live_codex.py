"""Codex app-server notifications -> live items (docs/live-items.md §2).

Tool start/finish reach the hub through the state machine; this adds what
only the app-server stream carries: reasoning summaries as they are written,
command output as it is printed, file-change diffs, and message phases.
Items are ``cx:<item id>`` (a tool's item id is its call id).
"""
from __future__ import annotations

from typing import Any

from . import live_hub
from .live_tools import diff_stats, output_tail

_PHASES = {"final_answer": "final", "final": "final", "commentary": "commentary"}


class CodexLiveItems:
    def __init__(self, agent_id: str, trace_id: str = "") -> None:
        self.agent_id = agent_id
        self.trace_id = trace_id
        self._reasoning: dict[str, str] = {}
        self._partial: dict[str, str] = {}
        self._lines: dict[str, int] = {}
        self._ready = False

    def on_notification(self, method: str, params: dict[str, Any]) -> None:
        hub = live_hub.current()
        if hub is None or not self.agent_id:
            return
        try:
            if not self._ready:
                self._ready = live_hub.ensure_turn(hub, self.agent_id, trace=self.trace_id) is not None
            if self._ready:
                self._handle(hub, method, params)
        except Exception as exc:  # noqa: BLE001 - live items never break a turn
            from .log import log_exception
            log_exception("liveCodexFail", exc, detail=method)

    def _handle(self, hub, method: str, params: dict[str, Any]) -> None:
        item_id = str(params.get("itemId") or "")
        if method == "item/reasoning/summaryTextDelta" and item_id:
            text = self._reasoning.get(item_id, "") + str(params.get("delta") or "")
            self._reasoning[item_id] = text
            hub.reasoning_text(self.agent_id, f"cx:{item_id}", text, turn_id=self.trace_id or None)
        elif method == "item/reasoning/summaryPartAdded" and item_id:
            if self._reasoning.get(item_id):
                self._reasoning[item_id] += "\n\n"
        elif method == "item/commandExecution/outputDelta" and item_id:
            buffer = self._partial.get(item_id, "") + str(params.get("delta") or "")
            *complete, rest = buffer.split("\n")
            self._partial[item_id] = rest
            if complete:
                self._lines[item_id] = self._lines.get(item_id, 0) + len(complete)
                hub.tool_output(self.agent_id, f"cx:{item_id}", complete,
                                total_lines=self._lines[item_id], turn_id=self.trace_id or None)
        elif method in ("item/started", "item/completed"):
            item = params.get("item")
            if isinstance(item, dict):
                self._item(hub, method == "item/completed", item)

    def _item(self, hub, completed: bool, item: dict[str, Any]) -> None:
        kind = str(item.get("type") or "")
        raw_id = str(item.get("id") or "")
        if not raw_id:
            return
        item_id = f"cx:{raw_id}"
        if kind == "reasoning":
            summary = item.get("summary")
            if isinstance(summary, list) and summary:
                text = "\n\n".join(str(part.get("text") if isinstance(part, dict) else part)
                                   for part in summary)
                self._reasoning[raw_id] = text
                hub.reasoning_text(self.agent_id, item_id, text, turn_id=self.trace_id or None)
            elif not hub.has_item(self.agent_id, item_id):
                hub.reasoning_text(self.agent_id, item_id, self._reasoning.get(raw_id, ""), turn_id=self.trace_id or None)
            if completed:
                hub.done(self.agent_id, item_id, turn_id=self.trace_id or None)
        elif kind == "commandExecution" and completed:
            exit_code = item.get("exitCode")
            output = item.get("aggregatedOutput")
            fields: dict[str, Any] = {"exit_code": exit_code if isinstance(exit_code, int) else None}
            if isinstance(output, str):
                tail, total = output_tail(output)
                fields.update(tail=tail, total_lines=total, truncated=total > len(tail))
            hub.patch(self.agent_id, item_id, {"tool": {"output": fields}}, turn_id=self.trace_id or None)
        elif kind == "fileChange":
            changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
            files, added, removed, preview = [], 0, 0, []
            for change in changes:
                stats = diff_stats("patch", {"diff": str(change.get("diff") or ""),
                                             "path": str(change.get("path") or "")})
                if stats is None:
                    continue
                files.extend(stats["files"])
                added += stats["added"]
                removed += stats["removed"]
                if stats["preview"]:
                    preview.append(stats["preview"])
            if files:
                lines = "\n".join(preview).splitlines()[:40]
                hub.patch(self.agent_id, item_id, {"tool": {"diff": {
                    "added": added, "removed": removed, "files": files,
                    "preview": "\n".join(lines)}}}, turn_id=self.trace_id or None)
        elif kind == "agentMessage" and item.get("phase"):
            phase = _PHASES.get(str(item["phase"]))
            if phase and hub.has_item(self.agent_id, item_id):
                hub.patch(self.agent_id, item_id, {"phase": phase}, turn_id=self.trace_id or None)
