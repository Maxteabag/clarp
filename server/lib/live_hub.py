"""The Host side of live items (docs/live-items.md).

One ``LiveHub`` lives in the process that runs the agents (the runtime, or the
single-process server). Backends report what they see (a message growing, a
tool starting, output lines, an item settling, a turn ending); the hub turns
that into ``live`` events with a per-conversation ``lseq``, keeps the open
turn's state for ``GET /live``, and derives the status line from the items.

Every op the hub emits is also applied to its own ``LiveView`` (the reference
reducer clients run), so the snapshot is by construction what a client that
saw every event holds.

Pacing: text and output growth is coalesced per conversation (at most one
event per ``text_interval``); item starts, settles, status and turn changes
go out at once, after any pending growth so order is kept.
"""
from __future__ import annotations

import copy
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .live_items import LiveView, OUTPUT_TAIL_LINES
from .live_pacing import Schedule, _thread_timer

EXPLORE_CATEGORIES = frozenset({"read", "list", "search"})
_VERBS = {
    "exec": "Running", "read": "Reading", "list": "Listing", "search": "Searching",
    "edit": "Editing", "write": "Writing", "fetch": "Fetching", "mcp": "Calling",
    "todo": "Updating plan", "agent": "Delegating",
}
_TITLE_BOLD = re.compile(r"^\s*\*\*(.+?)\*\*")
_TITLE_HEADING = re.compile(r"^\s*#+\s*(.+)$")


def _now_ms() -> int:
    return int(time.time() * 1000)


def reasoning_title(text: str) -> str | None:
    """A one-line headline: a leading **bold** line or heading, else the
    first sentence, at most 80 characters."""
    text = (text or "").strip()
    if not text:
        return None
    first = text.splitlines()[0]
    match = _TITLE_BOLD.match(first) or _TITLE_HEADING.match(first)
    if match:
        title = match.group(1).strip()
    else:
        sentence = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0]
        title = sentence.strip()
    return title[:80].rstrip() or None


def tool_headline(name: str, category: str, label: str) -> str:
    verb = _VERBS.get(category)
    if verb == "Updating plan":
        return verb
    if verb:
        return f"{verb} {label}".strip()
    return f"{name} {label}".strip()


@dataclass
class _Conv:
    conv: str
    agent_id: str
    session: str
    view: LiveView
    pending_text: dict[str, str] = field(default_factory=dict)       # id -> chunk
    pending_output: dict[str, tuple[list[str], int]] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)                   # pending ids in order
    timer: Any = None
    ordinal: int = 0
    explore_group: str | None = None
    account_wait: str | None = None


