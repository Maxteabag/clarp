"""Persistent Codex app-server transport with genuine mid-turn steering.

Unlike ``codex exec`` (one process per turn), app-server keeps a thread alive
and exposes ``turn/steer``.  Clarp keeps **one** stdio app-server for the Host
and multiplexes agents by ``threadId``. One process per agent left leftover
writers that blocked ``thread/resume`` with JSON-RPC ``-32600``.
"""
from __future__ import annotations

import json
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from . import agents as agents_db, backend_usage, tts_queue
from . import codex_runner
from .codex_runner import (
    _TurnState, _broadcast_transcript, _handle_item, _record_state,
    _persist_live_text, _speak, app_turn_instructions, persona_identity_instruction,
)
from .log import log, log_exception
from .protocol import AgentState


def _codex_argv() -> list[str]:
    """Resolve ``codex app-server --stdio`` from the live runner binary.

    Read ``codex_runner.CODEX_BIN`` at spawn time so the QA host (and tests)
    can point at ``fake_codex.py`` without also patching this module. A
    ``.py`` path is launched with the current interpreter.
    """
    binary = str(codex_runner.CODEX_BIN)
    if os.path.isfile(binary) and binary.endswith(".py"):
        return [sys.executable, binary, "app-server", "--stdio"]
    found = shutil.which(binary)
    if found is None and os.path.isfile(binary) and os.access(binary, os.X_OK):
        found = binary
    if found is None:
        raise FileNotFoundError(f"`{binary}` not on PATH")
    return [found, "app-server", "--stdio"]


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
    produced_output: bool = False
    steer_ready: threading.Event = field(default_factory=threading.Event)


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
        self.client.interrupt_active(self.agent_id)


class _Client:
    def __init__(self, agent_id: str, session: str, stream=None):
        argv = _codex_argv()
        self.agent_id = agent_id
        self.auth_fingerprint = _auth_fingerprint()
        self.refresh_requested = False
        self.rate_limits = {}
        env = {**os.environ, "CLAUDE_PWA_SESSION": session}
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            env=env,
        )
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self.active: _ActiveTurn | None = None
        self._actives: dict[str, _ActiveTurn] = {}
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
        actives = list(getattr(self, "_actives", {}).values())
        if self.active is not None and self.active not in actives:
            actives.append(self.active)
        self.active = None
        if hasattr(self, "_actives"):
            self._actives.clear()
        for active in actives:
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

    def _pick_active(self, params: dict) -> _ActiveTurn | None:
        thread_id = str(params.get("threadId") or "")
        turn = params.get("turn")
        if not thread_id and isinstance(turn, dict):
            thread_id = str(turn.get("threadId") or "")
        if thread_id:
            for active in list(getattr(self, "_actives", {}).values()):
                if active.thread_id == thread_id:
                    return active
        return self.active

    def _notification(self, method: str, params: dict) -> None:
        active = self._pick_active(params)
        if method == "account/rateLimits/updated":
            self.rate_limits = params
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
                active.produced_output = True
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
                active.produced_output = True
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
            if hasattr(self, "_actives"):
                self._actives.pop(active.agent_id, None)
            if self.active is active:
                self.active = None
            active.handle._done.set()
            if status == "failed":
                message = str(error.get("message") or "codex turn failed")
                if active.on_error:
                    self._callback(active.on_error, CodexTurnFailure(
                        message, self, active.produced_output))
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
              on_error, stream, enqueue, agent_id: str = "") -> None:
        cwd = pathlib.Path(os.path.expanduser(str(cwd))).resolve()
        owner = agent_id or self.agent_id
        developer_instructions = persona_identity_instruction(persona, session)
        # Claim the logical slot before any blocking RPC. A simultaneous send
        # then sees a live starting handle and waits to steer instead of
        # declaring the dispatch stale and starting an overlapping turn.
        active = _ActiveTurn(
            "", backend_session_id, owner, session, trace_id,
            _TurnState(live_backend_session_id=backend_session_id),
            handle, on_result, on_error, stream, enqueue, voice,
        )
        self.stream = stream
        self.active = active
        if not hasattr(self, "_actives"):
            self._actives = {}
        self._actives[owner] = active
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
            self._actives.pop(owner, None)
            if self.active is active:
                self.active = None
            active.handle._done.set()
            raise

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
                "value": app_turn_instructions(voice=True, session=active.session),
            }}
            active.voice = True
            agents_db.enable_latest_turn_audio(active.agent_id)
        self.request("turn/steer", params)
        return True

    def interrupt_active(self, agent_id: str = "") -> None:
        active = self._actives.get(agent_id) if agent_id else self.active
        if active:
            if not active.turn_id:
                if len(getattr(self, "_actives", {})) <= 1:
                    self.proc.terminate()
                return
            self.request("turn/interrupt", {
                "threadId": active.thread_id, "turnId": active.turn_id,
            })

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
        "reasoning": "reasoning",
    }
    out["type"] = kinds.get(str(out.get("type")), out.get("type"))
    if out.get("type") == "agent_message" and not out.get("text"):
        out["text"] = out.get("message") or ""
    return out


