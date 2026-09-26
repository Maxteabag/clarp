"""Codex: the app-server runner, goal protocol and account failover.

The steerable path is the long-lived app-server (``lib.codex_app_server``,
a connection pool, not a strategy): ``spawn_turn``, ``interrupt`` and
``active_handles`` go there. The bodies here are the per-turn ``codex exec
--json`` path it falls back to for isolated jobs (``start_turn``), plus the
event mapping both paths share (``handle_item``, ``update_live_text`` and
the shared ``transition``/``broadcast_transcript``), which the app-server
client calls on the backend object.

Codex has neither Claude Code's hook system nor an inotify transcript
watcher driving the PWA, so everything the PWA needs is read straight off
the single stdout event stream:

    session_meta        → bind the conversation UUID (on_session_init)
    task_started        → AgentState.THINKING
    agent_message       → assistant text → <speak> extraction → TTS,
                          plus a transcript-updated SSE for the history pane
    function_call /     → AgentState.TOOL + a transcript-updated SSE
      custom_tool_call
    context_compacted   → AgentState.COMPACTING
    task_complete       → terminal event for the turn (on_result), the
                          server flips the agent back to IDLE
    turn_aborted        → on_error / IDLE

Codex still writes its own rollout JSONL under
~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl, so the history
pane is served by re-reading that file (see lib/codex_transcript.py) —
we only need to broadcast "something changed" so the client refetches.

The runner is fire-and-forget: it returns the TurnHandle and callbacks
fire from the drainer thread, mirroring the Claude runner's contract so the
/send handler can treat both backends uniformly.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import tomllib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .. import agents as agents_db
from .. import codex_transcript
from .. import tts_queue
from ..log import log, log_exception
from ..proc_util import stderr_text
from ..process_registry import TurnHandle
from ..turn_lifecycle import TurnEvent
from ..voice_preamble import apply_voice_preamble
from .base import BackendBrand, CompactionStrategy, resolve
from .stream_json import StreamJsonBackend


@dataclass
class TurnState:
    """Accumulators for one turn's drainer thread."""
    saw_session: bool = False
    failed_error: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    last_agent_message: str = ""
    spoke_any: bool = False
    seen_speak: set[str] = field(default_factory=set)
    live_backend_session_id: str = ""
    pending_live_text: str = ""
    persisted_live_text: str = ""
    last_live_write_at: float = 0.0


# ---- event extraction helpers (tolerant of envelope vs flat shapes) ----

def _event_parts(ev: dict) -> tuple[str, dict]:
    """Return (inner_type, payload) for a codex event line.

    Codex emits both an envelope shape `{type, payload:{type,...}}` (its
    rollout files) and, depending on version, flatter shapes on the
    `--json` stdout stream. Normalise to the inner type + a payload dict
    so the matcher below doesn't care which shape arrived.
    """
    payload = ev.get("payload")
    if isinstance(payload, dict):
        inner = payload.get("type") or ev.get("type") or ""
        return str(inner), payload
    # Flat: the event itself is the payload.
    return str(ev.get("type") or ""), ev


def _session_id_from(ev: dict, payload: dict) -> str:
    for src in (payload, ev):
        for key in ("id", "session_id", "sessionId", "thread_id", "threadId"):
            v = src.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _agent_text(payload: dict) -> str:
    """Pull the spoken/assistant text out of an agent_message / message."""
    msg = payload.get("message")
    if isinstance(msg, str):
        return msg
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(str(c.get("text") or ""))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(p for p in parts if p)
    return ""


# Item types codex emits inside item.* events that represent the agent
# doing work (vs. speaking). Any of these flips the agent to TOOL.
_TOOL_ITEM_TYPES = {
    "command_execution", "function_call", "local_shell_call", "tool_call",
    "file_change", "patch_apply", "mcp_tool_call", "dynamic_tool_call",
    "collab_tool_call", "collab_agent_tool_call", "web_search",
    "web_search_call", "image_view", "image_generation",
}