class LiveHub:
    def __init__(self, *, sink: Callable[[dict[str, Any]], Any],
                 clock_ms: Callable[[], int] = _now_ms,
                 schedule: Schedule | None = None,
                 text_interval: float = 0.1, output_interval: float = 0.25):
        self.epoch = uuid.uuid4().hex[:12]
        self._sink = sink
        self._clock_ms = clock_ms
        self._schedule = schedule or _thread_timer
        self._text_interval = text_interval
        self._output_interval = output_interval
        self._lock = threading.RLock()
        self._by_agent: dict[str, _Conv] = {}
        self._state_ids: dict[str, int] = {}

    # --- turns -------------------------------------------------------------

    def begin_turn(self, *, agent_id: str, session: str, conv: str, turn_id: str,
                   started_at_ms: int | None = None) -> None:
        with self._lock:
            state = self._by_agent.get(agent_id)
            if state is None or state.conv != conv:
                view = LiveView()
                view.apply_snapshot({"epoch": self.epoch, "lseq": 0,
                                     "activity": {"state": "idle"}, "turn": None, "items": []})
                state = _Conv(conv=conv, agent_id=agent_id, session=session, view=view)
                self._by_agent[agent_id] = state
            state.session = session or state.session
            current = state.view.turn
            if current and current.get("turn_id") == turn_id and current.get("status") == "running":
                return
            if current and current.get("status") == "running":
                # A new provider turn while the last one never settled (it was
                # preempted or its process was killed without a stop): end it
                # first, so clients never keep a turn running forever.
                self.end_turn(agent_id, status="interrupted")
            self._flush(state)
            state.explore_group = None
            state.account_wait = None
            started = int(started_at_ms or self._clock_ms())
            self._emit(state, [{"op": "turn", "conv": conv, "turn": {
                "turn_id": turn_id, "status": "running", "started_at_ms": started,
                "ended_at_ms": None, "worked_ms": None, "tool_count": 0}}],
                status_after=True)

    def end_turn(self, agent_id: str, *, status: str = "completed",
                 error: dict[str, str] | None = None) -> None:
        """Settle the open turn. A failed turn carries ``error`` (reason,
        message, provider detail) so a client can say why instead of folding
        a one-second turn behind "Worked for 1s"."""
        with self._lock:
            state = self._open(agent_id)
            if state is None:
                return
            self._flush(state)
            now = self._clock_ms()
            settle = "completed" if status == "completed" else "interrupted"
            ops: list[dict[str, Any]] = []
            for item in state.view.items():
                if item.get("status") in {"pending", "running"}:
                    ops.append(self._done_op(state, item, settle, now, None, rev_offset=0))
            turn = dict(state.view.turn or {})
            turn.update(status=status, ended_at_ms=now,
                        worked_ms=now - int(turn.get("started_at_ms") or now))
            turn.pop("error", None)
            if status == "failed" and error:
                turn["error"] = error
            ops.append({"op": "turn", "conv": state.conv, "turn": turn})
            self._emit(state, ops, status_after=True, ended=status)

    # --- items -------------------------------------------------------------

    def account_recovery(self, agent_id: str, message: str | None) -> None:
        """Keep a parked turn's position while exposing its real wait state."""
        with self._lock:
            state = self._open(agent_id)
            if state is None or state.account_wait == message:
                return
            self._flush(state)
            state.account_wait = message
            self._emit(state, [], status_after=True)

    def message_text(self, agent_id: str, item_id: str, text: str, *,
                     phase: str | None = None, row_id: str | None = None,
                     turn_id: str | None = None) -> None:
        self._grow_text(agent_id, item_id, "message", text,
                        {"phase": phase or "final", "row_id": row_id}, turn_id=turn_id)

    def reasoning_text(self, agent_id: str, item_id: str, text: str, *,
                       turn_id: str | None = None) -> None:
        with self._lock:
            self._grow_text(agent_id, item_id, "reasoning", text, {"title": None}, turn_id=turn_id)
            state = self._open(agent_id, turn_id)
            item = state.view._items.get(item_id) if state else None
            if state is None or item is None:
                return
            full = (item.get("text") or "") + state.pending_text.get(item_id, "")
            title = reasoning_title(full)
            if title and title != item.get("title"):
                self._flush(state)
                self._emit(state, [self._upsert_op(state, item_id, "reasoning", {"title": title})],
                           status_after=True)

    def tool_start(self, agent_id: str, item_id: str, *, name: str, call_id: str,
                   category: str, label: str, command: str | None = None,
                   input_preview: dict[str, Any] | None = None,
                   status: str = "running", turn_id: str | None = None) -> None:
        with self._lock:
            state = self._open(agent_id, turn_id)
            if state is None:
                return
            existing = state.view._items.get(item_id)
            if category in EXPLORE_CATEGORIES:
                if state.explore_group is None:
                    state.explore_group = f"explore:{item_id}"
                group = state.explore_group
            else:
                state.explore_group = None
                group = None
            tool = {"name": name, "call_id": call_id, "category": category, "group": group,
                    "label": label, "command": command, "input_preview": input_preview or {},
                    "explain": None, "output": None, "diff": None}
            self._flush(state)
            if existing is not None:
                tool["group"] = (existing.get("tool") or {}).get("group")
                tool.pop("explain")
                tool.pop("output")
                tool.pop("diff")
                op = self._upsert_op(state, item_id, "tool", {"status": status, "tool": tool})
            else:
                op = self._new_op(state, item_id, "tool", {"status": status, "tool": tool})
            turn = dict(state.view.turn or {})
            ops = [op]
            if existing is None:
                # Text before a tool call was narration, not the answer.
                for item in state.view.items():
                    if item.get("kind") == "message" and item.get("phase") == "final":
                        ops.insert(0, self._upsert_op(state, item["id"], "message",
                                                      {"phase": "commentary"}))
            if existing is None and turn:
                turn["tool_count"] = int(turn.get("tool_count") or 0) + 1
                ops.append({"op": "turn", "conv": state.conv, "turn": turn})
            self._emit(state, ops, status_after=True)

    def tool_output(self, agent_id: str, item_id: str, lines: list[str], *,
                    total_lines: int, turn_id: str | None = None) -> None:
        with self._lock:
            state = self._open(agent_id, turn_id)
            if state is None or item_id not in state.view._items:
                return
            pending, _total = state.pending_output.get(item_id, ([], 0))
            merged = (pending + [str(line)[:400] for line in lines])[-OUTPUT_TAIL_LINES:]
            state.pending_output[item_id] = (merged, int(total_lines))
            if item_id not in state.order:
                state.order.append(item_id)
            self._arm(state, self._output_interval)

    def patch(self, agent_id: str, item_id: str, fields: dict[str, Any], *,
              turn_id: str | None = None) -> None:
        """Merge fields into an item (explanation, diff stats, plan steps)."""
        with self._lock:
            state = self._open(agent_id, turn_id)
            if state is None or item_id not in state.view._items:
                return
            self._flush(state)
            kind = state.view._items[item_id].get("kind")
            self._emit(state, [self._upsert_op(state, item_id, kind, fields)])

    def item(self, agent_id: str, item_id: str, kind: str, fields: dict[str, Any], *,
             turn_id: str | None = None) -> None:
        """Create or patch an item of any kind (plan, diff, compaction)."""
        with self._lock:
            state = self._open(agent_id, turn_id)
            if state is None:
                return
            self._flush(state)
            if item_id in state.view._items:
                op = self._upsert_op(state, item_id, kind, fields)
            else:
                op = self._new_op(state, item_id, kind, fields)
            self._emit(state, [op], status_after=True)

    def done(self, agent_id: str, item_id: str, *, status: str = "completed",
             patch: dict[str, Any] | None = None, turn_id: str | None = None) -> None:
        with self._lock:
            state = self._open(agent_id, turn_id)
            if state is None:
                return
            item = state.view._items.get(item_id)
            if item is None or item.get("status") not in {"pending", "running"}:
                return
            self._flush(state)
            item = state.view._items[item_id]     # the flush replaced it
            op = self._done_op(state, item, status, self._clock_ms(), patch, rev_offset=0)
            self._emit(state, [op], status_after=True)

    # --- reads -------------------------------------------------------------

    def has_open_turn(self, agent_id: str) -> bool:
        with self._lock:
            state = self._by_agent.get(agent_id)
            turn = state.view.turn if state else None
            return bool(turn and turn.get("status") == "running")

    def has_item(self, agent_id: str, item_id: str) -> bool:
        with self._lock:
            state = self._by_agent.get(agent_id)
            return bool(state and item_id in state.view._items)

    def snapshot(self, *, session: str = "", agent_id: str = "") -> dict[str, Any] | None:
        with self._lock:
            state = self._find(session=session, agent_id=agent_id)
            if state is None:
                return None
            self._flush(state)
            view = state.view
            return {
                "conv": state.conv, "session": state.session, "agent_id": state.agent_id,
                "epoch": self.epoch, "lseq": int(view.lseq or 0),
                "server_now_ms": self._clock_ms(),
                "activity": copy.deepcopy(view.activity),
                "turn": copy.deepcopy(view.turn),
                "items": copy.deepcopy(view.items()),
            }

    def activities(self) -> dict[str, dict[str, Any]]:
        """Current status per agent id (for /agents/snapshot)."""
        with self._lock:
            return {agent_id: copy.deepcopy(state.view.activity)
                    for agent_id, state in self._by_agent.items()}

    # --- internals ---------------------------------------------------------

    def _find(self, *, session: str = "", agent_id: str = "") -> _Conv | None:
        if agent_id and agent_id in self._by_agent:
            return self._by_agent[agent_id]
        for state in self._by_agent.values():
            if session and (state.session == session or state.agent_id == session):
                return state
        return None

    def _open(self, agent_id: str, turn_id: str | None = None) -> _Conv | None:
        """The agent's conversation state, or None. With ``turn_id`` (the turn
        that produced an op) only while that very turn is the running one:
        ops from a turn that already ended are dropped, never attached to the
        newer turn."""
        state = self._by_agent.get(agent_id)
        if state is None or not state.view.turn:
            return None
        if turn_id is not None and (state.view.turn.get("turn_id") != turn_id
                                    or state.view.turn.get("status") != "running"):
            return None
        return state

    def current_turn_id(self, agent_id: str) -> str | None:
        """The running turn's id, or None."""
        with self._lock:
            state = self._by_agent.get(agent_id)
            turn = state.view.turn if state else None
            return turn.get("turn_id") if turn and turn.get("status") == "running" else None

    def _grow_text(self, agent_id: str, item_id: str, kind: str, text: str,
                   create_fields: dict[str, Any], turn_id: str | None = None) -> None:
        with self._lock:
            state = self._open(agent_id, turn_id)
            if state is None:
                return
            item = state.view._items.get(item_id)
            if item is None:
                self._flush(state)
                state.explore_group = None
                self._emit(state, [self._new_op(state, item_id, kind,
                                                {**create_fields, "text": text})],
                           status_after=True)
                return
            if item.get("status") not in {"pending", "running"}:
                return
            known = (item.get("text") or "") + state.pending_text.get(item_id, "")
            if text == known:
                return
            if text.startswith(known):
                state.pending_text[item_id] = state.pending_text.get(item_id, "") + text[len(known):]
                if item_id not in state.order:
                    state.order.append(item_id)
                self._arm(state, self._text_interval)
                return
            # The provider rewrote the text (a corrected snapshot): replace it.
            self._flush(state)
            self._emit(state, [self._upsert_op(state, item_id, kind, {"text": text})])

    def _arm(self, state: _Conv, interval: float) -> None:
        if state.timer is not None:
            return

        def fire() -> None:
            with self._lock:
                state.timer = None
                self._flush(state)
        state.timer = self._schedule(interval, fire)

    def _flush(self, state: _Conv) -> None:
        if state.timer is not None:
            try:
                state.timer.cancel()
            except Exception:  # noqa: BLE001
                pass
            state.timer = None
        if not state.order:
            return
        ops: list[dict[str, Any]] = []
        revs: dict[str, int] = {}
        for item_id in state.order:
            item = state.view._items.get(item_id)
            if item is None:
                continue
            kind = item.get("kind")
            chunk = state.pending_text.pop(item_id, "")
            if chunk:
                revs[item_id] = revs.get(item_id, int(item.get("rev") or 0)) + 1
                ops.append({"op": "append", "conv": state.conv, "id": item_id, "kind": kind,
                            "rev": revs[item_id], "field": "text", "chunk": chunk})
            output = state.pending_output.pop(item_id, None)
            if output:
                lines, total = output
                revs[item_id] = revs.get(item_id, int(item.get("rev") or 0)) + 1
                ops.append({"op": "append", "conv": state.conv, "id": item_id, "kind": kind,
                            "rev": revs[item_id], "field": "tool.output",
                            "lines": lines, "total_lines": total})
        state.order = []
        state.pending_text.clear()
        state.pending_output.clear()
        if ops:
            self._emit(state, ops, status_after=True)

    def _new_op(self, state: _Conv, item_id: str, kind: str, fields: dict[str, Any]) -> dict:
        state.ordinal += 1
        turn = state.view.turn or {}
        item = {"id": item_id, "conv": state.conv, "turn_id": turn.get("turn_id") or "",
                "kind": kind, "status": "running", "ordinal": state.ordinal,
                "started_at_ms": self._clock_ms(), "ended_at_ms": None}
        item.update(fields)
        return {"op": "upsert", "conv": state.conv, "id": item_id, "kind": kind, "rev": 1,
                "item": item}

    def _upsert_op(self, state: _Conv, item_id: str, kind: str, fields: dict[str, Any]) -> dict:
        rev = int(state.view._items[item_id].get("rev") or 0) + 1
        return {"op": "upsert", "conv": state.conv, "id": item_id, "kind": kind, "rev": rev,
                "item": fields}

    def _done_op(self, state: _Conv, item: dict[str, Any], status: str, now: int,
                 patch: dict[str, Any] | None, rev_offset: int) -> dict:
        op = {"op": "done", "conv": state.conv, "id": item["id"], "kind": item.get("kind"),
              "rev": int(item.get("rev") or 0) + 1 + rev_offset, "status": status,
              "started_at_ms": int(item.get("started_at_ms") or now), "ended_at_ms": now}
        if patch:
            op["item"] = patch
        return op

    def _emit(self, state: _Conv, ops: list[dict[str, Any]], *, status_after: bool = False,
              ended: str = "") -> None:
        if status_after or ended:
            probe = LiveView()
            probe.__dict__.update(copy.deepcopy(state.view.__dict__))
            for op in ops:
                probe._apply_op(op)
            activity = self._activity(state, probe, ended)
            if _changed(state.view.activity, activity):
                ops = ops + [{"op": "status", "conv": state.conv, "activity": activity}]
        event = {"type": "live", "agent_id": state.agent_id, "session": state.session,
                 "conv": state.conv, "epoch": self.epoch,
                 "lseq": int(state.view.lseq or 0) + 1,
                 "server_now_ms": self._clock_ms(), "ops": ops}
        assert state.view.apply_event(event) == [], "hub op rejected by its own reducer"
        try:
            self._sink(event)
        except Exception:  # noqa: BLE001 - a dead sink never breaks a turn
            pass

    def _activity(self, state: _Conv, view: LiveView, ended: str) -> dict[str, Any]:
        turn = view.turn or {}
        now = self._clock_ms()
        base = {"tool": None, "running_tools": 0, "headline": None, "item_id": None,
                "since_ms": now, "turn_id": turn.get("turn_id"),
                "turn_started_ms": turn.get("started_at_ms")}
        if ended or turn.get("status") not in (None, "running"):
            final = ended or str(turn.get("status") or "completed")
            state_name = "interrupted" if final in ("interrupted", "failed") else "idle"
            headline = None
            if state_name == "interrupted":
                headline = str((turn.get("error") or {}).get("message") or "Interrupted")
            return {**base, "state": state_name, "turn_started_ms": None,
                    "headline": headline}
        if state.account_wait:
            return {**base, "state": "limited", "headline": state.account_wait,
                    "since_ms": _previous_since(state.view.activity, "limited", now)}
        running = [item for item in view.items() if item.get("status") in {"pending", "running"}]
        tools = [item for item in running if item.get("kind") == "tool"]
        if tools:
            newest = tools[-1]
            tool = newest.get("tool") or {}
            return {**base, "state": "tool", "running_tools": len(tools),
                    "item_id": newest["id"], "since_ms": newest.get("started_at_ms") or now,
                    "headline": tool_headline(tool.get("name") or "", tool.get("category") or "",
                                              tool.get("label") or ""),
                    "tool": {"name": tool.get("name"), "call_id": tool.get("call_id"),
                             "label": tool.get("label"), "category": tool.get("category"),
                             "item_id": newest["id"],
                             "started_at_ms": newest.get("started_at_ms")}}
        for kind, name in (("compaction", "compacting"), ("message", "responding"),
                           ("reasoning", "thinking")):
            match = [item for item in running if item.get("kind") == kind]
            if match:
                item = match[-1]
                headline = {"compacting": "Compacting", "responding": "Responding"}.get(name)
                if name == "thinking":
                    headline = f"Thinking: {item['title']}" if item.get("title") else "Thinking"
                return {**base, "state": name, "item_id": item["id"],
                        "since_ms": item.get("started_at_ms") or now, "headline": headline}
        return {**base, "state": "thinking", "headline": "Thinking",
                "since_ms": _previous_since(state.view.activity, "thinking", now)}


