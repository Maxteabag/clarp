"""Claude stream-json events -> live items (docs/live-items.md §2).

Claude's tool hooks run as their own processes and cannot reach the hub, so
everything a live client sees of a Claude turn comes from the stream: one
item per content block (``cl:<message.id>:<index>`` for thinking and text,
``cl:<toolu id>`` for a tool), tool results from the ``user`` records that
follow, and edit diffs from the tool input.
"""
from __future__ import annotations

import json
from typing import Any

from . import live_hub
from .live_tools import classify_tool, diff_stats, output_tail


class ClaudeLiveItems:
    def __init__(self, *, agent_id: str, trace_id: str, row_id) -> None:
        self.agent_id = agent_id
        self.trace_id = trace_id
        self._row_id = row_id            # message.id -> the live row it streams into
        self._message = ""
        self._blocks: dict[int, tuple[str, str]] = {}   # index -> (kind, item id)
        self._texts: dict[str, str] = {}
        self._inputs: dict[int, str] = {}
        self._tools: dict[str, str] = {}                # toolu id -> tool name
        self._turn_checked = False

    def on_event(self, ev: dict[str, Any]) -> None:
        hub = live_hub.current()
        if hub is None or not self.agent_id:
            return
        try:
            self._on_event(hub, ev)
        except Exception as exc:  # noqa: BLE001 - live items never break a turn
            from .log import log_exception
            log_exception("liveClaudeFail", exc, detail=self.trace_id)

    def _ready(self, hub) -> bool:
        if not self._turn_checked:
            self._turn_checked = live_hub.ensure_turn(hub, self.agent_id, trace=self.trace_id) is not None
        return self._turn_checked

    def _on_event(self, hub, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        if kind == "user":
            self._tool_results(hub, ev)
            return
        if kind != "stream_event":
            return
        inner = ev.get("event")
        if not isinstance(inner, dict) or not self._ready(hub):
            return
        inner_type = inner.get("type")
        index = inner.get("index")
        if inner_type == "message_start":
            message = inner.get("message")
            self._message = str(message.get("id") or "") if isinstance(message, dict) else ""
            self._blocks = {}
            self._inputs = {}
            return
        if inner_type == "message_stop":
            for kind, item_id in self._blocks.values():
                if kind in {"reasoning", "message"}:
                    hub.done(self.agent_id, item_id, turn_id=self.trace_id or None)
            return
        if inner_type == "content_block_start" and isinstance(index, int):
            self._block_start(hub, index, inner.get("content_block") or {})
        elif inner_type == "content_block_delta" and isinstance(index, int):
            self._block_delta(hub, index, inner.get("delta") or {})
        elif inner_type == "content_block_stop" and isinstance(index, int):
            self._block_stop(hub, index)

    def _block_start(self, hub, index: int, block: dict[str, Any]) -> None:
        block_type = block.get("type")
        if block_type in {"thinking", "redacted_thinking"}:
            item_id = f"cl:{self._message}:{index}"
            self._blocks[index] = ("reasoning", item_id)
            self._texts[item_id] = str(block.get("thinking") or "")
            hub.reasoning_text(self.agent_id, item_id, self._texts[item_id], turn_id=self.trace_id or None)
        elif block_type == "text":
            item_id = f"cl:{self._message}:{index}"
            self._blocks[index] = ("message", item_id)
            self._texts[item_id] = str(block.get("text") or "")
            hub.message_text(self.agent_id, item_id, self._texts[item_id], phase="final",
                             row_id=self._row_id(self._message), turn_id=self.trace_id or None)
        elif block_type in {"tool_use", "server_tool_use"} and block.get("id"):
            call_id = str(block["id"])
            name = str(block.get("name") or "tool")
            item_id = f"cl:{call_id}"
            self._blocks[index] = ("tool", item_id)
            self._tools[call_id] = name
            self._inputs[index] = ""
            category, label, command = classify_tool(name, block.get("input") or {})
            hub.tool_start(self.agent_id, item_id, name=name, call_id=call_id,
                           category=category, label=label, command=command, turn_id=self.trace_id or None)

    def _block_delta(self, hub, index: int, delta: dict[str, Any]) -> None:
        kind, item_id = self._blocks.get(index, ("", ""))
        delta_type = delta.get("type")
        if kind == "reasoning" and delta_type == "thinking_delta":
            self._texts[item_id] += str(delta.get("thinking") or "")
            hub.reasoning_text(self.agent_id, item_id, self._texts[item_id], turn_id=self.trace_id or None)
        elif kind == "message" and delta_type == "text_delta":
            self._texts[item_id] += str(delta.get("text") or "")
            hub.message_text(self.agent_id, item_id, self._texts[item_id], turn_id=self.trace_id or None)
        elif kind == "tool" and delta_type == "input_json_delta":
            self._inputs[index] = self._inputs.get(index, "") + str(delta.get("partial_json") or "")

    def _block_stop(self, hub, index: int) -> None:
        kind, item_id = self._blocks.get(index, ("", ""))
        if kind in {"reasoning", "message"}:
            hub.done(self.agent_id, item_id, turn_id=self.trace_id or None)
        elif kind == "tool":
            try:
                tool_input = json.loads(self._inputs.get(index) or "{}")
            except ValueError:
                tool_input = {}
            if not isinstance(tool_input, dict):
                return
            call_id = item_id.split(":", 1)[1]
            name = self._tools.get(call_id, "tool")
            category, label, command = classify_tool(name, tool_input)
            hub.tool_start(self.agent_id, item_id, name=name, call_id=call_id,
                           category=category, label=label, command=command,
                           input_preview=live_hub._preview(tool_input), turn_id=self.trace_id or None)
            diff = diff_stats(name, tool_input)
            if diff is not None:
                hub.patch(self.agent_id, item_id, {"tool": {"diff": diff}}, turn_id=self.trace_id or None)

    def _tool_results(self, hub, ev: dict[str, Any]) -> None:
        message = ev.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "tool_result":
                continue
            item_id = f"cl:{part.get('tool_use_id') or ''}"
            if not hub.has_item(self.agent_id, item_id):
                continue
            tail, total = output_tail(part.get("content"))
            if tail:
                hub.tool_output(self.agent_id, item_id, tail, total_lines=total, turn_id=self.trace_id or None)
            hub.done(self.agent_id, item_id,
                     status="failed" if part.get("is_error") else "completed",
                     patch={"tool": {"output": {"exit_code": 1 if part.get("is_error") else 0}}}, turn_id=self.trace_id or None)
