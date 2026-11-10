"""Persistent Codex app-server transport with genuine mid-turn steering.

Unlike ``codex exec`` (one process per turn), app-server keeps a thread alive
and exposes ``turn/steer``.  Clarp keeps **one** stdio app-server for the Host
and multiplexes agents by ``threadId``. One process per agent left leftover
writers that blocked ``thread/resume`` with JSON-RPC ``-32600``.
"""
from __future__ import annotations

import json
import hashlib
import itertools
import os
import pathlib
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import agent_goals, agents as agents_db, backend_usage, eventlog, settings_store, tts_queue
from .backend.codex import CodexBackend, TurnState
from .backend.registry import by_id
from .log import log, log_exception
from .protocol import TurnSource
from . import turn_lifecycle
from . import events
from .turn_lifecycle import TurnEvent
from .voice_preamble import app_turn_instructions, persona_identity_instruction, voice_instructions


def _backend() -> CodexBackend:
    """The Codex strategy whose event mapping and ``exec`` path this pool uses."""
    return by_id("codex")


def _codex_argv() -> list[str]:
    """Resolve ``codex app-server --stdio`` from the live runner binary.

    Read the backend's ``required_binary`` at spawn time so the QA host (and
    tests) can point at ``fake_codex.py`` without also patching this module.
    A ``.py`` path is launched with the current interpreter.
    """
    binary = str(_backend().required_binary)
    # A thread Clarp releases (a move off a draining generation, BACKENDS-06b)
    # closes 2 s after its last subscriber left instead of codex's 60 s; Clarp
    # unsubscribes nothing else, so normal operation is unchanged.
    unload = ["-c", "thread_unload_delay_secs=2"]
    if os.path.isfile(binary) and binary.endswith(".py"):
        return [sys.executable, binary, "app-server", "--stdio", *unload]
    found = shutil.which(binary)
    if found is None and os.path.isfile(binary) and os.access(binary, os.X_OK):
        found = binary
    if found is None:
        raise FileNotFoundError(f"`{binary}` not on PATH")
    return [found, "app-server", "--stdio", *unload]


@dataclass
class _ActiveTurn:
    turn_id: str
    thread_id: str
    agent_id: str
    session: str
    trace_id: str
    state: TurnState
    handle: "AppTurnHandle"
    on_result: Callable[[dict], None] | None
    on_error: Callable[[str], None] | None
    stream: Any
    enqueue: Callable[..., int]
    voice: bool = False
    produced_output: bool = False
    steer_ready: threading.Event = field(default_factory=threading.Event)


# A stopped Codex turn the app-server has not ended this long after its
# turn/interrupt is detached (BACKENDS-07b); the same 10 s as a CLI turn's
# stop deadline (process_registry.STOP_KILL_DEADLINE_SEC, BACKENDS-07).
STOP_DETACH_SEC = 10.0
# The bound of each RPC of a turn's start (thread/resume or thread/start,
# then turn/start). The start runs outside the pool lock (TP-N3), so a hung
# start holds up only its own agent, and only this long.
START_RPC_TIMEOUT_SEC = 30.0


class _StoppedBeforeLaunch(Exception):
    """A Stop arrived before turn/start was sent: nothing is launched."""


class CodexSessionStuck(RuntimeError):
    """The agent's thread is held by a stuck app-server process that is
    recycled once no other live turn runs on it."""


class AppTurnHandle:
    """ProcessRegistry-compatible logical handle for one app-server turn."""
    def __init__(self, client: "_Client", agent_id: str = ""):
        self.client = client
        self.agent_id = agent_id
        self.proc = self
        self._done = threading.Event()

    @property
    def pid(self) -> int:
        return self.client.proc.pid

    def poll(self):
        return 0 if self._done.is_set() else None

    def wait(self, timeout=None) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("codex app-server turn", timeout)
        return 0

    def is_alive(self) -> bool:
        return not self._done.is_set()

    def terminate(self) -> None:
        # Only this handle's own turn: a late terminate (a retry that lost its
        # slot during the handoff) must not reach the agent's next turn (TP-N1).
        if self._done.is_set():
            return
        self.stop_requested = True
        timer = threading.Timer(STOP_DETACH_SEC, self._detach_if_running)
        timer.daemon = True
        timer.start()
        self.client.interrupt_active(self.agent_id, handle=self, by_user=True)

    def _detach_if_running(self) -> None:
        """The server never ended this stopped turn: detach it (BACKENDS-07b).
        It no longer counts as busy; the shared process is recycled once no
        live turn runs on it, and until then this agent gets no new turn."""
        client = self.client
        with client._lock:
            if self._done.is_set():
                return
            actives = getattr(client, "_actives", {})
            mine = [turn for turn in (actives.get(self.agent_id), client.active)
                    if turn is not None and turn.handle is self]
            detached = getattr(client, "detached_agents", None)
            if detached is None:
                detached = client.detached_agents = set()
            detached.add(self.agent_id)
            # Its execution may still announce itself: never adopt it again.
            client.detached_executions = getattr(client, "detached_executions", set()) | {
                value for turn in mine for value in (turn.thread_id, turn.turn_id) if value}
            if actives.get(self.agent_id) is not None and actives[self.agent_id].handle is self:
                actives.pop(self.agent_id, None)
            if client.active is not None and client.active.handle is self:
                client.active = None
            self._done.set()
        log("codexTurnDetached", f"agent={self.agent_id} after={STOP_DETACH_SEC:g}s")
        client.recycle_if_quiet()