class CodexBackend(StreamJsonBackend):
    """Runs through the long-lived Codex app-server (``codex_app_server``)."""
    # --- catalogue data ---------------------------------------------------
    id = 'codex'
    label = 'Codex'
    required_binary = 'codex'
    badge = 'BackendCodex'
    detail = 'Runs on the Codex CLI.'
    symbol = 'terminal'
    brand = BackendBrand('#2b2f3c', '#14161d', '#c0caf5', '#3c4257')
    supports_steer = True
    supports_usage = True
    login_kind = 'device_code'
    efforts = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')
    effort_scope = 'model'
    runner = 'codex'
    config_model_field = 'codex_model'
    config_effort_field = 'codex_reasoning_effort'
    config_account_switch_field = 'codex_account_switch_command'
    api_providers = ('openai',)
    native_tool_explainer = True
    janitor_default_model = 'gpt-5.3-codex-spark'
    model_family = 'codex'
    fallback_models = (
        ('gpt-5.4', 'GPT-5.4'),
        ('gpt-5.4-mini', 'GPT-5.4 Mini'),
        ('gpt-5.2-codex', 'GPT-5.2 Codex'),
        ('gpt-5.1-codex-max', 'GPT-5.1 Codex Max'),
        ('gpt-5.1-codex', 'GPT-5.1 Codex'),
        ('gpt-5-codex', 'GPT-5 Codex'),
    )


    transcript = codex_transcript

    # --- the runner: app-server transport ----------------------------------

    def spawn_turn(self, **spec: Any):
        """Through the app-server, which keeps ``turn/steer``; it uses
        ``start_turn`` (``codex exec``) itself for isolated jobs."""
        kwargs = {k: v for k, v in spec.items() if k not in self.dropped_spawn_kwargs}
        return resolve("codex_app_server", "spawn_turn")(**kwargs)

    def interrupt(self, agent_id: str) -> int:
        return int(resolve("codex_app_server", "interrupt")(agent_id) or 0)

    def active_handles(self, agent_id: str) -> list:
        return list(resolve("codex_app_server", "active_handles")(agent_id) or [])

    def goal(self, agent_id: str, action: str, *, objective: str = "",
             stream: Any = None) -> dict | None:
        """The app-server's ``thread/goal`` protocol."""
        return resolve("codex_app_server", "goal")(
            agent_id, action, objective=objective, stream=stream)

    def steer(self, agent_id: str, text: str, *, client_msg_id: str = "",
              synthesize_audio: bool = False) -> bool:
        """``turn/steer`` on the live app-server turn."""
        return bool(resolve("codex_app_server", "steer")(
            agent_id, text, client_msg_id=client_msg_id,
            synthesize_audio=synthesize_audio))

    def interrupt_exec(self, agent_id: str) -> int:
        """SIGTERM every in-flight ``codex exec`` turn for an agent. Idempotent."""
        return self._registry.interrupt(agent_id, event=f"{self.runner}InterruptFail")

    def interrupt_all(self, agent_id: str) -> int:
        """The app-server turn and any isolated ``codex exec`` turns."""
        return self.interrupt(agent_id) + self.interrupt_exec(agent_id)

    def active_exec_handles(self, agent_id: str) -> list[TurnHandle]:
        return self._registry.active_handles(agent_id)

    # --- the runner: ``codex exec`` path -----------------------------------

    def build_cmd(self, session_id: str = "", *, is_new_session: bool = False,
                  model: str = "", reasoning_effort: str = "",
                  isolated: bool = False) -> list[str]:
        """argv for one `codex exec` turn (prompt appended by the caller).

        Fresh turn   →  codex exec --json --dangerously-bypass-approvals-and-sandbox
        Known session→  …same flags… resume <uuid>

        Unlike Claude (`--session-id <uuid>` lets us pre-pick the id), Codex
        assigns the conversation UUID itself and reports it in the
        `session_meta` event — so a fresh turn carries no id and we bind
        whatever Codex hands back. `is_new_session` is accepted for a uniform
        signature with the Claude runner's build_cmd but only `session_id`
        decides resume vs fresh.

        `model` / `reasoning_effort` are optional latency knobs (config
        [agents]). Empty → Codex defaults. Lowering reasoning effort is the
        biggest lever on time-to-first-word for hands-free voice turns.
        """
        base = [self.required_binary, "exec", "--json"]
        if isolated:
            # Routing runs in the workspace root, which need not be a git repo;
            # without the check skipped Codex refuses to start at all.
            base += ["--sandbox", "workspace-write", "--ephemeral",
                     "--skip-git-repo-check"]
        else:
            base.append("--dangerously-bypass-approvals-and-sandbox")
        if model:
            base += ["--model", model]
        if reasoning_effort:
            base += ["-c", f"model_reasoning_effort={reasoning_effort}"]
        if session_id and not is_new_session:
            base += ["resume", session_id]
        return base

    def start_turn(
        self,
        *,
        text: str,
        cwd: pathlib.Path,
        backend_session_id: str = "",
        is_new_session: bool = False,
        session: str = "",
        agent_id: str = "",
        on_session_init: Optional[Callable[[str], None]] = None,
        on_result: Optional[Callable[[dict], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        trace_id: str = "",
        stream: Any = None,
        enqueue: Optional[Callable[..., int]] = None,
        voice_preamble: bool = False,
        model: str = "",
        effort: str = "",
        isolated: bool = False,
    ) -> TurnHandle:
        """Spawn `codex exec` for ONE turn and drive progress off its stdout.

        Signature is a superset of the Claude runner's: the extra
        `stream` (SSE broadcaster) and `enqueue` (TTS sink, defaults to
        tts_queue.enqueue) let the Codex backend reproduce the state/TTS/
        history side-effects that Claude gets from hooks + the transcript
        watcher.

        Raises FileNotFoundError if `codex` isn't on PATH.
        """
        codex_bin = self.required_binary
        if shutil.which(codex_bin) is None:
            raise FileNotFoundError(
                f"`{codex_bin}` not on PATH — install the Codex CLI "
                f"(npm i -g @openai/codex) to run Codex-backed agents"
            )
        # Defensive: never hand Popen a literal "~" — the OS won't expand it and
        # the spawn fails with FileNotFoundError. Agents created with the default
        # working dir store the bare tilde.
        cwd = pathlib.Path(os.path.expanduser(str(cwd)))
        cmd = self.build_cmd(
            backend_session_id, is_new_session=is_new_session,
            model=model, reasoning_effort=effort, isolated=isolated)
        # Every codex turn is app-dispatched, so the instruction block is always
        # prepended (Codex has no hook to inject it like Claude does): the
        # no-interactive-questions rule always, plus the <speak> voice guidance
        # when this is a spoken turn.
        agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
        persona = (agent or {}).get("persona") or ""
        cmd.append(apply_voice_preamble(
            text,
            voice=voice_preamble,
            persona=persona,
            session=session,
        ))
        flag = "resume" if (backend_session_id and not is_new_session) else "new"
        log("codexSpawn", f"cwd={cwd} {flag}={backend_session_id or '∅'} "
                          f"text_len={len(text)} trace={trace_id or '∅'} "
                          f"agent={agent_id or '∅'}")
        proc, handle = self.launch(cmd, cwd=cwd, session=session)
        runtime_agent_id = "" if isolated else agent_id
        self.register_handle(runtime_agent_id, handle)
        self.start_drain(
            handle, self._drain_stdout,
            proc=proc, agent_id=runtime_agent_id, session=session,
            trace_id=trace_id, handle=handle,
            backend_session_id=backend_session_id,
            on_session_init=on_session_init, on_result=on_result,
            on_error=on_error, stream=stream,
            enqueue=enqueue or tts_queue.enqueue,
        )
        return handle

    # ---- the drainer -------------------------------------------------------

    def _drain_stdout(
        self,
        *,
        proc: subprocess.Popen,
        agent_id: str,
        session: str,
        trace_id: str,
        backend_session_id: str,
        handle: TurnHandle,
        on_session_init: Optional[Callable[[str], None]],
        on_result: Optional[Callable[[dict], None]],
        on_error: Optional[Callable[[str], None]],
        stream: Any,
        enqueue: Callable[..., int],
    ) -> None:
        """Background thread: parse codex stream-json and fire side-effects.

        Swallows every exception per-line — a drainer crash must never take
        down a server thread."""
        st = TurnState(live_backend_session_id=backend_session_id)
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue                       # banner / non-JSON noise
                if not isinstance(ev, dict):
                    continue
                if st.failed_error:
                    continue
                try:
                    self._handle_event(ev, st, agent_id=agent_id,
                                       session=session, trace_id=trace_id,
                                       on_session_init=on_session_init,
                                       on_error=on_error,
                                       stream=stream, enqueue=enqueue)
                except Exception as e:             # noqa: BLE001
                    log_exception("codexEventFail", e, detail=trace_id)

            rc = proc.wait()
            if rc != 0:
                err = stderr_text(proc)
                log("codexExitErr", f"rc={rc} trace={trace_id or '∅'} "
                                    f"stderr={(err or '')[:500]!r}")
                if on_error is not None and not st.failed_error:
                    try:
                        on_error(err or f"codex exited rc={rc}")
                    except Exception as e:         # noqa: BLE001
                        log_exception("codexOnErrorFail", e, detail=trace_id)
            else:
                # Clean exit → synthesise a Claude-shaped result event so the
                # server's shared on_result (IDLE + per-turn banner) just works.
                if on_result is not None and not st.failed_error:
                    result = {
                        "usage": {
                            "input_tokens": st.tokens_in,
                            "output_tokens": st.tokens_out,
                        },
                        "last_agent_message": st.last_agent_message,
                    }
                    try:
                        on_result(result)
                    except Exception as e:         # noqa: BLE001
                        log_exception("codexOnResultFail", e, detail=trace_id)
            if not st.saw_session:
                log("codexNoSessionEvent",
                    f"trace={trace_id or '∅'} — turn produced no thread.started/"
                    f"session_meta; history binding may be missing")
        except Exception as e:                     # noqa: BLE001
            log_exception("codexDrainFail", e, detail=trace_id)
        finally:
            if agent_id:
                self.unregister_handle(agent_id, handle)

    def _handle_event(
        self, ev: dict, st: TurnState, *,
        agent_id: str, session: str, trace_id: str,
        on_session_init: Optional[Callable[[str], None]],
        on_error: Optional[Callable[[str], None]],
        stream: Any, enqueue: Callable[..., int],
    ) -> None:
        etype = str(ev.get("type") or "")

        # ===== Modern `codex exec --json` stdout schema =====================
        # Verified against codex-cli 0.135.0: events are thread.started /
        # turn.started / item.{started,updated,completed} / turn.completed.
        # (The on-disk rollout files still use the older session_meta/
        # event_msg/response_item shape — that's handled by codex_transcript,
        # and as a fallback below.)
        if etype == "thread.started":
            sid = str(ev.get("thread_id") or ev.get("threadId") or "").strip()
            if sid:
                st.saw_session = True
                st.live_backend_session_id = sid
                log("codexSessionInit", f"sid={sid} trace={trace_id or '∅'}")
                if on_session_init is not None:
                    accepted = on_session_init(sid)
                    if accepted is False:
                        st.failed_error = "backend session binding rejected"
                        if on_error is not None:
                            on_error(st.failed_error)
            return
        if etype == "turn.started":
            self.transition(agent_id, TurnEvent.SPAWN_STARTED,
                               {"dispatch": self.runner, "trace_id": trace_id})
            return
        if etype in ("item.started", "item.updated", "item.completed"):
            item = ev.get("item")
            if isinstance(item, dict):
                self.handle_item(etype, item, st, agent_id=agent_id,
                                  session=session, trace_id=trace_id,
                                  stream=stream, enqueue=enqueue)
            return
        if etype == "turn.completed":
            usage = ev.get("usage")
            if isinstance(usage, dict):
                st.tokens_in = int(usage.get("input_tokens") or st.tokens_in or 0)
                st.tokens_out = int(usage.get("output_tokens") or st.tokens_out or 0)
            self.update_live_text(
                st, agent_id=agent_id, session=session, trace_id=trace_id,
                stream=stream, force=True)
            self.broadcast_transcript(stream, agent_id, session)
            return
        if etype in ("turn.failed", "thread.error", "error"):
            err = ""
            e = ev.get("error")
            if isinstance(e, dict):
                err = str(e.get("message") or "")
            elif isinstance(e, str):
                err = e
            st.failed_error = err or "codex turn failed"
            if on_error is not None:
                on_error(st.failed_error)
            return

        # ===== Legacy rollout/event_msg schema (fallback) ===================
        inner, payload = _event_parts(ev)

        # --- session id (first turn binds the conversation UUID) ---
        if inner == "session_meta" or ev.get("type") == "session_meta":
            sid = _session_id_from(ev, payload)
            if sid:
                st.saw_session = True
                st.live_backend_session_id = sid
                log("codexSessionInit", f"sid={sid} trace={trace_id or '∅'}")
                if on_session_init is not None:
                    accepted = on_session_init(sid)
                    if accepted is False:
                        st.failed_error = "backend session binding rejected"
                        if on_error is not None:
                            on_error(st.failed_error)
            return

        # --- turn lifecycle → agent state ---
        if inner == "task_started":
            self.transition(agent_id, TurnEvent.SPAWN_STARTED,
                               {"dispatch": self.runner, "trace_id": trace_id})
            return

        if inner in ("function_call", "custom_tool_call", "web_search_call",
                     "mcp_tool_call_begin", "exec_command_begin"):
            name = str(payload.get("name") or payload.get("tool") or "tool")
            self.transition(agent_id, TurnEvent.TOOL_STARTED,
                               {"dispatch": self.runner, "tool": name, "trace_id": trace_id})
            self.broadcast_transcript(stream, agent_id, session)
            return

        if inner in ("function_call_output", "custom_tool_call_output",
                     "patch_apply_end", "exec_command_end", "web_search_end"):
            # A tool finished — refresh the history pane; state goes back to
            # THINKING until the next agent_message / tool / task_complete.
            self.broadcast_transcript(stream, agent_id, session)
            return

        if inner == "context_compacted":
            self.transition(agent_id, TurnEvent.COMPACTION_STARTED,
                               {"dispatch": self.runner, "trace_id": trace_id})
            return

        if inner == "turn_aborted":
            self.transition(agent_id, TurnEvent.TURN_FAILED_UNCLASSIFIED,
                               {"dispatch": self.runner, "aborted": True,
                                "trace_id": trace_id})
            return

        # --- token telemetry (for the per-turn banner) ---
        if inner == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                st.tokens_in = int(
                    info.get("input_tokens")
                    or info.get("total_input_tokens")
                    or st.tokens_in or 0
                )
                st.tokens_out = int(
                    info.get("output_tokens")
                    or info.get("total_output_tokens")
                    or st.tokens_out or 0
                )
            return

        # --- assistant text → TTS + history ---
        if inner in ("agent_message", "message"):
            # Only assistant/agent speech is spoken. `message` may also carry
            # user/developer roles in the stream — skip those for voice.
            role = payload.get("role")
            if inner == "message" and role not in (None, "assistant"):
                return
            text = _agent_text(payload).strip()
            if not text:
                return
            st.last_agent_message = text
            self.update_live_text(
                st, text=text, agent_id=agent_id, session=session,
                trace_id=trace_id, stream=stream)
            self._speak(text, st, agent_id=agent_id, session=session,
                        trace_id=trace_id, enqueue=enqueue)
            return

        if inner == "task_complete":
            msg = payload.get("last_agent_message")
            if isinstance(msg, str) and msg.strip():
                st.last_agent_message = msg.strip()
                st.pending_live_text = st.last_agent_message
                # The final message may contain <speak> blocks the streaming
                # agent_message events didn't (some models only emit the full
                # text at completion). Speak any we haven't already.
                self._speak(msg, st, agent_id=agent_id, session=session,
                            trace_id=trace_id, enqueue=enqueue)
            self.update_live_text(
                st, agent_id=agent_id, session=session, trace_id=trace_id,
                stream=stream, force=True)
            self.broadcast_transcript(stream, agent_id, session)
            return

    def handle_item(self, etype: str, item: dict, st: TurnState, *,
                     agent_id: str, session: str, trace_id: str,
                     stream: Any, enqueue: Callable[..., int]) -> None:
        """Handle a modern item.{started,updated,completed} event.

        `item.type == "agent_message"` carries the assistant text (spoken on
        completion); tool-ish item types flip the agent to TOOL; reasoning
        items are silent."""
        itype = str(item.get("type") or "")

        if itype == "agent_message":
            text = (item.get("text") or item.get("message") or "").strip()
            if not text:
                return
            st.last_agent_message = text
            self.update_live_text(
                st, text=text, agent_id=agent_id, session=session,
                trace_id=trace_id, stream=stream,
                force=etype == "item.completed")
            # Only speak the completed block — item.updated may carry partials.
            if etype == "item.completed":
                self._speak(text, st, agent_id=agent_id, session=session,
                            trace_id=trace_id, enqueue=enqueue)
            return

        if itype == "reasoning":
            # Model thinking — keep the agent in a busy state, but nothing to
            # speak or render.
            self.transition(agent_id, TurnEvent.TEXT_STREAMED,
                               {"dispatch": self.runner, "trace_id": trace_id})
            return

        if itype in _TOOL_ITEM_TYPES:
            name = str(item.get("command") or item.get("name") or itype)
            self.transition(agent_id, TurnEvent.TOOL_STARTED,
                               {"dispatch": self.runner, "tool": name[:80],
                                "trace_id": trace_id})
            self.broadcast_transcript(stream, agent_id, session)
            return

        # Unknown item kind — just refresh the history pane.
        self.broadcast_transcript(stream, agent_id, session)

    def update_live_text(
        self,
        st: TurnState,
        *,
        agent_id: str,
        session: str,
        trace_id: str,
        stream: Any,
        text: str = "",
        force: bool = False,
    ) -> None:
        """Write one mutable assistant row at a bounded visual cadence."""
        if text.strip():
            st.pending_live_text = text.strip()
        self.persist_live_text(
            st, text=st.pending_live_text,
            backend_session_id=st.live_backend_session_id,
            agent_id=agent_id, session=session, trace_id=trace_id, stream=stream,
            force=force,
            interval=self.live_text_interval)

    def _speak(self, text: str, st: TurnState, *, agent_id: str, session: str,
               trace_id: str, enqueue: Callable[..., int]) -> None:
        """Extract <speak>…</speak> regions and enqueue each as a TTS clip.

        De-duplicates against blocks already spoken this turn (the same region
        can appear in a streamed agent_message and again in task_complete's
        last_agent_message)."""
        if self.speak(text, st, agent_id=agent_id, session=session,
                      trace_id=trace_id, enqueue=enqueue):
            st.spoke_any = True

    # ---- orchestrator routing ----------------------------------------------

    def routing_cmd(self, prompt: str, *, model: str = "", effort: str = "") -> list[str]:
        """argv for one ephemeral ``codex exec --json`` request (orchestrator)."""
        cmd = self.build_cmd(
            model=model, reasoning_effort=effort, isolated=True)
        cmd.append(prompt)
        return cmd

    def routing_text(self, stdout: str) -> str:
        """The final agent message of a ``codex exec --json`` run."""
        final_text = ""
        for line in (stdout or "").splitlines():
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                candidate = item.get("text")
                if isinstance(candidate, str) and candidate.strip():
                    final_text = candidate
        if not final_text:
            raise ValueError("codex orchestrator returned no agent message")
        return final_text

    # --- interactive terminal ---------------------------------------------

    def terminal_argv(self, session_id: str) -> list[str]:
        argv = [self.required_binary]
        if session_id:
            argv += ["resume", session_id]
        return argv

    # --- model policy -----------------------------------------------------

    def _home(self) -> pathlib.Path:
        return pathlib.Path(os.environ.get("CODEX_HOME") or pathlib.Path.home() / ".codex")

    def model_transcript(self, session_id: str) -> pathlib.Path | None:
        """The rollout file: the thread index first, else the newest match."""
        try:
            with sqlite3.connect(f"file:{self._home() / 'state_5.sqlite'}?mode=ro",
                                 uri=True, timeout=0.1) as db:
                row = db.execute("SELECT rollout_path FROM threads WHERE id = ?",
                                 (session_id,)).fetchone()
            if row and row[0] and pathlib.Path(row[0]).is_file():
                return pathlib.Path(row[0])
        except sqlite3.Error:
            pass
        return self.find_transcript(session_id)

    def recorded_model(self, session_id: str) -> str:
        """The thread index's model, else the rollout's last turn_context."""
        if not session_id:
            return ""
        indexed = resolve("session_models", "indexed_model")(self._home(), session_id)
        if indexed:
            return indexed
        return resolve("session_models", "transcript_model")(self.id, session_id)

    def cli_default_model(self) -> str:
        """The active profile's ``model`` in config.toml, else the top-level one."""
        data = tomllib.loads((self._home() / "config.toml").read_text())
        profile = data.get("profiles", {}).get(data.get("profile", ""), {})
        return str(profile.get("model") or data.get("model") or "")

    # --- compaction -------------------------------------------------------

    def compaction(self, session: str) -> CompactionStrategy:
        """``/compact`` typed into ``codex resume <id>``; no transcript handle
        to watch, so the Host waits a fixed window."""
        return CompactionStrategy(launch=(self.required_binary, "resume"), command="/compact")

    # --- credentials and quota --------------------------------------------

    def on_credential_change(self) -> None:
        """Drop leftover Codex app-servers after credentials change.

        ``codex login`` rewrites ``~/.codex/auth.json`` in another process.
        The per-agent app-server still holds thread writer locks and the
        previous token. The next ``thread/resume`` then fails with
        ``already has an active writer``. Closing stdin lets flock drop; the
        next turn starts a fresh app-server that re-reads auth.
        """
        from ..log import log_exception
        try:
            resolve("codex_app_server", "recycle_clients")()
        except Exception as exc:  # noqa: BLE001
            log_exception("codexAppServerRecycleFail", exc)

    def recover_usage_limit(self, message: str) -> bool:
        """Ask the app-server whether the limit was a stale connection it can
        reconnect past; True means the turn may be retried as is."""
        return bool(resolve("codex_app_server", "recover_usage_failure")(message))

    def account_pool(self) -> str:
        """The pool is named after the CLI; turn_dispatch keys its failover
        coordinator by it and reads ``config_account_switch_field``."""
        return self.id

    def classify_usage_limit(self, message: str, *,
                             quota_confirmed: bool | None = None) -> dict | None:
        if quota_confirmed is False:
            return None
        return resolve("backend_usage", "record_classified_usage_limit")(self.id)