def _previous_since(activity: dict[str, Any] | None, state_name: str, now: int) -> int:
    if activity and activity.get("state") == state_name and activity.get("since_ms"):
        return int(activity["since_ms"])
    return now


def _changed(old: dict[str, Any] | None, new: dict[str, Any]) -> bool:
    if not old:
        return True
    keys = set(old) | set(new)
    return any(old.get(key) != new.get(key) for key in keys if key != "since_ms") or \
        old.get("state") != new.get("state")


# --- process wiring -----------------------------------------------------------

class LiveFanout:
    """The runtime's sink: hands every event to each connected HTTP relay.

    A relay that falls behind (a full queue) is dropped; it reconnects and
    its clients recover through GET /live.
    """

    QUEUE_SIZE = 4096

    def __init__(self) -> None:
        import queue as _queue
        self._queue_type = _queue.Queue
        self._lock = threading.Lock()
        self._subs: list[Any] = []

    def subscribe(self):
        q = self._queue_type(maxsize=self.QUEUE_SIZE)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, event: dict[str, Any]) -> None:
        import queue as _queue
        with self._lock:
            for q in list(self._subs):
                try:
                    q.put_nowait(event)
                except _queue.Full:
                    # The stream handler sees this and hangs up; the relay
                    # reconnects and its clients resync from GET /live.
                    self._subs.remove(q)
                    q.dropped = True