_CLIENTS: dict[str, _Client] = {}
_LOCK = threading.RLock()
_SHARED_KEY = "__shared__"


def _auth_fingerprint() -> bytes:
    # Private generation marker only; never log credential contents or hashes.
    try:
        return hashlib.sha256((backend_usage._codex_home() / "auth.json").read_bytes()).digest()
    except OSError:
        return b""


def _busy(client) -> bool:
    turns = list(getattr(client, "_actives", {}).values())
    active = getattr(client, "active", None)
    if active is not None:
        turns.append(active)
    return any(not turn.handle._done.is_set() for turn in turns)


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
        if _busy(client) or message.produced_output:
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


def _client(agent_id: str, session: str, stream=None) -> _Client:
    """One app-server process for the Host.

    Codex serialises thread writes with an exclusive flock. One process per
    agent meant a leftover writer blocked ``thread/resume`` with -32600.
    """
    with _LOCK:
        client = _CLIENTS.get(_SHARED_KEY)
        if client is not None:
            fingerprint = _auth_fingerprint()
            changed = getattr(client, "auth_fingerprint", fingerprint) != fingerprint
            if changed or getattr(client, "refresh_requested", False):
                client.refresh_requested = True
                if not _busy(client):
                    client.shutdown()
                    _CLIENTS.clear()
                    client = None
        if client is None or client.proc.poll() is not None:
            client = _Client(agent_id, session, stream=stream)
            _CLIENTS[_SHARED_KEY] = client
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
    with _LOCK:
        client = _client(agent_id, session, stream=stream)
        handle = AppTurnHandle(client, agent_id=agent_id)
        agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
        persona = (agent or {}).get("persona") or ""
        client.start(text=text, cwd=cwd, backend_session_id=backend_session_id,
                     is_new_session=is_new_session, session=session,
                     trace_id=trace_id, model=model, effort=effort,
                     voice=voice_preamble, persona=persona, handle=handle,
                     on_session_init=on_session_init, on_result=on_result,
                     on_error=on_error, stream=stream,
                     enqueue=enqueue or tts_queue.enqueue, agent_id=agent_id)
        log("codexAppTurnStart", f"agent={agent_id} thread={client.thread_id} trace={trace_id}")
        return handle



def _shared_client() -> _Client | None:
    with _LOCK:
        client = _CLIENTS.get(_SHARED_KEY)
        if client is None:
            for item in _CLIENTS.values():
                return item
        return client


def steer(agent_id: str, text: str, *, client_msg_id: str = "",
          synthesize_audio: bool = False) -> bool:
    client = _shared_client()
    return bool(client and client.steer(
        text, client_msg_id, synthesize_audio, agent_id=agent_id))


def active_handles(agent_id: str) -> list[AppTurnHandle]:
    client = _shared_client()
    if client is None:
        return []
    active = getattr(client, "_actives", {}).get(agent_id) or (
        client.active if getattr(client.active, "agent_id", None) == agent_id
        else None)
    return [active.handle] if active and active.handle.is_alive() else []


def interrupt(agent_id: str) -> int:
    handles = active_handles(agent_id)
    if handles:
        for handle in handles:
            handle.terminate()
        return len(handles)
    client = _shared_client()
    if client is None:
        return 0
    client.interrupt_active(agent_id)
    return 0


def recycle_clients() -> int:
    """Retire idle connections; active connections refresh on next idle admission."""
    with _LOCK:
        clients = list({id(client): client for client in _CLIENTS.values()}.values())
        closed = 0
        for client in clients:
            client.refresh_requested = True
            if _busy(client):
                continue
            client.shutdown()
            for key, value in list(_CLIENTS.items()):
                if value is client:
                    del _CLIENTS[key]
            closed += 1
        return closed
