"""Plain-language explanations on live tool items (docs/live-items.md §6).

Runs in the HTTP process, where the explainer lives. It watches the live
events going out, and for each new tool item (or a tool whose command
changed) asks the explainer at the Host's level, then patches the result
into the item through the hub (``patch``: the runtime's ``live_patch`` RPC,
or the local hub). With the Host setting off it does nothing at all.

Agents run tools faster than a model explains them one at a time, so every
round asks for an agent's items in one request (the service's batch of 8:
those already asked for until they settle, then the newest waiting) and
patches each as its answer lands. An item that
waits too long, falls off a full backlog, or outlives its turn by a grace
period settles ``failed`` with ``reason: skipped``: no item stays pending.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from . import tool_explanation_settings

MAX_WAIT_SEC = 45.0
# After its turn ends an item still gets this long, then it is skipped.
TURN_END_GRACE_SEC = 15.0
# The service takes at most 8 items per request (and claims 8 per model call).
BATCH = 8
# Per agent; admitting a newer item past this skips the oldest.
BACKLOG = 24
LOCK_BACKOFF_SEC = (0.2, 0.5, 1.0, 2.0, 4.0)


@dataclass
class _Job:
    agent_id: str
    item_id: str
    tool: dict[str, Any]
    level: int
    demand: str
    deadline: float
    asked: bool = False


def _settled(level: int, status: str, reason: str) -> dict[str, Any]:
    return {"tool": {"explain": {"text": None, "level": level, "status": status, "reason": reason}}}


class LiveExplainer:
    def __init__(self, service: Callable[[], Any],
                 patch: Callable[[str, str, dict[str, Any]], Any], *,
                 poll_interval: float = 0.7, synchronous: bool = False,
                 clock: Callable[[], float] = time.monotonic):
        self._service = service
        self._patch = patch
        self._poll = poll_interval
        self._synchronous = synchronous
        self._clock = clock
        self._seen: dict[str, str] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        # agent_id -> item_id -> job, in admission order (oldest first).
        self._jobs: dict[str, OrderedDict[str, _Job]] = {}
        self._turns: dict[str, str] = {}
        self._releases: list[str] = []
        self._locked = 0
        self._closed = False
        self._thread: threading.Thread | None = None

    def observe(self, event: dict[str, Any]) -> None:
        if event.get("type") != "live":
            return
        setting = tool_explanation_settings.get()
        level = int(setting["detail_level"])
        if not setting["enabled"] or level == 0:
            return
        agent_id = str(event.get("agent_id") or "")
        settle: list[_Job] = []
        admitted: list[_Job] = []
        for op in event.get("ops") or []:
            if op.get("op") == "turn":
                self._turn(agent_id, op.get("turn") or {})
                continue
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
                if not agent_id:
                    continue
                demand = "live-" + hashlib.sha256(f"{agent_id}\0{item_id}\0{key}".encode()).hexdigest()[:40]
                job = _Job(agent_id, item_id, tool, level, demand, self._clock() + MAX_WAIT_SEC)
                jobs = self._jobs.setdefault(agent_id, OrderedDict())
                replaced = jobs.pop(item_id, None)
                if replaced is not None:
                    self._releases.append(replaced.demand)
                jobs[item_id] = job
                while len(jobs) > BACKLOG:
                    _, oldest = jobs.popitem(last=False)
                    self._releases.append(oldest.demand)
                    settle.append(oldest)
            admitted.append(job)
        for job in admitted:
            # Announce at admission, not after earlier tools' answers. Clients
            # show an honest explanation wait rather than the raw command.
            self._patch(job.agent_id, job.item_id, {"tool": {"explain": {
                "text": None, "level": job.level, "status": "pending"}}})
        for job in settle:
            self._patch(job.agent_id, job.item_id, _settled(job.level, "failed", "skipped"))
        if not admitted:
            return
        if self._synchronous:
            while self._has_work():
                delay = self._round()
                if delay:
                    time.sleep(delay)
        else:
            self._start()
            self._wake.set()

    def _turn(self, agent_id: str, turn: dict[str, Any]) -> None:
        turn_id = str(turn.get("turn_id") or "")
        with self._lock:
            jobs = self._jobs.get(agent_id)
            if turn.get("status") == "running":
                previous = self._turns.get(agent_id)
                self._turns[agent_id] = turn_id
                if jobs and previous and previous != turn_id:
                    # A new turn replaces the items; nothing is left to patch.
                    self._releases.extend(job.demand for job in jobs.values())
                    jobs.clear()
            elif jobs:
                cap = self._clock() + TURN_END_GRACE_SEC
                for job in jobs.values():
                    job.deadline = min(job.deadline, cap)

    def _has_work(self) -> bool:
        with self._lock:
            return any(self._jobs.values()) or bool(self._releases)

    def close(self) -> None:
        self._closed = True
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _start(self) -> None:
        if self._closed:
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="live-explainer")
            self._thread.start()

    def _run(self) -> None:
        while not self._closed:
            if not self._has_work():
                self._wake.wait()
                self._wake.clear()
                continue
            try:
                delay = self._round()
            except Exception as exc:  # noqa: BLE001 - a presentation extra
                from .log import log_exception
                log_exception("liveExplainFail", exc)
                delay = self._poll or 0.1
            if delay:
                self._wake.wait(delay)
                self._wake.clear()

    def _round(self) -> float:
        """Ask once per agent for its newest waiting items; patch what landed.

        Returns how long to wait before the next round.
        """
        now = self._clock()
        expired: list[_Job] = []
        batches: list[list[_Job]] = []
        with self._lock:
            for agent_id, jobs in list(self._jobs.items()):
                for item_id in [i for i, job in jobs.items() if job.deadline <= now]:
                    job = jobs.pop(item_id)
                    self._releases.append(job.demand)
                    expired.append(job)
                if jobs:
                    # An item already asked for keeps its place until it
                    # settles: its model call may be running. Free places go
                    # to the newest items still waiting.
                    batch = [job for job in jobs.values() if job.asked]
                    waiting = [job for job in jobs.values() if not job.asked]
                    free = BATCH - len(batch)
                    batch += waiting[-free:] if free > 0 else []
                    for job in batch:
                        job.asked = True
                    batches.append(batch)
                else:
                    del self._jobs[agent_id]
            releases, self._releases = self._releases[-64:], []
        for job in expired:
            self._patch(job.agent_id, job.item_id, _settled(job.level, "failed", "skipped"))
        setting = tool_explanation_settings.get()
        level = int(setting["detail_level"])
        if not setting["enabled"] or level == 0:
            self._drop(batches, "disabled")
            return 0.0
        service = self._service()
        if service is None:
            self._drop(batches, "unavailable")
            return 0.0
        if not batches and releases:
            batches = [[]]
        for batch in batches:
            try:
                self._ask(service, level, batch, releases)
            except Exception as exc:  # noqa: BLE001 - logged, and the batch settles
                from . import db
                from .log import log, log_exception
                if db.is_locked_error(exc):
                    # Busy SQLite is transient: the jobs stay queued and their
                    # deadlines still bound the wait.
                    delay = LOCK_BACKOFF_SEC[min(self._locked, len(LOCK_BACKOFF_SEC) - 1)]
                    self._locked += 1
                    log("liveExplainLocked", f"attempt={self._locked} items={len(batch)} retry_in={delay}s")
                    with self._lock:
                        self._releases.extend(releases)
                    return delay
                log_exception("liveExplainFail", exc)
                self._drop([batch], "error")
            releases = []
        self._locked = 0
        return self._poll

    def _drop(self, batches: list[list[_Job]], reason: str) -> None:
        dropped = []
        with self._lock:
            for batch in batches:
                for job in batch:
                    jobs = self._jobs.get(job.agent_id)
                    if jobs is not None and jobs.get(job.item_id) is job:
                        del jobs[job.item_id]
                        dropped.append(job)
        for job in dropped:
            self._patch(job.agent_id, job.item_id, _settled(job.level, "failed", reason))

    def _ask(self, service: Any, level: int, batch: list[_Job], releases: list[str]) -> None:
        items = [{"id": job.demand, "demand_id": job.demand, "activity": self._activity(job.tool)}
                 for job in batch]
        agent_id = batch[0].agent_id if batch else None
        cwd = None
        if agent_id:
            from . import agents as agents_db
            cwd = (agents_db.get_by_agent_id(agent_id) or {}).get("cwd")
        result = service.request(level, items, cwd=cwd, release=releases or None,
                                 target_agent_id=agent_id)
        answers = {item.get("id"): item for item in result.get("items") or []}
        settled: list[tuple[_Job, dict[str, Any]]] = []
        with self._lock:
            for job in batch:
                answer = answers.get(job.demand) or {}
                status = answer.get("status")
                if status in {"pending", "busy"}:
                    continue
                if status == "ready" and answer.get("text"):
                    fields = {"tool": {"explain": {"text": answer["text"], "level": level, "status": "ready"}}}
                else:
                    reason = "skipped" if status == "cancelled" else str(answer.get("reason") or status or "invalid_response")
                    fields = _settled(level, "failed", reason)
                jobs = self._jobs.get(job.agent_id)
                if jobs is not None and jobs.get(job.item_id) is job:
                    del jobs[job.item_id]
                    settled.append((job, fields))
        for job, fields in settled:
            self._patch(job.agent_id, job.item_id, fields)

    @staticmethod
    def _activity(tool: dict[str, Any]) -> dict[str, Any]:
        preview = tool.get("input_preview") if isinstance(tool.get("input_preview"), dict) else {}
        activity = {"name": tool.get("name") or "", "command": tool.get("command") or "",
                    "input": preview, "title": tool.get("label") or ""}
        for key in ("file_path", "path", "pattern"):
            if preview.get(key):
                activity[key] = preview[key]
        return activity