class LiveRelay:
    """In the HTTP process: holds one ``live_stream`` call open on the
    runtime socket and hands each event to the SSE hub. Reconnects with
    backoff; an older runtime that does not know the call is retried slowly."""

    def __init__(self, socket_path, stream, *, on_nudge: Callable[[], Any] | None = None,
                 on_event: Callable[[dict[str, Any]], Any] | None = None):
        import pathlib
        self.socket_path = pathlib.Path(socket_path)
        self.stream = stream
        self.on_nudge = on_nudge
        self.on_event = on_event
        # True only while connected to a runtime that serves live events.
        self.serving = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock = None

    def start(self) -> "LiveRelay":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="live-relay")
        self._thread.start()
        return self

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        delay = 0.25
        while not self._stop.is_set():
            unsupported = False
            try:
                unsupported = self._follow()
                delay = 0.25
            except Exception as exc:  # noqa: BLE001 - relay must self-heal
                if not self._stop.is_set():
                    from .log import log_exception
                    log_exception("liveRelayFail", exc)
            if self._stop.wait(30.0 if unsupported else delay):
                return
            delay = min(delay * 2, 5.0)

    def _follow(self) -> bool:
        import json as _json
        import socket as _socket
        from .runtime_bridge import encode_request
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self._sock = sock
        try:
            sock.connect(str(self.socket_path))
            sock.sendall(encode_request("live_stream", {}))
            reader = sock.makefile("rb")
            for raw in reader:
                if self._stop.is_set():
                    return False
                try:
                    event = _json.loads(raw)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind in ("hello", "live", "ping"):
                    self.serving = True
                if kind == "live":
                    self.stream.broadcast_live(event)
                    if self.on_event is not None:
                        try:
                            self.on_event(event)
                        except Exception:  # noqa: BLE001 - an extra, never the relay
                            pass
                elif kind == "sse" and self.on_nudge is not None:
                    self.on_nudge()
                elif "ok" in event and not event.get("ok"):
                    return True       # an older runtime without live_stream
            return False
        finally:
            self.serving = False
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass


