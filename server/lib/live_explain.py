"""Plain-language explanations on live tool items (docs/live-items.md §6).

Runs in the HTTP process, where the explainer lives. It watches the live
events going out, and for each new tool item (or a tool whose command
changed) asks the explainer at the Host's level, then patches the result
into the item through the hub (``patch``: the runtime's ``live_patch`` RPC,
or the local hub). With the Host setting off it does nothing at all.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from . import tool_explanation_settings

MAX_WAIT_SEC = 45.0


class LiveExplainer:
    def __init__(self, service: Callable[[], Any],
                 patch: Callable[[str, str, dict[str, Any]], Any], *,
                 poll_interval: float = 0.7, synchronous: bool = False):
        self._service = service
        self._patch = patch
        self._poll = poll_interval
        self._synchronous = synchronous
        self._seen: dict[str, str] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=256)
        self._thread: threading.Thread | None = None

    def observe(self, event: dict[str, Any]) -> None:
        if event.get("type") != "live":
            return
        setting = tool_explanation_settings.get()
        level = int(setting["detail_level"])
        if not setting["enabled"] or level == 0:
            return
        for op in event.get("ops") or []:
            if op.get("op") != "upsert" or op.get("kind") != "tool":
                continue
            tool = (op.get("item") or {}).get("tool")
            if not isinstance(tool, dict) or not (tool.get("name") or tool.get("command")):
                continue
            key = f"{tool.get('name')}\0{tool.get('command') or tool.get('label') or ''}"
            item_id = str(op.get("id") or "")
            with self._lock:
                if self._seen.get(item_id) == key:
                    continue
                self._seen[item_id] = key
                if len(self._seen) > 4096:
                    self._seen.pop(next(iter(self._seen)))
            job = (str(event.get("agent_id") or ""), item_id, tool)
            # Announce at admission, not after an earlier tool's 45-second
            # polling window. Clients can show an honest explanation wait
            # rather than silently falling back to raw command text.
            self._patch(job[0], item_id, {"tool": {"explain": {
                "text": None, "level": level, "status": "pending"}}})
            if self._synchronous:
                self._explain(*job)
            else:
                self._start()
                try:
                    self._queue.put_nowait(job)
                except queue.Full:
                    self._patch(job[0], item_id, {"tool": {"explain": {
                        "text": None, "level": level, "status": "failed"}}})

    def _start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="live-explainer")
            self._thread.start()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                self._explain(*job)
            except Exception as exc:  # noqa: BLE001 - a presentation extra
                from .log import log_exception
                log_exception("liveExplainFail", exc)

    def _explain(self, agent_id: str, item_id: str, tool: dict[str, Any]) -> None:
        setting = tool_explanation_settings.get()
        level = int(setting["detail_level"])
        if not setting["enabled"] or level == 0 or not agent_id:
            return
        service = self._service()
        if service is None:
            self._patch(agent_id, item_id, {"tool": {"explain": {
                "text": None, "level": level, "status": "failed"}}})
            return
        preview = tool.get("input_preview") if isinstance(tool.get("input_preview"), dict) else {}
        activity = {"name": tool.get("name") or "", "command": tool.get("command") or "",
                    "input": preview, "title": tool.get("label") or ""}
        for key in ("file_path", "path", "pattern"):
            if preview.get(key):
                activity[key] = preview[key]
        from . import agents as agents_db
        agent = agents_db.get_by_agent_id(agent_id) or {}
        request_id = item_id[-128:]
        deadline = time.monotonic() + MAX_WAIT_SEC
        while True:
            result = service.request(level, [{"id": request_id, "activity": activity}],
                                     cwd=agent.get("cwd"), target_agent_id=agent_id)
            answer = next((item for item in result.get("items") or []
                           if item.get("id") == request_id), {})
            status = answer.get("status")
            if status == "ready" and answer.get("text"):
                self._patch(agent_id, item_id, {"tool": {"explain": {
                    "text": answer["text"], "level": level, "status": "ready"}}})
                return
            if status not in {"pending", "busy"} or time.monotonic() >= deadline:
                self._patch(agent_id, item_id, {"tool": {"explain": {
                    "text": None, "level": level, "status": "failed"}}})
                return
            if self._poll:
                time.sleep(self._poll)
