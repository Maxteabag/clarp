"""Persistent Codex app-server transport with genuine mid-turn steering.

Unlike ``codex exec`` (one process per turn), app-server keeps a thread alive
and exposes ``turn/steer``.  Clarp uses one lightweight server connection per
agent so a follow-up can join the active turn without cancelling its work.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from . import agents as agents_db, backend_usage, tts_queue
from .codex_runner import (
    CODEX_BIN, _TurnState, _broadcast_transcript, _handle_item, _record_state,
    _persist_live_text, _speak, app_turn_instructions, persona_identity_instruction,
)
from .log import log, log_exception
from .protocol import AgentState


@dataclass
class _ActiveTurn:
    turn_id: str
    thread_id: str
    agent_id: str
    session: str
    trace_id: str
    state: _TurnState
    handle: "AppTurnHandle"
    on_result: Callable[[dict], None] | None
    on_error: Callable[[str], None] | None
    stream: Any
    enqueue: Callable[..., int]
    voice: bool = False
    steer_ready: threading.Event = field(default_factory=threading.Event)


class AppTurnHandle:
    """ProcessRegistry-compatible logical handle for one app-server turn."""
    def __init__(self, client: "_Client"):
        self.client = client
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
        self.client.interrupt_active()


class _Client:
    def __init__(self, agent_id: str, session: str, stream=None):
        if shutil.which(CODEX_BIN) is None:
            raise FileNotFoundError(f"`{CODEX_BIN}` not on PATH")
        self.agent_id = agent_id
        env = {**os.environ, "CLAUDE_PWA_SESSION": session}
        self.proc = subprocess.Popen(
            [CODEX_BIN, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            env=env,
        )
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self.active: _ActiveTurn | None = None
        self.thread_id = ""
        self.stream = stream
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

    def request(self, method: str, params: dict, timeout: float = 30) -> dict:
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            done = threading.Event()
            box: dict = {}
            self._pending[rid] = (done, box)
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
                if "id" in message and ("result" in message or "error" in message):
                    with self._lock:
                        pending = self._pending.pop(message["id"], None)
                    if pending:
                        done, box = pending
                        box.update(message)
                        done.set()
                    continue
                method = str(message.get("method") or "")
                params = message.get("params") or {}
                if "id" in message:  # unexpected server request: reject safely
                    self._send({"id": message["id"], "error": {
                        "code": -32601, "message": "unsupported by headless Clarp"
                    }})
                    continue
                self._notification(method, params)
        except Exception as exc:  # noqa: BLE001
            failure = str(exc) or failure
            log_exception("codexAppServerReadFail", exc, detail=self.agent_id)
        finally:
            self._fail_all(failure)

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
        for done, box in pending:
            box["error"] = {"message": message}
            done.set()
        active = self.active
        self.active = None
        if active and not active.handle._done.is_set():
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

    def _notification(self, method: str, params: dict) -> None:
        active = self.active
        if method == "account/rateLimits/updated":
            try:
                snapshot = backend_usage.capture_codex_rate_limits(params)
                event_stream = (active.stream if active is not None
                                else getattr(self, "stream", None))
                if event_stream is not None:
                    for event in snapshot.get("limit_events") or []:
                        event_stream.broadcast(event)
            except Exception as exc:  # noqa: BLE001
                log_exception("codexRateLimitsUpdateFail", exc,
                              detail=self.agent_id)
            return
        if active is None:
            return
        if method == "turn/started":
            notified_turn = params.get("turn") or {}
            active.turn_id = str(notified_turn.get("id") or active.turn_id)
            active.steer_ready.set()
            _record_state(active.agent_id, AgentState.THINKING,
                          {"dispatch": "codex", "trace_id": active.trace_id})
            return
        if method == "thread/tokenUsage/updated":
            usage = (params.get("tokenUsage") or {}).get("last") or {}
            active.state.tokens_in = int(usage.get("inputTokens") or 0)
            active.state.tokens_out = int(usage.get("outputTokens") or 0)
            return
        if method in ("item/started", "item/updated", "item/completed"):
            item = params.get("item")
            if isinstance(item, dict):
                normalized = _normalize_item(item)
                phase = method.replace("/", ".")
                _handle_item(phase, normalized, active.state,
                             agent_id=active.agent_id, session=active.session,
                             trace_id=active.trace_id, stream=active.stream,
                             enqueue=active.enqueue)
            return
        if method == "item/agentMessage/delta":
            delta = params.get("delta") or params.get("text") or ""
            if isinstance(delta, dict):
                delta = delta.get("text") or ""
            if isinstance(delta, str) and delta:
                current = active.state.pending_live_text
                # Protocol deltas are normally incremental chunks, but tolerate
                # servers that send a progressively complete snapshot.
                merged = delta if delta.startswith(current) else current + delta
                active.state.last_agent_message = merged
                _persist_live_text(
                    active.state, text=merged, agent_id=active.agent_id,
                    session=active.session, trace_id=active.trace_id,
                    stream=active.stream)
            return
        if method == "turn/completed":
            turn = params.get("turn") or {}
            status = str(turn.get("status") or "completed")
            error = turn.get("error") or {}
            _persist_live_text(
                active.state, agent_id=active.agent_id,
                session=active.session, trace_id=active.trace_id,
                stream=active.stream, force=True)
            self.active = None
            active.handle._done.set()
            if status == "failed":
                message = str(error.get("message") or "codex turn failed")
                if active.on_error:
                    self._callback(active.on_error, message)
            elif status == "interrupted":
                if active.on_error:
                    self._callback(active.on_error, "codex turn interrupted")
            elif active.on_result:
                self._callback(active.on_result, {
                    "usage": {"input_tokens": active.state.tokens_in,
                              "output_tokens": active.state.tokens_out},
                    "last_agent_message": active.state.last_agent_message,
                })
            _broadcast_transcript(active.stream, active.agent_id, active.session)

    def start(self, *, text: str, cwd: pathlib.Path, backend_session_id: str,
              is_new_session: bool, session: str, trace_id: str, model: str,
              effort: str, voice: bool, persona: str,
              handle: AppTurnHandle, on_session_init, on_result,
              on_error, stream, enqueue) -> None:
        cwd = pathlib.Path(os.path.expanduser(str(cwd))).resolve()
        developer_instructions = persona_identity_instruction(persona, session)
        # Claim the logical slot before any blocking RPC. A simultaneous send
        # then sees a live starting handle and waits to steer instead of
        # declaring the dispatch stale and starting an overlapping turn.
        active = _ActiveTurn(
            "", backend_session_id, self.agent_id, session, trace_id,
            _TurnState(live_backend_session_id=backend_session_id),
            handle, on_result, on_error, stream, enqueue, voice,
        )
        self.stream = stream
        self.active = active
        try:
            if backend_session_id and not is_new_session:
                result = self.request("thread/resume", {
                    "threadId": backend_session_id, "cwd": str(cwd),
                    "approvalPolicy": "never", "sandbox": "danger-full-access",
                    "developerInstructions": developer_instructions,
                    **({"model": model} if model else {}),
                })
            else:
                result = self.request("thread/start", {
                    "cwd": str(cwd), "approvalPolicy": "never",
                    "sandbox": "danger-full-access",
                    "developerInstructions": developer_instructions,
                    **({"model": model} if model else {}),
                })
            thread = result.get("thread") or {}
            thread_id = str(thread.get("id") or backend_session_id)
            if not thread_id:
                raise RuntimeError("Codex app-server returned no thread id")
            self.thread_id = thread_id
            active.thread_id = thread_id
            active.state.live_backend_session_id = thread_id
            if on_session_init:
                if on_session_init(thread_id) is False:
                    raise RuntimeError("backend session binding rejected")
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
            })
            turn = turn_result.get("turn") or {}
            turn_id = str(turn.get("id") or "")
            if not turn_id:
                raise RuntimeError("Codex app-server returned no turn id")
            active.turn_id = turn_id
        except Exception:
            if self.active is active:
                self.active = None
            active.handle._done.set()
            raise

    def steer(self, text: str, client_msg_id: str = "",
              synthesize_audio: bool = False) -> bool:
        active = self.active
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
                "value": app_turn_instructions(voice=True, session=active.session),
            }}
            active.voice = True
            agents_db.enable_latest_turn_audio(active.agent_id)
        self.request("turn/steer", params)
        return True

    def interrupt_active(self) -> None:
        active = self.active
        if active:
            if not active.turn_id:
                self.proc.terminate()
                return
            self.request("turn/interrupt", {
                "threadId": active.thread_id, "turnId": active.turn_id,
            })


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
        "reasoning": "reasoning",
    }
    out["type"] = kinds.get(str(out.get("type")), out.get("type"))
    if out.get("type") == "agent_message" and not out.get("text"):
        out["text"] = out.get("message") or ""
    return out


_CLIENTS: dict[str, _Client] = {}
_LOCK = threading.Lock()


def _client(agent_id: str, session: str, stream=None) -> _Client:
    with _LOCK:
        client = _CLIENTS.get(agent_id)
        if client is None or client.proc.poll() is not None:
            client = _Client(agent_id, session, stream=stream)
            _CLIENTS[agent_id] = client
        elif stream is not None:
            client.stream = stream
        return client


def spawn_turn(*, text: str, cwd: pathlib.Path, backend_session_id: str = "",
               is_new_session: bool = False, session: str = "", agent_id: str = "",
               on_session_init=None, on_result=None, on_error=None,
               trace_id: str = "", stream=None, enqueue=None,
               voice_preamble: bool = False, model: str = "", effort: str = "",
               isolated: bool = False) -> AppTurnHandle:
    if isolated:
        # Isolated jobs do not need steering and retain the hardened exec path.
        from . import codex_runner
        return codex_runner.spawn_turn(
            text=text, cwd=cwd, backend_session_id=backend_session_id,
            is_new_session=is_new_session, session=session, agent_id=agent_id,
            on_session_init=on_session_init, on_result=on_result,
            on_error=on_error, trace_id=trace_id, stream=stream, enqueue=enqueue,
            voice_preamble=voice_preamble, model=model, effort=effort,
            isolated=True,
        )
    client = _client(agent_id, session, stream=stream)
    handle = AppTurnHandle(client)
    agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
    persona = (agent or {}).get("persona") or ""
    client.start(text=text, cwd=cwd, backend_session_id=backend_session_id,
                 is_new_session=is_new_session, session=session,
                 trace_id=trace_id, model=model, effort=effort,
                 voice=voice_preamble, persona=persona, handle=handle,
                 on_session_init=on_session_init, on_result=on_result,
                 on_error=on_error, stream=stream,
                 enqueue=enqueue or tts_queue.enqueue)
    log("codexAppTurnStart", f"agent={agent_id} thread={client.thread_id} trace={trace_id}")
    return handle


def steer(agent_id: str, text: str, *, client_msg_id: str = "",
          synthesize_audio: bool = False) -> bool:
    with _LOCK:
        client = _CLIENTS.get(agent_id)
    return bool(client and client.steer(text, client_msg_id, synthesize_audio))


def active_handles(agent_id: str) -> list[AppTurnHandle]:
    with _LOCK:
        client = _CLIENTS.get(agent_id)
    active = client.active if client else None
    return [active.handle] if active and active.handle.is_alive() else []


def interrupt(agent_id: str) -> int:
    handles = active_handles(agent_id)
    for handle in handles:
        handle.terminate()
    return len(handles)