_HUB: LiveHub | None = None
_FANOUT: LiveFanout | None = None


def install(hub: LiveHub | None, fanout: LiveFanout | None = None) -> None:
    global _HUB, _FANOUT
    _HUB = hub
    _FANOUT = fanout


def nudge() -> None:
    """Tell the HTTP relay a durable SSE row was just written (so it relays
    it now instead of on its next poll)."""
    fanout = _FANOUT
    if fanout is not None:
        fanout.publish({"type": "sse"})


def current() -> LiveHub | None:
    return _HUB


def report(method: str, *args: Any, **kwargs: Any) -> Any:
    """Call a hub method if this process runs one; never raises."""
    hub = _HUB
    if hub is None:
        return None
    try:
        return getattr(hub, method)(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - live items never break a turn
        from .log import log_exception
        log_exception("liveHubFail", exc, detail=method)
        return None


PREFIXES = {"claude": "cl", "codex": "cx", "opencode": "oc", "grok": "gk",
            "agy": "ag", "deepseek": "ds"}


def item_prefix(backend: str) -> str:
    return PREFIXES.get(str(backend or "").lower(), str(backend or "x")[:4] or "x")


def observe_transition(agent_id: str, event: str, to_state: str,
                       detail: dict[str, Any] | None) -> None:
    """Feed one state-machine transition to the hub (no-op without one).

    Turn start and end, compaction, and tool start/finish for every backend
    whose tools are observed in this process (Claude's hooks run in their
    own processes; its tools come from the stream instead).
    """
    hub = _HUB
    if not agent_id:
        return
    if hub is None:
        _forward_to_runtime(agent_id, event, to_state, detail or {})
        return
    try:
        _observe(hub, agent_id, event, to_state, detail or {})
    except Exception as exc:  # noqa: BLE001 - live items never break a turn
        from .log import log_exception
        log_exception("liveObserveFail", exc, detail=event)


def _observe(hub: LiveHub, agent_id: str, event: str, to_state: str,
             detail: dict[str, Any]) -> None:
    # HTTP->runtime RPCs run off the caller's thread. A delayed earlier busy
    # transition must not reopen a turn after its newer Stop has settled it.
    # The writer supplies SQLite's durable order, independent of clocks.
    with hub._lock:
        state_id = int(detail.get("_state_id") or 0)
        if state_id:
            if state_id <= hub._state_ids.get(agent_id, 0):
                return
            hub._state_ids[agent_id] = state_id
        _observe_state(hub, agent_id, event, to_state, detail)


def _observe_state(hub: LiveHub, agent_id: str, event: str, to_state: str,
                   detail: dict[str, Any]) -> None:
    from .protocol import AgentState
    from .turn_lifecycle import BUSY, TERMINAL, TurnEvent
    trace = str(detail.get("trace_id") or "")
    current = hub.current_turn_id(agent_id)
    if to_state in TERMINAL:
        interrupted = to_state in (AgentState.INTERRUPTED, AgentState.STOPPED)
        if trace and current and trace != current:
            return              # an older turn's late end: the running one is newer
        if interrupted and current is None:
            # The hub never saw this turn or lost it (a restarted runtime, a
            # stop recorded elsewhere): settle it from the database so clients
            # get an explicit end instead of a turn that runs forever.
            _settle_from_database(hub, agent_id, detail)
            return
        status = "completed"
        if event in _FAILED_EVENTS:
            status = "failed"
        elif interrupted:
            status = "interrupted"
        hub.end_turn(agent_id, status=status, error=_turn_error(detail))
        return
    if to_state not in BUSY:
        return
    agent = ensure_turn(hub, agent_id, trace=trace)
    if agent is None:
        return
    if event == TurnEvent.ACCOUNT_RECOVERY_WAIT:
        hub.account_recovery(agent_id, str(detail.get("message") or
                                          "Waiting for an account with available usage")[:140])
        return
    if event in (TurnEvent.SPAWN_STARTED, TurnEvent.TEXT_STREAMED, TurnEvent.TOOL_STARTED):
        hub.account_recovery(agent_id, None)
    prefix = item_prefix(agent.get("backend") or "")
    call_id = str(detail.get("call_id") or "")
    if event in (TurnEvent.TOOL_STARTED, TurnEvent.TOOL_FINISHED) and call_id:
        from .live_tools import classify_tool
        name = str(detail.get("tool") or "tool")
        category, label, command = classify_tool(name, detail.get("input"))
        item_id = f"{prefix}:{call_id}"
        if event == TurnEvent.TOOL_STARTED or not hub.has_item(agent_id, item_id):
            hub.tool_start(agent_id, item_id, name=name, call_id=call_id, category=category,
                           label=label, command=command,
                           input_preview=_preview(detail.get("input")))
        if event == TurnEvent.TOOL_FINISHED:
            hub.done(agent_id, item_id,
                     status="failed" if detail.get("status") == "error" else "completed")
        return
    turn = (hub.snapshot(agent_id=agent_id) or {}).get("turn") or {}
    compaction_id = f"{prefix}:{turn.get('turn_id') or ''}:compaction"
    if event == TurnEvent.COMPACTION_STARTED:
        hub.item(agent_id, compaction_id, "compaction", {"status": "running"})
    elif event == TurnEvent.COMPACTION_FINISHED:
        hub.done(agent_id, compaction_id)


def _turn_error(detail: dict[str, Any]) -> dict[str, str] | None:
    """What a failed turn's state detail says went wrong, for its turn op."""
    from .error_classify import provider_message
    raw = str(detail.get("provider_message") or detail.get("error") or "")
    message = str(detail.get("message") or "")
    if not (raw or message):
        return None
    return {"reason": str(detail.get("reason") or "failed"),
            "message": message[:300] or "Turn failed",
            "detail": provider_message(raw)[:500]}


# A provider turn that failed (its process died and was not retried): the
# live turn ends `failed`, its running items `interrupted`.
_FAILED_EVENTS = frozenset({"process_exited_failed", "turn_failed_unclassified", "unlaunched"})


def _settle_from_database(hub: LiveHub, agent_id: str, detail: dict[str, Any]) -> None:
    from . import agents as agents_db
    from .db import conn
    agent = agents_db.get_by_agent_id(agent_id)
    if not agent:
        return
    trace = str(detail.get("trace_id") or "")
    row = conn().execute(
        """SELECT trace_id, started_at FROM turns WHERE agent_id = ?
              AND (? = '' OR trace_id = ?)
            ORDER BY started_at DESC LIMIT 1""", (agent_id, trace, trace)).fetchone()
    if row is None:
        return
    snapshot = hub.snapshot(agent_id=agent_id) or {}
    if (snapshot.get("turn") or {}).get("turn_id") == row["trace_id"]:
        return                                    # already settled
    conv = agents_db.live_backend_session(agent_id) or f"pending:{agent_id}"
    hub.begin_turn(agent_id=agent_id, session=str(agent.get("session") or ""), conv=conv,
                   turn_id=str(row["trace_id"] or f"turn-{_now_ms()}"),
                   started_at_ms=int(row["started_at"]) if row["started_at"] else None)
    hub.end_turn(agent_id, status="interrupted")


def _forward_to_runtime(agent_id: str, event: str, to_state: str,
                        detail: dict[str, Any]) -> None:
    """This process has no hub (the HTTP server of a split Host): hand the
    transition to the runtime's hub, which serves the live events. Off the
    caller's thread; best effort."""
    from . import backends
    client = backends._RUNTIME_CLIENT
    forward = getattr(client, "live_observe", None)
    if forward is None:
        return
    keep = {key: detail.get(key) for key in ("trace_id", "call_id", "tool", "input", "status",
                                           "account_recovery", "message", "_state_id")
            if detail.get(key) is not None}

    def send() -> None:
        try:
            forward(agent_id, event, to_state, keep)
        except Exception:  # noqa: BLE001 - an older runtime, or it is restarting
            pass
    threading.Thread(target=send, daemon=True, name="live-forward").start()


def _preview(tool_input: Any) -> dict[str, Any]:
    if not isinstance(tool_input, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in tool_input.items():
        if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 400:
            out[key] = value
        if len(out) >= 8:
            break
    return out


def ensure_turn(hub: LiveHub, agent_id: str, *, trace: str = "") -> dict[str, Any] | None:
    """The agent's live turn is the provider turn that is running now.

    Without a trace: keep the running turn, else open the newest from the
    database. With the trace of the turn that produced an event: that turn,
    opening it (and so settling an unsettled older one) when it is newer than
    the running turn. An event from an older turn changes nothing.
    """
    from . import agents as agents_db
    from .db import conn
    agent = agents_db.get_by_agent_id(agent_id)
    if not agent:
        return None
    current = hub.current_turn_id(agent_id)
    if current is not None and (not trace or trace == current):
        return agent
    session = str(agent.get("session") or "")
    conv = agents_db.live_backend_session(agent_id) or f"pending:{agent_id}"
    if trace:
        bounds = conn().execute(
            """SELECT MIN(started_at) AS first, MAX(started_at) AS last, MAX(turn_id) AS row
                 FROM turns WHERE agent_id = ? AND trace_id = ?""", (agent_id, trace)).fetchone()
        first = int(bounds["first"]) if bounds and bounds["first"] is not None else None
        newest = int(bounds["last"]) if bounds and bounds["last"] is not None else None
        last = (hub.snapshot(agent_id=agent_id) or {}).get("turn") or {}
        if last.get("turn_id") and last["turn_id"] != trace and bounds and bounds["row"] is not None:
            # Turn rows are numbered in the order prompts were admitted, which
            # two prompts in one millisecond cannot break.
            held = conn().execute(
                "SELECT MAX(turn_id) AS row FROM turns WHERE agent_id = ? AND trace_id = ?",
                (agent_id, last["turn_id"])).fetchone()
            if held and held["row"] is not None and int(bounds["row"]) < int(held["row"]):
                return None     # a late event from an older turn
        if last.get("turn_id") == trace and last.get("status") != "running" and not (
                newest is not None and last.get("ended_at_ms") and newest > int(last["ended_at_ms"])):
            return None         # that turn ended; only a new prompt reopens its trace
        hub.begin_turn(agent_id=agent_id, session=session, conv=conv, turn_id=trace,
                       started_at_ms=first)
        return agent
    row = conn().execute(
        """SELECT trace_id, started_at FROM turns
            WHERE agent_id = ? AND ended_at IS NULL
            ORDER BY started_at DESC LIMIT 1""", (agent_id,)).fetchone()
    hub.begin_turn(agent_id=agent_id, session=session, conv=conv,
                   turn_id=str((row["trace_id"] if row else "") or f"turn-{_now_ms()}"),
                   started_at_ms=int(row["started_at"]) if row and row["started_at"] else None)
    return agent


def empty_snapshot(*, conv: str, session: str, agent_id: str, epoch: str) -> dict[str, Any]:
    return {"conv": conv, "session": session, "agent_id": agent_id, "epoch": epoch,
            "lseq": 0, "server_now_ms": _now_ms(),
            "activity": {"state": "idle", "tool": None, "running_tools": 0, "headline": None,
                         "item_id": None, "since_ms": None, "turn_id": None,
                         "turn_started_ms": None},
            "turn": None, "items": []}
