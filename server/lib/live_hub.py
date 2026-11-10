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

from .live_core import (
    LiveView, OUTPUT_TAIL_LINES, Schedule, _thread_timer,
    EXPLORE_CATEGORIES,
    LiveFanout,
    LiveHub,
    PREFIXES,
    _Conv,
    _TITLE_BOLD,
    _TITLE_HEADING,
    _VERBS,
    _changed,
    _now_ms,
    _previous_since,
    current,
    empty_snapshot,
    ensure_turn,
    install,
    item_prefix,
    nudge,
    reasoning_title,
    report,
    tool_headline,
)


# --- process wiring -----------------------------------------------------------


class LiveRelay:
    """In the HTTP process: holds one ``live_stream`` call open on the
    runtime socket and hands each event to the SSE hub. Reconnects with
    backoff; an older runtime that does not know the call is retried slowly.
    Events are never replayed, so ``on_connect`` runs (on its own thread)
    each time the stream opens, to catch up from snapshots."""

    def __init__(self, socket_path, stream, *, on_nudge: Callable[[], Any] | None = None,
                 on_event: Callable[[dict[str, Any]], Any] | None = None,
                 on_connect: Callable[[], Any] | None = None):
        import pathlib
        self.socket_path = pathlib.Path(socket_path)
        self.stream = stream
        self.on_nudge = on_nudge
        self.on_event = on_event
        self.on_connect = on_connect
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

    def _connected(self) -> None:
        try:
            self.on_connect()
        except Exception as exc:  # noqa: BLE001 - an extra, never the relay
            from .log import log_exception
            log_exception("liveRelayConnectFail", exc)

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
                if kind == "hello" and self.on_connect is not None:
                    threading.Thread(target=self._connected, daemon=True,
                                     name="live-relay-connect").start()
                if kind == "live":
                    self.stream.broadcast_live(event)
                    if self.on_event is not None:
                        try:
                            self.on_event(event)
                        except Exception as exc:  # noqa: BLE001 - an extra, never the relay
                            from .log import log_exception
                            log_exception("liveRelayObserverFail", exc)
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


def observe_transition(agent_id: str, event: str, to_state: str,
                       detail: dict[str, Any] | None) -> None:
    """Feed one state-machine transition to the hub (no-op without one).

    Turn start and end, compaction, and tool start/finish for every backend
    whose tools are observed in this process (Claude's hooks run in their
    own processes; its tools come from the stream instead).
    """
    hub = current()
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
    if event == TurnEvent.UPDATE_HANDOFF:
        # The turn never ran and waits for the update as held work: keep its
        # place (a revert relaunches the same trace into it) and say why.
        if current is not None and (not trace or trace == current):
            hub.account_recovery(agent_id, str(detail.get("message") or
                                               "Waiting for the Clarp update")[:140])
        return
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
_FAILED_EVENTS = frozenset({"process_exited_failed", "turn_failed_unclassified", "unlaunched",
                            "call_opening_unconfirmed"})


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