class _Client:
    # Guards each client's goal-state marks. Taken after self._lock,
    # never before it; the goal mirror takes only this one.
    _goal_state_lock = threading.Lock()
    # Makes each claim handover one step: registering a turn, its terminal
    # removal and a failed launch's cleanup (TP-N3). Innermost; held briefly.
    _claims_lock = threading.Lock()

    def __init__(self, agent_id: str, session: str, stream=None):
        argv = _codex_argv()
        self.agent_id = agent_id
        self.auth_fingerprint = _auth_fingerprint()
        self.refresh_requested = False
        # Agents whose stopped turn was detached here, and that turn's thread
        # and turn ids (BACKENDS-07b); set once this process is being killed.
        self.detached_agents: set[str] = set()
        self.detached_executions: set[str] = set()
        self.recycled = False
        # BACKENDS-06b: a generation is one app-server process. A draining
        # one takes no new turns and releases its threads at turn boundaries.
        self.generation = next(_GENERATIONS)
        self.rotation_incarnation = secrets.token_hex(16)
        self.draining = False
        self.unsubscribe_unsupported = False
        # Turns running on threads no Clarp agent owns (codex subagents):
        # until they end no thread moves off this process.
        self.foreign_turns: set[str] = set()
        self.releasing: set[str] = set()
        self.rate_limits = {}
        # One shared server serves every Codex agent: it has no turn identity
        # and no agent identity. Naming the agent that started it would let a
        # tool in any agent's shell act as that one; without it they refuse
        # (BACKENDS-N1).
        env = dict(os.environ)
        env.pop("CLARP_PROVIDER_TURN", None)
        env.pop("CLAUDE_PWA_SESSION", None)
        # Its own process group, so a recycle (BACKENDS-07b) kills the server
        # and the helpers in its group. Codex starts each shell tool command
        # in its own session with SIGTERM as its parent-death signal, so those
        # get SIGTERM when the server dies and what they started can survive.
        self.process_group = os.name == "posix"
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace", bufsize=1,
            env=env, start_new_session=self.process_group,
        )
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, tuple[threading.Event, dict, Callable[[dict], None] | None]] = {}
        self.active: _ActiveTurn | None = None
        self._actives: dict[str, _ActiveTurn] = {}
        self.thread_id = ""
        # Threads this process has resumed or started, so goal calls on an
        # idle agent do not resume the same thread again.
        self._loaded_threads: set[str] = set()
        self.stream = stream
        self.reader_dead = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._drain_stderr,
                                              daemon=True)
        self.stderr_reader.start()
        self.request("initialize", {
            "clientInfo": {"name": "clarp", "title": "Clarp", "version": "1"},
            # Required by app-server for application-scoped per-turn context.
            "capabilities": {"experimentalApi": True},
        })
        self.notify("initialized", {})

    def _send(self, message: dict) -> None:
        line = json.dumps(message, separators=(",", ":")) + "\n"
        with self._write_lock:
            if self.proc.stdin is None:
                raise RuntimeError("codex app-server stdin closed")
            self.proc.stdin.write(line)
            self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 30, *,
                on_result: Callable[[dict], None] | None = None) -> dict:
        with self._lock:
            if getattr(self, "reader_dead", False):
                raise RuntimeError("codex app-server reader ended")
            rid = self._next_id
            self._next_id += 1
            done = threading.Event()
            box: dict = {}
            self._pending[rid] = (done, box, on_result)
        self._send({"method": method, "id": rid, "params": params})
        if not done.wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"Codex app-server timed out: {method}")
        if "error" in box:
            raise RuntimeError(f"{method}: {box['error']}")
        return box.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        self._send({"method": method, "params": params})

    def _read(self) -> None:
        failure = "codex app-server exited"
        try:
            if self.proc.stdout is None:
                return
            for line in self.proc.stdout:
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                # One bad message must not end the reader every agent shares.
                try:
                    self._handle_message(message)
                except Exception as exc:  # noqa: BLE001
                    log_exception("codexAppServerMessageFail", exc,
                                  detail=self.agent_id)
        except Exception as exc:  # noqa: BLE001
            failure = str(exc) or failure
            log_exception("codexAppServerReadFail", exc, detail=self.agent_id)
        finally:
            # Nothing reads this process any more: before any cleanup, stop
            # _client() reusing it and request() waiting on it.
            with self._lock:
                self.reader_dead = True
            self._fail_all(failure)

    def _handle_message(self, message) -> None:
        if "id" in message and ("result" in message or "error" in message):
            with self._lock:
                pending = self._pending.pop(message["id"], None)
            if pending:
                done, box, on_result = pending
                box.update(message)
                try:
                    if on_result is not None and "error" not in message:
                        on_result(message.get("result") or {})
                except Exception as exc:  # noqa: BLE001 - keep the shared reader alive
                    log_exception("codexGoalMirrorFail", exc, detail=self.agent_id)
                finally:
                    done.set()
            return
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        if "id" in message:  # unexpected server request: reject safely
            self._send({"id": message["id"], "error": {
                "code": -32601, "message": "unsupported by headless Clarp"
            }})
            return
        self._notification(method, params)

    def _drain_stderr(self) -> None:
        if self.proc.stderr is None:
            return
        try:
            for line in self.proc.stderr:
                if line.strip():
                    log("codexAppServerStderr", line.strip()[:1000])
        except Exception as exc:  # noqa: BLE001
            log_exception("codexAppServerStderrFail", exc, detail=self.agent_id)

    def _fail_all(self, message: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for done, box, _on_result in pending:
            box["error"] = {"message": message}
            done.set()
        actives = list(getattr(self, "_actives", {}).values())
        if self.active is not None and self.active not in actives:
            actives.append(self.active)
        self.active = None
        if hasattr(self, "_actives"):
            self._actives.clear()
        for active in actives:
            if not active:
                continue
            with self._lock:  # the reader and retire() may both fail it
                if active.handle._done.is_set():
                    continue
                active.handle._done.set()
            if active.on_error:
                self._callback(active.on_error, message)

    def _callback(self, callback: Callable, value: Any) -> None:
        """Never block the protocol reader in dispatch/queue lifecycle code."""
        def run() -> None:
            try:
                callback(value)
            except Exception as exc:  # noqa: BLE001
                log_exception("codexAppServerCallbackFail", exc,
                              detail=self.agent_id)
        threading.Thread(target=run, daemon=True).start()

    def _pick_active(self, params: dict) -> _ActiveTurn | None:
        thread_id = str(params.get("threadId") or "")
        turn = params.get("turn")
        if not thread_id and isinstance(turn, dict):
            thread_id = str(turn.get("threadId") or "")
        if thread_id:
            for active in list(getattr(self, "_actives", {}).values()):
                if active.thread_id == thread_id:
                    return active
            # Single-client compatibility still requires the explicitly named thread.
            if self.active is not None and self.active.thread_id == thread_id:
                return self.active
            # Subagents also emit notifications on the shared app-server stream.
            # An explicit foreign thread must never inherit the last active writer.
            return None
        turn_id = str(params.get("turnId") or (turn.get("id") if isinstance(turn, dict) else "") or "")
        if turn_id:
            for active in list(getattr(self, "_actives", {}).values()):
                if active.turn_id == turn_id:
                    return active
            return self.active if self.active is not None and self.active.turn_id == turn_id else None
        return self.active

    def _notification(self, method: str, params: dict) -> None:
        if self._suppressed(method, params):
            return
        active = self._pick_active(params)
        if method == "account/rateLimits/updated":
            self.rate_limits = params
            try:
                snapshot = backend_usage.capture_codex_rate_limits(params)
                event_stream = (active.stream if active is not None
                                else getattr(self, "stream", None))
                if event_stream is not None:
                    for event in snapshot.get("limit_events") or []:
                        events.broadcast(event_stream, events.as_event(event))
            except Exception as exc:  # noqa: BLE001
                log_exception("codexRateLimitsUpdateFail", exc,
                              detail=self.agent_id)
            return
        if method in ("thread/goal/updated", "thread/goal/cleared"):
            self._mirror_goal(str(params.get("threadId") or ""),
                              params.get("goal") if method.endswith("updated") else None)
            return
        if method == "thread/closed":
            thread_id = str(params.get("threadId") or "")
            self._loaded_threads.discard(thread_id)
            getattr(self, "releasing", set()).discard(thread_id)
            if getattr(self, "draining", False):
                log("codexThreadMoveDone", f"thread={thread_id[:8]} from={self.generation}")
                _drain_later(self)
                if (_move_get(thread_id) or {}).get("phase") == "moving":
                    threading.Thread(target=_continue_move, args=(thread_id,), daemon=True,
                                     name="codex-goal-move").start()
            return
        if active is None and method == "turn/started" and self._detached_execution(params):
            # The detached turn (or a continuation on its stuck thread) must
            # not count as busy again: that would keep the stuck process alive.
            log("codexDetachedTurnIgnored", "a turn on a detached thread started late")
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            late = str(turn.get("id") or params.get("turnId") or "")
            thread = str(params.get("threadId") or turn.get("threadId") or "")
            if late and thread and late not in getattr(self, "detached_executions", set()):
                # A stopped or uncertain launch that started after all: it is
                # not adopted, and it is interrupted by its exact ids (TP-N3).
                with self._lock:
                    self.detached_executions = getattr(self, "detached_executions", set()) | {late}
                threading.Thread(target=self._interrupt_quietly, args=(thread, late),
                                 daemon=True, name="codex-late-start").start()
            return
        if active is None and method == "turn/started":
            # Codex starts continuation turns for an active goal on its own.
            # Nobody in Clarp asked for this turn, so nothing is listening yet;
            # adopt it so it streams, records state and marks the agent busy
            # like any other turn instead of being dropped on the floor.
            active = self._adopt_server_turn(params)
            if active is None:
                self._note_foreign_turn(params, started=True)
        if active is None:
            if method == "turn/completed":
                self._note_foreign_turn(params, started=False)
                if getattr(self, "detached_agents", None) and self._detached_execution(params):
                    # A detached or uncertain execution whose claim is already
                    # gone ended: the process may be quiet now (TP-N3).
                    threading.Thread(target=self.recycle_if_quiet, daemon=True,
                                     name="codex-recycle").start()
            return
        notified_turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        event_turn_id = str(params.get("turnId") or notified_turn.get("id") or "")
        if (method != "turn/started" and event_turn_id and active.turn_id
                and event_turn_id != active.turn_id):
            return
        if method == "turn/started":
            notified_turn = params.get("turn") or {}
            active.turn_id = str(notified_turn.get("id") or active.turn_id)
            active.steer_ready.set()
            self._stop_learned(active)          # a Stop during the start (TP-N3)
            _backend().transition(active.agent_id, TurnEvent.SPAWN_STARTED,
                                  {"dispatch": "codex", "trace_id": active.trace_id})
            return
        if method == "thread/tokenUsage/updated":
            usage = (params.get("tokenUsage") or {}).get("last") or {}
            active.state.tokens_in = int(usage.get("inputTokens") or 0)
            active.state.tokens_out = int(usage.get("outputTokens") or 0)
            return
        live_items = getattr(active.state, "live_items", None)
        if live_items is None:
            from .live_codex import CodexLiveItems
            live_items = active.state.live_items = CodexLiveItems(active.agent_id, active.trace_id)
        if method in ("item/started", "item/completed", "item/reasoning/summaryTextDelta",
                      "item/reasoning/summaryPartAdded", "item/commandExecution/outputDelta"):
            live_items.on_notification(method, params)
        if method in ("item/started", "item/updated", "item/completed"):
            item = params.get("item")
            if isinstance(item, dict):
                active.produced_output = True
                normalized = _normalize_item(item)
                phase = method.replace("/", ".")
                _backend().handle_item(phase, normalized, active.state,
                                       agent_id=active.agent_id, session=active.session,
                                       trace_id=active.trace_id, stream=active.stream,
                                       enqueue=active.enqueue)
            return
        if method == "item/agentMessage/delta":
            delta = params.get("delta") or params.get("text") or ""
            if isinstance(delta, dict):
                delta = delta.get("text") or ""
            if isinstance(delta, str) and delta:
                active.produced_output = True
                item_key = str(params.get("itemId") or "")
                current = active.state.pending_live_text
                if item_key and item_key != active.state.live_item_key:
                    # A new agentMessage item: its text starts its own row.
                    current = ""
                # Protocol deltas are normally incremental chunks, but tolerate
                # servers that send a progressively complete snapshot.
                merged = delta if delta.startswith(current) else current + delta
                active.state.last_agent_message = merged
                _backend().update_live_text(
                    active.state, text=merged, agent_id=active.agent_id,
                    session=active.session, trace_id=active.trace_id,
                    stream=active.stream, item_key=item_key or None)
            return
        if method == "turn/completed":
            turn = params.get("turn") or {}
            status = str(turn.get("status") or "completed")
            error = turn.get("error") or {}
            try:
                _backend().update_live_text(
                    active.state, agent_id=active.agent_id,
                    session=active.session, trace_id=active.trace_id,
                    stream=active.stream, force=True)
            except Exception as exc:  # noqa: BLE001 - the turn must still end
                log_exception("codexAppServerFlushFail", exc,
                              detail=active.agent_id)
            with self._claims_lock:   # only this claim: a successor may own the slot
                if getattr(self, "_actives", {}).get(active.agent_id) is active:
                    self._actives.pop(active.agent_id, None)
                if self.active is active:
                    self.active = None
            active.handle._done.set()
            if getattr(self, "detached_agents", None):
                # Not on the reader: a send may hold the pool lock while it
                # waits for this reader to deliver its turn/start reply.
                threading.Thread(target=self.recycle_if_quiet, daemon=True,
                                 name="codex-recycle").start()
            if getattr(self, "draining", False):
                _drain_later(self)
            if status == "failed":
                message = str(error.get("message") or "codex turn failed")
                if active.on_error:
                    self._callback(active.on_error, CodexTurnFailure(
                        message, self, active.produced_output))
            elif status == "interrupted":
                if not getattr(active.handle, "stop_requested", False):
                    # No Stop asked for it. A move must never cause this
                    # (pausing an active goal leaves its turn running); the
                    # post-release check counts these lines (BACKENDS-06b).
                    log("codexTurnInterruptedUnasked",
                        f"agent={active.agent_id} thread={str(active.thread_id)[:8]} "
                        f"moving={bool(getattr(self, 'draining', False))} "
                        f"goal_paused_for_move={_move_get(active.thread_id) is not None}")
                if active.on_error:
                    self._callback(active.on_error, "codex turn interrupted")
            elif active.on_result:
                self._callback(active.on_result, {
                    "usage": {"input_tokens": active.state.tokens_in,
                              "output_tokens": active.state.tokens_out},
                    "last_agent_message": active.state.last_agent_message,
                })
            _backend().broadcast_transcript(active.stream, active.agent_id, active.session)

    def _mirror_goal(self, thread_id: str, goal: dict | None) -> None:
        """Keep Clarp's goal table and clients in step with the app-server."""
        if not thread_id:
            return
        recorded = False
        with self._goal_state_lock:
            # Marks set after this point are newer than what this write saw.
            started_at = getattr(self, "goal_mark_seq", 0)
        try:
            mirrored = agent_goals.mirror_codex(
                thread_id, goal, incarnation=getattr(self, "rotation_incarnation", None))
            if mirrored is None:
                return
            agent, row = mirrored
            recorded = True
            with self._goal_state_lock:
                mark = self._goal_marks().get(thread_id)
                # Only a failed write it began after can this write resolve.
                if mark and mark[0] == "write" and mark[1] <= started_at:
                    del self._goal_marks()[thread_id]
            stream = getattr(self, "stream", None)
            if stream is not None:
                events.broadcast(stream, events.as_event(agent_goals.event(agent, row)))
        except Exception as exc:  # noqa: BLE001 - a mirror failure must not stop the reader
            if not recorded:
                # The mirror may now be stale: the recycle treats this
                # goal as possibly active until a later write records it.
                self._mark_goal_unknown(thread_id, "write")
            log_exception("codexGoalMirrorFail", exc, detail=thread_id)

    def _goal_answer_begin(self, thread_id: str) -> None:
        with self._goal_state_lock:
            inflight = self._goal_activations()
            inflight[thread_id] = inflight.get(thread_id, 0) + 1

    def _goal_answer_end(self, thread_id: str) -> None:
        with self._goal_state_lock:
            inflight = self._goal_activations()
            inflight[thread_id] -= 1
            if not inflight[thread_id]:
                del inflight[thread_id]
        if getattr(self, "detached_agents", None):
            # An outstanding answer may be all that held a stuck process.
            threading.Thread(target=self.recycle_if_quiet, daemon=True,
                             name="codex-recycle").start()

    def _goal_activations(self) -> dict[str, int]:
        """Threads with a goal request whose answer is outstanding, and how
        many (call under _goal_state_lock)."""
        inflight = getattr(self, "goal_activations_inflight", None)
        if inflight is None:
            inflight = self.goal_activations_inflight = {}
        return inflight

    def _goal_marks(self) -> dict[str, tuple[str, int]]:
        """Threads whose goal state is unknown: (kind, sequence). "write": a
        failed mirror write, resolved by a successful write that began after
        it. "unanswered": a start/resume whose answer was lost; nothing proves
        which state codex settled on, so it lasts for this process (call
        under _goal_state_lock)."""
        marks = getattr(self, "goal_state_marks", None)
        if marks is None:
            marks = self.goal_state_marks = {}
        return marks

    def _mark_goal_unknown(self, thread_id: str, kind: str) -> None:
        with self._goal_state_lock:
            self.goal_mark_seq = getattr(self, "goal_mark_seq", 0) + 1
            marks = self._goal_marks()
            if (marks.get(thread_id) or ("",))[0] != "unanswered":
                marks[thread_id] = (kind, self.goal_mark_seq)

    def _goal_unknown(self) -> set[str]:
        """Threads whose goal state is unknown (call under _goal_state_lock)."""
        return set(self._goal_marks())

    def _note_foreign_turn(self, params: dict, *, started: bool) -> None:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        thread_id = str(params.get("threadId") or turn.get("threadId") or "")
        if not thread_id or agents_db.get_by_backend_session(thread_id):
            return
        key = str(turn.get("id") or params.get("turnId") or thread_id)
        foreign = getattr(self, "foreign_turns", None)
        if foreign is None:
            foreign = self.foreign_turns = set()
        if started:
            foreign.add(key)
        elif key in foreign:
            foreign.discard(key)
            if getattr(self, "draining", False):
                _drain_later(self)

    def _thread_running(self, thread_id: str) -> bool:
        turns = list(getattr(self, "_actives", {}).values())
        if self.active is not None:
            turns.append(self.active)
        return any(turn.thread_id == thread_id and not turn.handle._done.is_set()
                   for turn in turns)

    def drain_step(self) -> None:
        """Release what a draining generation can release now (BACKENDS-06b):
        each idle thread is unsubscribed, a running turn is never interrupted
        and its thread follows at turn/completed. While a codex subagent turn
        runs here, nothing moves. Exits once nothing is left."""
        if getattr(self, "exiting", False) or _DRAINING.get(id(self)) is not self:
            return
        if self.foreign_turns or self.unsubscribe_unsupported:
            self._exit_if_drained()
            return
        for thread_id in sorted(self._loaded_threads - self.releasing):
            if self.proc.poll() is not None:
                continue
            self._pause_for_move(thread_id)
            if self._thread_running(thread_id):
                continue
            if not self._capture_move(thread_id):
                continue
            self.releasing.add(thread_id)
            try:
                result = self.request("thread/unsubscribe", {"threadId": thread_id}, timeout=10)
            except RuntimeError as exc:
                self.releasing.discard(thread_id)
                if "-32601" in str(exc) or "not found" in str(exc).lower():
                    # An app-server without the method: drain like stage 1,
                    # until quiet, then shut down.
                    self.unsubscribe_unsupported = True
                    log("codexMoveFallback", f"gen={self.generation}")
                    break
                log_exception("codexThreadReleaseFail", exc, detail=thread_id[:8])
                continue
            except Exception as exc:  # noqa: BLE001 - try again at the next boundary
                self.releasing.discard(thread_id)
                log_exception("codexThreadReleaseFail", exc, detail=thread_id[:8])
                continue
            status = str((result or {}).get("status") or "")
            log("codexThreadMoveStart", f"thread={thread_id[:8]} from={self.generation} status={status}")
            # Only notLoaded means released. notSubscribed means this
            # connection was already unsubscribed (an earlier reply lost) and
            # the thread is still loaded with its writer lock (pinned
            # thread_processor.rs:1028-1045): keep holding it until
            # thread/closed or the process exits.
            if status == "notLoaded":
                self._loaded_threads.discard(thread_id)
                self.releasing.discard(thread_id)
        self._exit_if_drained()

    def _pause_for_move(self, thread_id: str) -> None:
        """An active goal is paused before its thread moves, so neither this
        process nor the new one starts a continuation during the move. The
        intent is recorded first (decide, then act); pausing never interrupts
        the running turn (ext/goal runtime.rs:248-253)."""
        agent = agents_db.get_by_backend_session(thread_id)
        if not agent:
            return
        record = {"agent_id": agent["agent_id"], "thread": thread_id, "phase": "pausing",
                  "generation": f"{os.getpid()}:{self.generation}"}
        # The whole decision, the pause's effect at codex and the record are
        # under the lock users' goal actions take: a user's pause or resume is
        # ordered entirely before or after the move's pause, so the user's
        # word is the last effect codex sees (a record-only fence does not
        # order the requests themselves). Bounded by the request's timeout.
        with _GOAL_MOVE_LOCK:
            row = agent_goals.get(agent["agent_id"])
            if not row or row.get("status") != agent_goals.ACTIVE or _move_get(thread_id):
                return
            _move_put(thread_id, record)
            self._goal_answer_begin(thread_id)
            try:
                def mirror(result):
                    goal = result.get("goal") if isinstance(result, dict) else None
                    self._mirror_goal(thread_id, goal if isinstance(goal, dict) else None)

                self.request("thread/goal/set", {"threadId": thread_id, "status": "paused"},
                             timeout=10, on_result=mirror)
            except Exception as exc:  # noqa: BLE001 - left 'pausing': recovery decides
                log_exception("codexGoalMovePauseFail", exc, detail=thread_id[:8])
                return
            finally:
                self._goal_answer_end(thread_id)
            if not _move_advance(thread_id, "pausing", {**record, "phase": "paused"}):
                log("codexGoalMoveYielded", f"thread={thread_id[:8]} at=paused")
                return
        log("codexThreadMoveStart", f"thread={thread_id[:8]} from={self.generation} goal_paused=True")

    def _capture_move(self, thread_id: str) -> bool:
        """At the boundary (no turn runs on the thread), record the paused
        goal's updatedAt for the compare-and-set: codex accounts a turn's usage
        on a paused goal too and bumps it (state/runtime/goals.rs), so the
        pause answer's value is stale once the turn has ended. False: try
        again at the next boundary."""
        record = _move_get(thread_id)
        if not record or record.get("phase") not in ("paused",):
            return record is None or record.get("phase") == "moving"
        try:
            result = self.request("thread/goal/get", {"threadId": thread_id}, timeout=10)
        except Exception as exc:  # noqa: BLE001
            log_exception("codexGoalMoveCaptureFail", exc, detail=thread_id[:8])
            return False
        goal = result.get("goal") if isinstance(result, dict) else None
        if not isinstance(goal, dict) or goal.get("status") != "paused":
            with _GOAL_MOVE_LOCK:            # completed, cleared or changed: nothing to restore
                if (_move_get(thread_id) or {}).get("phase") == "paused":
                    _move_forget(thread_id)
            return True
        if not _move_advance(thread_id, "paused",
                             {**record, "phase": "moving", "updated_at": goal.get("updatedAt")}):
            log("codexGoalMoveYielded", f"thread={thread_id[:8]} at=moving")
        return True

    def _mark_retiring_if_quiet(self) -> bool:
        """Before any direct shutdown of this process (credential refresh,
        recycle_clients, usage recovery): the exit mark goal() refuses on is
        set under self._lock, only while no goal answer is outstanding, and
        kept only if nothing may still run here (busy, a subagent turn, or a
        goal that may continue: the coarse rule). So a goal request is
        admitted before the mark (and blocks the shutdown) or refused after
        it. True: the caller may shut it down."""
        with self._lock:
            with self._goal_state_lock:
                if self._goal_activations():
                    return False
            self.exiting = True
        if _busy(self) or getattr(self, "foreign_turns", None) or self._goal_may_continue():
            with self._lock:
                self.exiting = False
            return False
        return True

    def _exit_if_drained(self, *, backstop: bool = False) -> None:
        with _LOCK:
            if (_DRAINING.get(id(self)) is not self or getattr(self, "exiting", False)
                    or _busy(self) or self.foreign_turns):
                return
            if self._loaded_threads and not (backstop or self.unsubscribe_unsupported):
                return
            # The exit mark shares self._lock with goal admission (goal()
            # refuses on it), as stage 1's recycle mark does: a goal request
            # is admitted before the mark (its answer is outstanding: no
            # exit) or refused after it.
            with self._lock:
                with self._goal_state_lock:
                    answering = bool(self._goal_activations())
                if answering:
                    return
                self.exiting = True
            if self._loaded_threads and self._goal_may_continue():
                # A thread still loaded here may have a native turn running
                # whose turn/started is not delivered yet: the backstop and
                # the fallback never end a process while any loaded thread's
                # goal could continue or any goal answer is outstanding
                # (stage 1's coarse rule). They try again later.
                with self._lock:
                    self.exiting = False
                return
            # It stays in _DRAINING until the process has exited: its threads'
            # holds (and their writer locks) end only then.
        log("codexGenerationExited", f"gen={self.generation} backstop={backstop} "
                                     f"loaded={len(self._loaded_threads)}")
        try:
            self.shutdown()
        finally:
            with _LOCK:
                if _DRAINING.get(id(self)) is self and self.proc.poll() is not None:
                    del _DRAINING[id(self)]

    def _detached_execution(self, params: dict) -> bool:
        detached = getattr(self, "detached_executions", None)
        if not detached:
            return False
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        thread_id = str(params.get("threadId") or turn.get("threadId") or "")
        turn_id = str(turn.get("id") or params.get("turnId") or "")
        return bool({thread_id, turn_id} & detached)

    def _adopt_server_turn(self, params: dict) -> _ActiveTurn | None:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        thread_id = str(params.get("threadId") or turn.get("threadId") or "")
        turn_id = str(turn.get("id") or "")
        if not thread_id:
            return None
        agent = agents_db.get_by_backend_session(thread_id)
        if not agent:
            return None
        agent_id, session = agent["agent_id"], agent["session"]
        trace_id = f"goal-{turn_id[:8] or 'turn'}"
        try:
            db_turn = agents_db.open_turn(agent_id=agent_id, source=TurnSource.LOCAL,
                                          trace_id=trace_id, synthesize_audio=False)
        except Exception as exc:  # noqa: BLE001
            log_exception("codexGoalTurnOpenFail", exc, detail=agent_id)
            db_turn = None

        def finish(turn_event: str, detail: dict) -> None:
            if db_turn is not None:
                try:
                    agents_db.close_turn(db_turn)
                except Exception as exc:  # noqa: BLE001
                    log_exception("codexGoalTurnCloseFail", exc, detail=agent_id)
            _backend().transition(agent_id, turn_event, {**detail, "dispatch": "codex-goal",
                                                         "trace_id": trace_id})
            eventlog.emit("server", "codexGoalTurnDone", session=session,
                          agent_id=agent_id, backend_session_id=thread_id,
                          detail={"trace_id": trace_id, "state": turn_lifecycle.target(turn_event), **detail})

        def on_result(event: dict) -> None:
            usage = event.get("usage") or {}
            finish(TurnEvent.PROCESS_EXITED_OK, {"tokens_in": usage.get("input_tokens"),
                                     "tokens_out": usage.get("output_tokens")})

        def on_error(message: str) -> None:
            finish(TurnEvent.PROCESS_EXITED_FAILED, {"error": str(message)[:300]})

        handle = AppTurnHandle(self, agent_id=agent_id)
        active = _ActiveTurn(
            turn_id, thread_id, agent_id, session, trace_id,
            TurnState(live_backend_session_id=thread_id),
            handle, on_result, on_error, getattr(self, "stream", None),
            tts_queue.enqueue, False,
        )
        if not hasattr(self, "_actives"):
            self._actives = {}
        self._actives[agent_id] = active
        log("codexGoalTurnAdopted", f"agent={agent_id} thread={thread_id} turn={turn_id}")
        return active

    def goal(self, agent: dict, action: str, objective: str = "") -> dict | None:
        """Drive ``thread/goal/*`` for one agent and mirror the answer.

        ``action`` is start, pause, resume, clear or get. The thread must exist:
        a goal runs inside a conversation, and Codex rejects ephemeral threads.
        """
        agent_id = agent["agent_id"]
        thread_id = agents_db.live_backend_session(agent_id)
        if not thread_id:
            raise ValueError("Send the agent a message first; a goal needs a conversation to run in.")
        if action == "start" and not objective.strip():
            raise ValueError("objective required")
        if action not in ("start", "pause", "resume", "clear", "get"):
            raise ValueError(f"unknown goal action: {action}")
        activating = action in ("start", "resume")
        # Every goal request is an outstanding answer until its own answer is
        # mirrored; no recycle while one is (BACKENDS-07b, the lead's coarse
        # rule). No goal request of any kind starts on a process the recycle
        # has marked: this check and the mark share self._lock.
        with self._lock:
            if getattr(self, "recycled", False):
                raise RuntimeError("Codex is restarting a stuck session; try again in a moment")
            if getattr(self, "exiting", False):
                # A drained generation being shut down (BACKENDS-06b).
                raise RuntimeError("Codex is moving this conversation to a new session; "
                                   "try again in a moment")
            self._goal_answer_begin(thread_id)
        try:
            try:
                self._goal_request(agent, action, thread_id, objective)
            except Exception:
                if activating:
                    # It may have reached codex: possibly active from now on.
                    self._mark_goal_unknown(thread_id, "unanswered")
                raise
        finally:
            self._goal_answer_end(thread_id)
        return agent_goals.public(agent_goals.get(agent_id))

    def _goal_request(self, agent: dict, action: str, thread_id: str, objective: str) -> dict:
        def mirror(result):
            goal = result.get("goal") if isinstance(result, dict) else None
            self._mirror_goal(thread_id, goal if isinstance(goal, dict) else None)

        if action == "get":
            result = self.request("thread/goal/get", {"threadId": thread_id}, on_result=mirror)
        else:
            self._ensure_loaded(agent, thread_id)
            if action == "clear":
                self.request("thread/goal/clear", {"threadId": thread_id},
                             on_result=lambda _result: self._mirror_goal(thread_id, None))
                result = {"goal": None}
            else:
                params: dict = {"threadId": thread_id, "status": {
                    "start": "active", "resume": "active", "pause": "paused"}[action]}
                if action == "start":
                    params["objective"] = objective.strip()
                result = self.request("thread/goal/set", params, on_result=mirror)
        return result

    def _suppressed(self, method: str, params: dict) -> bool:
        """A continuation codex starts while a stopped goal is being paused
        after a resume is the user's Stop being undone: it is interrupted at
        turn/started and nothing of it is taken for a Clarp turn."""
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn_id = str(turn.get("id") or params.get("turnId") or "")
        dropped = getattr(self, "suppressed_turns", set())
        if turn_id and turn_id in dropped:
            return True
        thread_id = str(params.get("threadId") or turn.get("threadId") or "")
        if method != "turn/started" or not thread_id:
            return False
        own = self._pick_active(params)
        if own is not None and getattr(own.handle, "stop_requested", False):
            return False                    # our own stopped claim: _stop_learned handles it
        # The fence lasts as long as the durable marker: until the goal's
        # pause is established, even after a refused launch (round 11).
        if (thread_id not in getattr(self, "pausing_stopped", set())
                and _stop_pause_get(thread_id) is None):
            return False
        self.suppressed_turns = dropped | {turn_id}
        log("codexStoppedGoalContinuationInterrupted", f"thread={thread_id[:8]} turn={turn_id[:8]}")
        threading.Thread(target=self._interrupt_and_repause, args=(thread_id, turn_id),
                         daemon=True, name="codex-stop-continuation").start()
        return True

    def _interrupt_and_repause(self, thread_id: str, turn_id: str) -> None:
        """Interrupt the continuation, then try the pause again (one try at a
        time per thread): codex restarts an active goal after an interrupt, so
        only the pause ends the fence. Success clears the marker."""
        self._interrupt_quietly(thread_id, turn_id)
        with self._goal_state_lock:
            trying = getattr(self, "repausing", set())
            if thread_id in trying or thread_id in getattr(self, "pausing_stopped", set()):
                return
            self.repausing = trying | {thread_id}
        try:
            self._goal_answer_begin(thread_id)
            try:
                def mirror(result):
                    goal = result.get("goal") if isinstance(result, dict) else None
                    self._mirror_goal(thread_id, goal if isinstance(goal, dict) else None)

                self.request("thread/goal/set", {"threadId": thread_id, "status": "paused"},
                             timeout=10, on_result=mirror)
                _stop_pause_forget(thread_id)
                log("codexStoppedGoalPausedOnResume", f"thread={thread_id[:8]} retry=True")
            except Exception as exc:  # noqa: BLE001 - the fence stays; the next start tries again
                log_exception("codexStoppedGoalPauseFail", exc, detail=thread_id[:8])
            finally:
                self._goal_answer_end(thread_id)
        finally:
            with self._goal_state_lock:
                self.repausing = getattr(self, "repausing", set()) - {thread_id}

    def _interrupt_quietly(self, thread_id: str, turn_id: str) -> None:
        try:
            self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=10)
        except Exception as exc:  # noqa: BLE001
            log_exception("codexStoppedGoalInterruptFail", exc, detail=thread_id[:8])

    def _resume_stopped_safely(self, thread_id: str, resume) -> dict:
        """Resume ``thread_id``; if its Stop-pause never applied (the stuck
        process did not answer it), pause the goal right after the resume,
        before anything else runs, and interrupt any continuation codex
        starts first (BACKENDS-07b, lead round 8)."""
        if not _stop_pause_get(thread_id):
            return resume()
        self.pausing_stopped = getattr(self, "pausing_stopped", set()) | {thread_id}
        try:
            result = resume()
            self._goal_answer_begin(thread_id)
            try:
                def mirror(result):
                    goal = result.get("goal") if isinstance(result, dict) else None
                    self._mirror_goal(thread_id, goal if isinstance(goal, dict) else None)

                self.request("thread/goal/set", {"threadId": thread_id, "status": "paused"},
                             timeout=10, on_result=mirror)
                _stop_pause_forget(thread_id)
                log("codexStoppedGoalPausedOnResume", f"thread={thread_id[:8]}")
            except Exception as exc:
                # The goal the user stopped may still run: admit nothing on
                # this thread. The marker stays for the next resume.
                log_exception("codexStoppedGoalPauseFail", exc, detail=thread_id[:8])
                raise RuntimeError("Could not pause the stopped Codex goal; "
                                   "the message waits for the next try") from exc
            finally:
                self._goal_answer_end(thread_id)
            return result
        finally:
            self.pausing_stopped = getattr(self, "pausing_stopped", set()) - {thread_id}

    def _ensure_loaded(self, agent: dict, thread_id: str) -> None:
        """A goal call needs the thread open in this process; a live turn or an
        earlier resume already did that."""
        if thread_id in self._loaded_threads:
            return
        for active in list(getattr(self, "_actives", {}).values()):
            if active.thread_id == thread_id:
                self._loaded_threads.add(thread_id)
                return
        cwd = pathlib.Path(os.path.expanduser(str(agent.get("cwd") or "~"))).resolve()
        self._resume_stopped_safely(thread_id, lambda: self.request("thread/resume", {
            "threadId": thread_id, "cwd": str(cwd),
            "approvalPolicy": "never", "sandbox": "danger-full-access",
            "developerInstructions": persona_identity_instruction(
                str(agent.get("persona") or ""), agent["session"]),
            **({"model": agent["model"]} if agent.get("model") else {}),
            **_thread_identity(agent["session"]),
        }))
        self._loaded_threads.add(thread_id)

    def start(self, *, text: str, cwd: pathlib.Path, backend_session_id: str,
              is_new_session: bool, session: str, trace_id: str, model: str,
              effort: str, voice: bool, persona: str,
              handle: AppTurnHandle, on_session_init, on_result,
              on_error, stream, enqueue, agent_id: str = "",
              on_claimed: Callable[[], None] | None = None) -> None:
        cwd = pathlib.Path(os.path.expanduser(str(cwd))).resolve()
        owner = agent_id or self.agent_id
        developer_instructions = persona_identity_instruction(persona, session)
        # Claim the logical slot before any blocking RPC. A simultaneous send
        # then sees a live starting handle and waits to steer instead of
        # declaring the dispatch stale and starting an overlapping turn.
        active = _ActiveTurn(
            "", backend_session_id, owner, session, trace_id,
            TurnState(live_backend_session_id=backend_session_id),
            handle, on_result, on_error, stream, enqueue, voice,
        )
        self.stream = stream
        with self._claims_lock:   # one claim handover at a time (_forget_claim)
            self.active = active
            if not hasattr(self, "_actives"):
                self._actives = {}
            self._actives[owner] = active
        if on_claimed is not None:
            # The turn is registered (busy to every fence): the caller lets go
            # of the pool lock before the RPCs below (TP-N3, BACKENDS-01).
            on_claimed()
        launched = False
        try:
            if getattr(handle, "stop_requested", False):
                raise _StoppedBeforeLaunch()
            if backend_session_id and not is_new_session:
                result = self._resume_stopped_safely(backend_session_id, lambda: self.request(
                    "thread/resume", {
                        "threadId": backend_session_id, "cwd": str(cwd),
                        "excludeTurns": True,
                        "approvalPolicy": "never", "sandbox": "danger-full-access",
                        "developerInstructions": developer_instructions,
                        **({"model": model} if model else {}),
                        **_thread_identity(session),
                    }, timeout=START_RPC_TIMEOUT_SEC))
            else:
                result = self.request("thread/start", {
                    "cwd": str(cwd), "approvalPolicy": "never",
                    "sandbox": "danger-full-access",
                    "developerInstructions": developer_instructions,
                    **({"model": model} if model else {}),
                    **_thread_identity(session),
                }, timeout=START_RPC_TIMEOUT_SEC)
            thread = result.get("thread") or {}
            thread_id = str(thread.get("id") or backend_session_id)
            if not thread_id:
                raise RuntimeError("Codex app-server returned no thread id")
            self.thread_id = thread_id
            self._loaded_threads.add(thread_id)
            active.thread_id = thread_id
            active.state.live_backend_session_id = thread_id
            if on_session_init:
                if on_session_init(thread_id) is False:
                    raise RuntimeError("backend session binding rejected")
            if getattr(handle, "stop_requested", False):
                # Stopped between the resume and the launch: nothing is sent.
                raise _StoppedBeforeLaunch()
            launched = True
            turn_result = self.request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                # Stable in app-server 0.145's generated protocol when the
                # initialize handshake opts into experimentalApi. A live
                # contract probe is part of this transport's integration test.
                "additionalContext": {"clarp-app": {
                    "kind": "application",
                    "value": app_turn_instructions(voice=voice, session=session),
                }},
                "cwd": str(cwd), "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
                **({"model": model} if model else {}),
                **({"effort": effort} if effort else {}),
            }, timeout=START_RPC_TIMEOUT_SEC)
            turn = turn_result.get("turn") or {}
            turn_id = str(turn.get("id") or "")
            if not turn_id:
                raise RuntimeError("Codex app-server returned no turn id")
            active.turn_id = turn_id
            if handle._done.is_set():
                # Detached meanwhile (a Stop's deadline passed while the start
                # was in flight): fence this exact execution too.
                with self._lock:
                    self.detached_executions = getattr(self, "detached_executions", set()) | {turn_id}
            self._stop_learned(active)
        except _StoppedBeforeLaunch:
            self._forget_claim(owner, active)
            log("codexStartStoppedBeforeLaunch", f"agent={owner} trace={trace_id}")
        except TimeoutError:
            if launched:
                # turn/start may have reached codex: its outcome is unknown,
                # not 'nothing ran'. Keep a conservative hold: the thread is
                # fenced like a detached execution (late notifications are not
                # adopted, sends wait, the process is recycled once quiet), and
                # a turn id learned meanwhile is interrupted (TP-N3).
                # Marked first: if the reader learns the id at any point from
                # here on, it interrupts it (_stop_learned, exactly once). Not
                # stop_requested: this is Clarp's interrupt, not a user Stop,
                # and stays visible to codexTurnInterruptedUnasked.
                active.interrupt_on_learn = True
                with self._lock:
                    self.detached_agents = getattr(self, "detached_agents", set()) | {owner}
                    self.detached_executions = getattr(self, "detached_executions", set()) | {
                        value for value in (active.thread_id, active.turn_id) if value}
                self._stop_learned(active)
                log("codexStartUncertain", f"agent={owner} thread={str(active.thread_id)[:8]}")
            self._forget_claim(owner, active)
            raise
        except Exception:
            self._forget_claim(owner, active)
            raise

    def _forget_claim(self, owner: str, active: "_ActiveTurn") -> None:
        """Only this exact claim: a later turn of the same agent may already
        own the slot once the start runs outside the pool lock (TP-N3). The
        check and the removal are one step under _claims_lock, like the claim
        registration and the terminal removal, so a successor registered
        meanwhile is never removed."""
        with self._claims_lock:
            if getattr(self, "_actives", {}).get(owner) is active:
                self._actives.pop(owner, None)
            if self.active is active:
                self.active = None
            active.handle._done.set()

    def _stop_learned(self, active: "_ActiveTurn") -> None:
        """A turn the user stopped while its start was in flight, once its id
        is known (from the reply or the reader's turn/started): pause its goal
        and interrupt that exact execution, off the caller, once (TP-N3)."""
        wanted = (getattr(active.handle, "stop_requested", False)
                  or getattr(active, "interrupt_on_learn", False))
        if not wanted or not active.turn_id:
            return
        with self._lock:
            if getattr(active, "interrupt_sent", False):
                return
            active.interrupt_sent = True

        def run() -> None:
            self._pause_goal_for_stop(active)
            self._interrupt_quietly(active.thread_id, active.turn_id)
        threading.Thread(target=run, daemon=True, name="codex-stop-learned").start()

    def steer(self, text: str, client_msg_id: str = "",
              synthesize_audio: bool = False, agent_id: str = "") -> bool:
        active = self._actives.get(agent_id) if agent_id else self.active
        if active is None or not active.handle.is_alive():
            return False
        # turn/start's response can precede turn/started. The protocol only
        # accepts steering after the active lifecycle notification arrives.
        if not active.steer_ready.wait(30.0) or not active.turn_id:
            return False
        params = {
            "threadId": active.thread_id, "expectedTurnId": active.turn_id,
            "input": [{"type": "text", "text": text}],
            **({"clientUserMessageId": client_msg_id} if client_msg_id else {}),
        }
        if synthesize_audio and not active.voice:
            params["additionalContext"] = {"clarp-voice-followup": {
                "kind": "application",
                # The turn already carries the app guidance from turn/start.
                "value": voice_instructions(active.session),
            }}
            active.voice = True
            agents_db.enable_latest_turn_audio(active.agent_id)
        self.request("turn/steer", params)
        return True

    def interrupt_active(self, agent_id: str = "", *, handle: "AppTurnHandle | None" = None,
                         by_user: bool = False) -> None:
        """Interrupt the agent's active turn; with ``handle``, only if that turn
        is the one the handle was returned for."""
        active = self._actives.get(agent_id) if agent_id else self.active
        if active and (handle is None or active.handle is handle):
            if by_user:
                # Only a user's Stop: an internal interrupt (a shutdown, a
                # move) must stay visible to codexTurnInterruptedUnasked.
                active.handle.stop_requested = True
            if not active.turn_id:
                # The start is in flight: return at once. The durable
                # stopped-goal fence is written now; the goal pause and the
                # exact interrupt follow once the id is known (_stop_learned).
                if by_user and active.thread_id:
                    row = agent_goals.get(active.agent_id)
                    if row and row.get("status") == agent_goals.ACTIVE:
                        _stop_pause_put(active.thread_id)
                return
            self._pause_goal_for_stop(active)
            if not active.turn_id:  # pragma: no cover - handled above
                return
            self.request("turn/interrupt", {
                "threadId": active.thread_id, "turnId": active.turn_id,
            })

    def _pause_goal_for_stop(self, active: _ActiveTurn) -> None:
        """Stop means stop. Codex keeps an active goal going after
        ``turn/interrupt`` (only progress is accounted), so a user's Stop would
        otherwise be undone by the next continuation turn. Pause it first."""
        try:
            row = agent_goals.get(active.agent_id)
            if not row or row.get("status") != agent_goals.ACTIVE or not active.thread_id:
                return
            def mirror(result):
                goal = result.get("goal") if isinstance(result, dict) else None
                self._mirror_goal(active.thread_id, goal if isinstance(goal, dict) else None)

            self._goal_answer_begin(active.thread_id)
            try:
                try:
                    self.request("thread/goal/set", {
                        "threadId": active.thread_id, "status": "paused"}, timeout=10,
                        on_result=mirror)
                except Exception:
                    # A stuck process may never apply it: the next resume of
                    # this thread pauses the goal first (Stop means stop).
                    _stop_pause_put(active.thread_id)
                    raise
                _stop_pause_forget(active.thread_id)   # the pause landed
            finally:
                self._goal_answer_end(active.thread_id)
        except Exception as exc:  # noqa: BLE001 - the interrupt itself must still go out
            log_exception("codexGoalPauseFail", exc, detail=active.agent_id)

    def recycle_if_quiet(self) -> None:
        """A process holding a detached (stuck) turn is killed once no live turn
        runs on it: the kill releases the stuck thread's writer lock, and a new
        process serves the next turns (BACKENDS-07b). Never while another
        agent's turn still runs here. The quiet check and the retirement share
        the pool lock with admission (spawn_turn), so no turn is admitted
        between them."""
        with _LOCK:
            with self._lock:
                with self._goal_state_lock:
                    detached = set(getattr(self, "detached_executions", ()))
                    # Other agents' threads only: the stopped agent's own
                    # Stop-pause may never be answered by a stuck process.
                    unknown = bool((set(self._goal_activations()) | self._goal_unknown()) - detached)
                if (not getattr(self, "detached_agents", None)
                        or getattr(self, "recycled", False) or _busy(self) or unknown):
                    return
                self.recycled = True
            # After the mark no goal activation starts here (goal() refuses).
            # Recheck what codex may have started on its own: a turn adopted
            # before the mark is busy now, and an active goal on a thread
            # loaded here can start a continuation at any moment.
            if _busy(self) or self._goal_may_continue():
                with self._lock:
                    self.recycled = False
                return
            # Its thread stays held (turn_holders) until the process is gone.
            # Published as retiring BEFORE it leaves the pool, so a lock-free
            # reader never sees it in neither place (TP-N3).
            _RETIRING[id(self)] = self
            if _CLIENTS.get(_SHARED_KEY) is self:
                _CLIENTS.clear()
            log("codexStuckProcessRecycled",
                f"detached={len(self.detached_agents)}")
            try:
                if getattr(self, "process_group", False):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                else:
                    self.proc.kill()
            except (OSError, ValueError):
                try:
                    self.proc.kill()
                except (OSError, ValueError):
                    pass
            try:
                # Dead before the next process resumes the stuck thread.
                self.proc.wait(timeout=5)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                # No exit evidence: the stopped agent's thread stays held and
                # its send keeps waiting until proc.poll() reports the exit.
                log("codexRecycleUnconfirmed", f"type={type(exc).__name__}")

    def _goal_may_continue(self) -> bool:
        """Could codex start a turn here by itself? The lead's coarse rule: yes
        while any goal answer is outstanding, while a goal's state is unknown
        (a failed mirror write or an unanswered activation), and for any goal
        on a thread loaded here (not a detached one, which dies with it) that
        is not known to be complete or paused. Unknown means yes."""
        detached = getattr(self, "detached_executions", set())
        with self._goal_state_lock:
            # Any outstanding goal answer on another agent's thread holds the
            # process (each request is bounded by its timeout).
            if (set(self._goal_activations()) | self._goal_unknown()) - set(detached):
                return True
        try:
            for thread_id in set(getattr(self, "_loaded_threads", ())) - set(detached):
                agent = agents_db.get_by_backend_session(thread_id)
                row = agent_goals.get(agent["agent_id"]) if agent else None
                # Coarse rule (lead): only a goal known to be complete or
                # paused lets the process go; blocked and limited ones may
                # still continue.
                if row and row.get("status") not in (agent_goals.COMPLETE, agent_goals.PAUSED):
                    return True
        except Exception as exc:  # noqa: BLE001 - fail closed: keep the process
            log_exception("codexRecycleGoalCheckFail", exc, detail=self.agent_id)
            return True
        return False

    def shutdown(self) -> None:
        """EOF stdin so the stdio app-server exits and drops its writer lock."""
        try:
            owners = list(getattr(self, "_actives", {}))
            if self.active is not None and self.active.agent_id not in owners:
                owners.append(self.active.agent_id)
            if owners:
                for owner in owners:
                    self.interrupt_active(owner)
            elif self.active:
                self.interrupt_active()
        except Exception:
            pass
        self._stop_process()

    def retire(self) -> None:
        """Stop a client whose reader ended, without any protocol request:
        nothing would read the reply, so a request would wait out its timeout."""
        self._fail_all("codex app-server reader ended")
        self._stop_process()

    def _stop_process(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def _normalize_item(item: dict) -> dict:
    out = dict(item)
    kinds = {
        "agentMessage": "agent_message", "commandExecution": "command_execution",
        "fileChange": "file_change", "mcpToolCall": "mcp_tool_call",
        "dynamicToolCall": "dynamic_tool_call",
        "collabToolCall": "collab_tool_call",
        "collabAgentToolCall": "collab_agent_tool_call",
        "webSearch": "web_search_call", "imageView": "image_view",
        "imageGeneration": "image_generation", "plan": "plan",
        "reasoning": "reasoning", "contextCompaction": "context_compaction",
    }
    out["type"] = kinds.get(str(out.get("type")), out.get("type"))
    if out.get("type") == "agent_message" and not out.get("text"):
        out["text"] = out.get("message") or ""
    return out


_CLIENTS: dict[str, _Client] = {}
# Generations still releasing their threads after a credential change
# (BACKENDS-06b); each exits once nothing runs or is loaded on it.
_DRAINING: dict[int, _Client] = {}
_GENERATIONS = itertools.count(1)
# A draining generation that still has an idle thread loaded this long after
# its last turn ended is shut down: the thread has no writer left to protect.
DRAIN_BACKSTOP_SEC = 60.0
# Recycled clients whose process has not been seen to exit (BACKENDS-07b).
_RETIRING: dict[int, _Client] = {}
_LOCK = threading.RLock()
_SHARED_KEY = "__shared__"


def _auth_fingerprint() -> bytes:
    # Private generation marker only; never log credential contents or hashes.
    try:
        return hashlib.sha256((backend_usage._codex_home() / "auth.json").read_bytes()).digest()
    except OSError:
        return b""


_STOP_PAUSE_PREFIX = "codex.stop_pause."


def _stop_pause_get(thread_id: str) -> int | None:
    """A Stop-pause the app-server never answered for ``thread_id`` (when it
    was recorded), a small marker kept through settings_store."""
    if not thread_id:
        return None
    value = settings_store.get(_STOP_PAUSE_PREFIX + thread_id)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return 0


def _stop_pause_put(thread_id: str) -> None:
    if thread_id:
        settings_store.set_text(_STOP_PAUSE_PREFIX + thread_id, str(int(time.time() * 1000)))


def _stop_pause_forget(thread_id: str) -> None:
    if thread_id:
        settings_store.delete(_STOP_PAUSE_PREFIX + thread_id)


def _busy(client) -> bool:
    turns = list(getattr(client, "_actives", {}).values())
    active = getattr(client, "active", None)
    if active is not None:
        turns.append(active)
    return any(not turn.handle._done.is_set() for turn in turns)


def credential_switch_pending(message) -> bool:
    """A Codex failure from a client still running credentials older than
    auth.json: the account was switched, but the shared process is busy and
    keeps the old ones until it is idle (BACKENDS-06 (c))."""
    if not isinstance(message, CodexTurnFailure):
        return False
    held = getattr(message.client, "auth_fingerprint", None)
    return held is not None and held != _auth_fingerprint()


def admit_turn(client, agent_id: str) -> None:
    """Refuse a turn for an agent whose stopped turn is detached on this
    (stuck) process: its thread is still held there until it is recycled."""
    if agent_id and agent_id in (getattr(client, "detached_agents", None) or ()):
        raise CodexSessionStuck(
            "Waiting for a stuck Codex session to end; it restarts as soon as "
            "the other Codex agents finish their turns")


class _StuckSessionHold:
    """The agent's stopped turn is detached on a process that still runs and
    holds its thread; its next send waits until that process is gone."""

    def __init__(self, client: "_Client", agent_id: str):
        self.client, self.agent_id = client, agent_id

    def is_alive(self) -> bool:
        # Only process-exit evidence releases the thread, not the kill.
        client = self.client
        return (self.agent_id in (getattr(client, "detached_agents", None) or ())
                and client.proc.poll() is None)


class _MovingThreadHold:
    """The agent's thread is still open in a draining generation; its next
    turn waits until that process released it (BACKENDS-06b)."""

    def __init__(self, client: "_Client", thread_id: str):
        self.client, self.thread_id = client, thread_id

    def is_alive(self) -> bool:
        return (self.thread_id in self.client._loaded_threads
                and self.client.proc.poll() is None)


def turn_holders(agent_id: str) -> list:
    """Processes that still hold ``agent_id``'s thread outside any turn: a
    stuck one (BACKENDS-07b) or a draining generation (BACKENDS-06b). A send
    waits behind them instead of failing."""
    # Read-only: called under the dispatcher's _TURN_LOCK, so never waits on
    # _LOCK and mutates nothing (TP-N3); _client() prunes exited retirees.
    clients = list({id(client): client for client in
                    [*list(_CLIENTS.values()), *list(_RETIRING.values())]}.values())
    draining = list(_DRAINING.values())
    holds: list = [_StuckSessionHold(client, agent_id) for client in clients]
    thread_id = agents_db.live_backend_session(agent_id) if draining else ""
    holds += [_MovingThreadHold(client, thread_id) for client in draining if thread_id]
    return [hold for hold in holds if hold.is_alive()]


class CodexTurnFailure(str):
    def __new__(cls, message, client, produced_output):
        value = super().__new__(cls, message)
        value.client = client
        value.produced_output = produced_output
        value.quota_confirmed = False
        return value


def _matching_bucket_blocked(previous: dict, fresh: dict) -> bool:
    """Never substitute regular Codex quota for a premium/model bucket."""
    bucket = previous.get("rateLimits") or {}
    limit_id = bucket.get("limitId")
    if not limit_id:
        return False
    candidate = (fresh.get("rateLimitsByLimitId") or {}).get(limit_id)
    default = fresh.get("rateLimits") or {}
    if candidate is None and default.get("limitId") == limit_id:
        candidate = default
    if not isinstance(candidate, dict):
        return False  # unknown: one fresh-connection attempt, not quota proof
    if candidate.get("spendControlReached") or candidate.get("rateLimitReachedType"):
        return True
    for name in ("primary", "secondary", "individualLimit"):
        window = candidate.get(name) or {}
        used = window.get("usedPercent")
        if isinstance(used, (int, float)) and used >= 100:
            return True
    return False


def recover_usage_failure(message: str) -> bool:
    """Refresh only an idle failed connection, before any model output/tools.

    The dispatcher fences this to one retry of the same admitted turn. A fresh
    matching exhausted bucket suppresses that retry. Sparse/missing quota is
    unknown, so we permit one connection repair without claiming usable quota.
    """
    if not isinstance(message, CodexTurnFailure):
        return False
    with _LOCK:
        client = message.client
        if _CLIENTS.get(_SHARED_KEY) is not client:
            return False
        client.refresh_requested = True
        if message.produced_output or not client._mark_retiring_if_quiet():
            return False
        # Close the old writer before allowing a new process to resume its
        # threads. Admission and retirement share this lock.
        previous = getattr(client, "rate_limits", {})
        client.shutdown()
        _CLIENTS.clear()
        fresh = _Client(client.agent_id, "", stream=getattr(client, "stream", None))
        _CLIENTS[_SHARED_KEY] = fresh
        try:
            limits = fresh.request("account/rateLimits/read", {}, timeout=8)
        except Exception:
            # An unavailable quota service is not evidence of available quota.
            # The new connection is retained; let the original failure surface.
            return False
        message.quota_confirmed = _matching_bucket_blocked(previous, limits)
        return not message.quota_confirmed


def _thread_identity(session: str) -> dict:
    """The agent's session for this thread's shell only. The shared server
    carries none (BACKENDS-N1); the app-server applies a thread's config
    overrides to the commands that thread runs."""
    if not session:
        return {}
    return {"config": {"shell_environment_policy": {"set": {
        "CLAUDE_PWA_SESSION": session}}}}


def _client(agent_id: str, session: str, stream=None) -> _Client:
    """One app-server process for the Host.

    Codex serialises thread writes with an exclusive flock. One process per
    agent meant a leftover writer blocked ``thread/resume`` with -32600.
    """
    with _LOCK:
        for key, retired in list(_RETIRING.items()):
            if retired.proc.poll() is not None:
                del _RETIRING[key]
        client = _CLIENTS.get(_SHARED_KEY)
        if client is not None:
            fingerprint = _auth_fingerprint()
            changed = getattr(client, "auth_fingerprint", fingerprint) != fingerprint
            if changed or getattr(client, "refresh_requested", False):
                client.refresh_requested = True
                # No direct shutdown while a goal could be running natively
                # with its turn/started not delivered yet (the coarse rule):
                # such a process drains as a generation instead.
                if client._mark_retiring_if_quiet():
                    client.shutdown()
                    _CLIENTS.clear()
                    client = None
                elif changed:
                    # A busy process keeps its turns; the new credentials
                    # start a new generation for the next ones (BACKENDS-06b).
                    _begin_drain(client)
                    _CLIENTS.clear()
                    client = None
        if client is not None and getattr(client, "reader_dead", False):
            # Stop the unread process so it releases the thread writer lock
            # before a new one starts.
            client.retire()
            client = None
        if client is None or client.proc.poll() is not None:
            client = _Client(agent_id, session, stream=stream)
            _CLIENTS[_SHARED_KEY] = client
        elif stream is not None:
            client.stream = stream
        return client


_MOVE_PREFIX = "codex.goal_move."
# Serialises a move's compare-and-set with users' goal actions.
_GOAL_MOVE_LOCK = threading.RLock()


def _move_get(thread_id: str) -> dict | None:
    """The durable record of a goal paused for a thread move (BACKENDS-06b),
    in the settings table: an older release ignores it, and a goal left
    paused by a rollback stays visibly paused for the user to resume."""
    if not thread_id:
        return None
    value = settings_store.get(_MOVE_PREFIX + thread_id)
    try:
        return json.loads(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _move_put(thread_id: str, record: dict) -> None:
    """Through settings_store, the settings table's only writer. The record
    carries the time of its first write (the move decision, ms) for the
    recovery window; later phases keep it."""
    record = {**record, "written_at": record.get("written_at") or int(time.time() * 1000)}
    settings_store.set_text(_MOVE_PREFIX + thread_id, json.dumps(record))


def _move_forget(thread_id: str) -> None:
    settings_store.delete(_MOVE_PREFIX + thread_id)


def _move_advance(thread_id: str, expected_phase: str, record: dict) -> bool:
    """Compare-and-set: write ``record`` only while the stored record is
    still in ``expected_phase`` (a user's goal action deletes it under the
    same lock). False: the claim is gone and stays gone."""
    with _GOAL_MOVE_LOCK:
        current = _move_get(thread_id)
        if not current or current.get("phase") != expected_phase:
            return False
        _move_put(thread_id, {**record, "written_at": current.get("written_at")})
        return True


def _continue_move(thread_id: str) -> None:
    """After the move: resume the thread on the current generation and set
    its goal active again, only if it is still paused exactly as the move
    left it (compare-and-set on updatedAt). The record is cleared either
    way; a thread that no longer exists just loses its record."""
    with _GOAL_MOVE_LOCK:
        record = _move_get(thread_id)
        if not record or record.get("phase") != "moving":
            return
        agent = agents_db.get_by_backend_session(thread_id)
        if not agent:
            _move_forget(thread_id)
            log("codexGoalMoveDropped", f"thread={thread_id[:8]} reason=no-agent")
            return
        try:
            with _LOCK:
                client = _client(agent["agent_id"], agent["session"])
            client._ensure_loaded(agent, thread_id)
            result = client.request("thread/goal/get", {"threadId": thread_id})
        except RuntimeError as exc:
            if "no rollout" in str(exc).lower() or "not found" in str(exc).lower():
                _move_forget(thread_id)
                log("codexGoalMoveDropped", f"thread={thread_id[:8]} reason=gone")
                return
            log_exception("codexGoalMoveResumeFail", exc, detail=thread_id[:8])
            return
        goal = result.get("goal") if isinstance(result, dict) else None
        unchanged = (isinstance(goal, dict) and goal.get("status") == "paused"
                     and goal.get("updatedAt") == record.get("updated_at"))
        if not unchanged:
            _move_forget(thread_id)
            log("codexGoalMoveRefused", f"thread={thread_id[:8]} status={(goal or {}).get('status')}")
            return
        try:
            client.goal(agent, "resume")
        except Exception as exc:  # noqa: BLE001 - the record stays: the next pass retries
            log_exception("codexGoalMoveResumeFail", exc, detail=thread_id[:8])
            return
        _move_forget(thread_id)       # only once the goal is active again
        log("codexGoalMoveResumed", f"thread={thread_id[:8]} to={client.generation}")


# A 'pausing' or 'paused' leftover younger than this is restored at recovery:
# the surviving record proves no user goal action through Clarp since. An
# older one (a rollback and re-upgrade, a long outage) is only cleared.
MOVE_RESTORE_WINDOW_MS = 30 * 60 * 1000


def _restore_paused(thread_id: str) -> None:
    """Recovery of a recent 'pausing'/'paused' leftover: re-activate the goal
    if codex still shows it paused, then clear the record."""
    with _GOAL_MOVE_LOCK:
        if not _move_get(thread_id):
            return
        agent = agents_db.get_by_backend_session(thread_id)
        if not agent:
            _move_forget(thread_id)
            log("codexGoalMoveDropped", f"thread={thread_id[:8]} reason=no-agent")
            return
        try:
            with _LOCK:
                client = _client(agent["agent_id"], agent["session"])
            client._ensure_loaded(agent, thread_id)
            result = client.request("thread/goal/get", {"threadId": thread_id})
        except RuntimeError as exc:
            if "no rollout" in str(exc).lower() or "not found" in str(exc).lower():
                _move_forget(thread_id)
                log("codexGoalMoveDropped", f"thread={thread_id[:8]} reason=gone")
                return
            raise
        goal = result.get("goal") if isinstance(result, dict) else None
        if not isinstance(goal, dict) or goal.get("status") != "paused":
            _move_forget(thread_id)
            log("codexGoalMoveDropped", f"thread={thread_id[:8]} reason=not-paused")
            return
        try:
            client.goal(agent, "resume")
        except Exception as exc:  # noqa: BLE001 - the record stays: the next pass retries
            log_exception("codexGoalMoveResumeFail", exc, detail=thread_id[:8])
            return
        _move_forget(thread_id)       # only once the goal is active again
        log("codexGoalMoveRestored", f"thread={thread_id[:8]} to={client.generation}")


def recover_goal_moves() -> int:
    """Runtime start: finish or clear every move record a crash or a rollback
    left. A 'moving' record is continued (compare-and-set). A 'pausing' or
    'paused' one written within MOVE_RESTORE_WINDOW_MS is restored if codex
    still shows the goal paused; an older one is only cleared, and its goal
    stays visibly paused (lead, 4 Nov)."""
    keys = settings_store.keys_with_prefix(_MOVE_PREFIX)
    now_ms = int(time.time() * 1000)
    for key in keys:
        thread_id = key[len(_MOVE_PREFIX):]
        record = _move_get(thread_id) or {}
        phase = record.get("phase")
        try:
            written_at = int(record.get("written_at") or 0)
        except (TypeError, ValueError):
            written_at = 0
        try:
            if phase == "moving":
                _continue_move(thread_id)
            elif phase in ("pausing", "paused") and now_ms - written_at <= MOVE_RESTORE_WINDOW_MS:
                _restore_paused(thread_id)
            else:
                _move_forget(thread_id)
                log("codexGoalMoveDropped", f"thread={thread_id[:8]} reason=stale phase={phase or 'invalid'}")
        except Exception as exc:  # noqa: BLE001 - one record never stops the others
            log_exception("codexGoalMoveRecoverFail", exc, detail=thread_id[:8])
    return len(keys)


def _begin_drain(client: _Client) -> None:
    """Called under _LOCK: the client becomes a draining generation."""
    client.draining = True
    _DRAINING[id(client)] = client
    log("codexGenerationDraining", f"gen={client.generation} loaded={len(client._loaded_threads)} "
                                   f"running={int(_busy(client))}")
    _drain_later(client)

    def backstop() -> None:
        # Repeated until it exits: never while a turn runs on it.
        if _DRAINING.get(id(client)) is not client:
            return
        client._exit_if_drained(backstop=True)
        if _DRAINING.get(id(client)) is client:
            _schedule_backstop()

    def _schedule_backstop() -> None:
        timer = threading.Timer(DRAIN_BACKSTOP_SEC, backstop)
        timer.daemon = True
        timer.start()

    _schedule_backstop()


def _drain_later(client: _Client) -> None:
    """Off the reader thread: a release waits for its own reply."""
    threading.Thread(target=client.drain_step, daemon=True, name="codex-drain").start()


def _moving_owner(thread_id: str) -> _Client | None:
    """The draining generation that still has ``thread_id`` open."""
    if not thread_id:
        return None
    for client in list(_DRAINING.values()):
        if thread_id in client._loaded_threads and client.proc.poll() is None:
            return client
    return None


def _all_clients() -> list[_Client]:
    """A snapshot of the pool, never waiting on _LOCK: read-only lookups are
    called under the dispatcher's _TURN_LOCK and must not wait on a Codex RPC
    (TP-N3, BACKENDS-01)."""
    return list({id(client): client for client in
                 [*list(_CLIENTS.values()), *list(_DRAINING.values())]}.values())


def spawn_turn(*, text: str, cwd: pathlib.Path, backend_session_id: str = "",
               is_new_session: bool = False, session: str = "", agent_id: str = "",
               on_session_init=None, on_result=None, on_error=None,
               trace_id: str = "", stream=None, enqueue=None,
               voice_preamble: bool = False, model: str = "", effort: str = "",
               isolated: bool = False) -> AppTurnHandle:
    if isolated:
        # Isolated jobs do not need steering and retain the hardened exec path.
        return _backend().start_turn(
            text=text, cwd=cwd, backend_session_id=backend_session_id,
            is_new_session=is_new_session, session=session, agent_id=agent_id,
            on_session_init=on_session_init, on_result=on_result,
            on_error=on_error, trace_id=trace_id, stream=stream, enqueue=enqueue,
            voice_preamble=voice_preamble, model=model, effort=effort,
            isolated=True,
        )
    _LOCK.acquire()
    released = []

    def release_pool() -> None:
        if not released:
            released.append(True)
            _LOCK.release()

    try:
        client = _client(agent_id, session, stream=stream)
        admit_turn(client, agent_id)   # before turn/start: nothing is sent
        if not is_new_session and _moving_owner(backend_session_id) is not None:
            # Never open one thread in two processes (codex's writer lock
            # would refuse it anyway): the dispatcher waits for the release.
            raise CodexSessionStuck(
                "Waiting for the previous Codex session to release this conversation")
        handle = AppTurnHandle(client, agent_id=agent_id)
        agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
        persona = (agent or {}).get("persona") or ""
        # The decisions above and the turn's registration happen under the
        # pool lock; the start's RPCs run after it is released (TP-N3).
        client.start(text=text, cwd=cwd, backend_session_id=backend_session_id,
                     is_new_session=is_new_session, session=session,
                     trace_id=trace_id, model=model, effort=effort,
                     voice=voice_preamble, persona=persona, handle=handle,
                     on_session_init=on_session_init, on_result=on_result,
                     on_error=on_error, stream=stream,
                     enqueue=enqueue or tts_queue.enqueue, agent_id=agent_id,
                     on_claimed=release_pool)
    finally:
        release_pool()
    log("codexAppTurnStart", f"agent={agent_id} thread={client.thread_id} trace={trace_id}")
    return handle



def fork_thread(thread_id: str, *, cwd: str = "") -> str:
    """Fork ``thread_id`` into a new thread and return its id.

    A one-off app-server rather than the pool: the fork runs on the Host while
    the source thread may be mid-turn in the runtime's process, and Codex
    forks without taking the source's writer lock (checked on 0.159).
    """
    client = _Client("", "")
    try:
        result = client.request("thread/fork", {
            "threadId": thread_id, **({"cwd": cwd} if cwd else {})})
    except RuntimeError as exc:
        if "no rollout found" in str(exc):
            raise FileNotFoundError(f"no Codex thread {thread_id}") from exc
        raise
    finally:
        client.shutdown()
    new = str((result.get("thread") or {}).get("id") or "")
    if not new:
        raise RuntimeError("Codex app-server returned no forked thread id")
    return new


def goal(agent_id: str, action: str, *, objective: str = "", stream=None) -> dict | None:
    """Start, pause, resume, clear or read a Codex agent's goal (see _Client.goal)."""
    agent = agents_db.get_by_agent_id(agent_id)
    if not agent:
        raise ValueError("no such agent")
    thread_id = agents_db.live_backend_session(agent_id)
    with _GOAL_MOVE_LOCK:
        with _LOCK:
            # During a move the thread is still open in the draining
            # generation: act there, never open it twice (BACKENDS-06b).
            # _client() first: it may itself turn the current process into a
            # draining generation (a credential change), so the owner is
            # resolved after it.
            current = _client(agent_id, agent["session"], stream=stream)
            client = _moving_owner(thread_id) or current
        if action != "get" and thread_id:
            # The user's own word on the goal ends a move's claim and
            # replaces a pending Stop-pause.
            _move_forget(thread_id)
            _stop_pause_forget(thread_id)
        return client.goal(agent, action, objective)


def _shared_client() -> _Client | None:
    """Read-only, never waiting on _LOCK (see _all_clients)."""
    client = _CLIENTS.get(_SHARED_KEY)
    if client is None:
        for item in list(_CLIENTS.values()):
            return item
    return client


def start_pending(agent_id: str) -> bool:
    """The agent's turn is registered but its start has not been confirmed
    (turn/started not seen yet): a follow-up waits for it instead of
    preempting it or steering into nothing (TP-N3)."""
    for client in _all_clients():
        active = getattr(client, "_actives", {}).get(agent_id)
        if active is not None and active.handle.is_alive() and not active.steer_ready.is_set():
            return True
    return False


def _client_running(agent_id: str) -> _Client | None:
    """The generation where ``agent_id`` has a live turn: the current one or
    a draining one (BACKENDS-06b), so Stop and steering reach it there."""
    for client in _all_clients():
        active = getattr(client, "_actives", {}).get(agent_id) or (
            client.active if getattr(client.active, "agent_id", None) == agent_id
            else None)
        if active and active.handle.is_alive():
            return client
    return None


def steer(agent_id: str, text: str, *, client_msg_id: str = "",
          synthesize_audio: bool = False) -> bool:
    client = _client_running(agent_id) or _shared_client()
    return bool(client and client.steer(
        text, client_msg_id, synthesize_audio, agent_id=agent_id))


def active_handles(agent_id: str) -> list[AppTurnHandle]:
    for client in _all_clients():
        active = getattr(client, "_actives", {}).get(agent_id) or (
            client.active if getattr(client.active, "agent_id", None) == agent_id
            else None)
        if active and active.handle.is_alive():
            return [active.handle]
    return []


def busy_agents() -> set[str]:
    """Agents with a live turn on any app-server connection, including goal
    continuations the server started itself (adopted, without a Clarp slot)."""
    clients = _all_clients()
    busy: set[str] = set()
    for client in clients:
        turns = list(getattr(client, "_actives", {}).values())
        if getattr(client, "active", None) is not None:
            turns.append(client.active)
        busy.update(str(turn.agent_id) for turn in turns
                    if turn is not None and not turn.handle._done.is_set())
    return busy


def interrupt(agent_id: str) -> int:
    handles = active_handles(agent_id)
    if handles:
        for handle in handles:
            handle.terminate()
        return len(handles)
    client = _client_running(agent_id) or _shared_client()
    if client is None:
        return 0
    client.interrupt_active(agent_id, by_user=True)
    return 0


def recycle_clients() -> int:
    """Retire idle connections; active connections refresh on next idle admission."""
    with _LOCK:
        clients = list({id(client): client for client in _CLIENTS.values()}.values())
        closed = 0
        for client in clients:
            client.refresh_requested = True
            if not client._mark_retiring_if_quiet():
                continue
            client.shutdown()
            for key, value in list(_CLIENTS.items()):
                if value is client:
                    del _CLIENTS[key]
            closed += 1
        return closed
