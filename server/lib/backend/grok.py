"""Grok Build (``grok -p``): a plain JSON-lines CLI with no interactive terminal yet."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .. import agents as agents_db
from .. import tts_queue
from ..log import log, log_exception
from ..proc_util import stderr_text
from ..process_registry import TurnHandle
from ..protocol import AgentState
from ..voice_preamble import apply_voice_preamble
from .base import hooked
from .stream_json import StreamJsonBackend, iter_json_dicts


@dataclass
class _TurnState:
    """Accumulators for one turn's drainer thread.

    ``live_text`` is the assistant text of the current model call (Grok
    writes one ``assistant`` row per model call to chat_history.jsonl, so a
    tool call ends the segment); ``turn_text`` is every segment of the turn.
    """
    session_id: str = ""
    last_agent_message: str = ""
    live_text: str = ""
    turn_text: str = ""
    failed_error: str = ""
    saw_session: bool = False
    phase: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    seen_speak: set[str] = field(default_factory=set)
    persisted_live_text: str = ""
    last_live_write_at: float = 0.0


def _text_from(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        content = value.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(filter(None, (_text_from(part) for part in content)))
    if isinstance(value, list):
        return "\n".join(filter(None, (_text_from(part) for part in value)))
    return ""


def _session_id_from(ev: dict) -> str:
    params = ev.get("params") if isinstance(ev.get("params"), dict) else {}
    update = params.get("update") if isinstance(params.get("update"), dict) else {}
    for src in (ev, params, update, ev.get("result") if isinstance(ev.get("result"), dict) else {}):
        if not isinstance(src, dict):
            continue
        for key in ("session_id", "sessionId", "id"):
            value = src.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _assistant_delta(ev: dict) -> str:
    if ev.get("method") == "session/update":
        params = ev.get("params") if isinstance(ev.get("params"), dict) else {}
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = str(update.get("sessionUpdate") or "")
        if kind in {"agent_message_chunk", "agent_message"}:
            return _text_from(update.get("content"))
        return ""
    etype = str(ev.get("type") or ev.get("event") or "")
    if etype == "text" and isinstance(ev.get("data"), str):
        # grok >= 1.0.30 ``--output-format streaming-json``: one ACP session
        # update per line; assistant text arrives as ``text`` deltas in
        # ``data`` (``thought`` carries reasoning the same way).
        return ev["data"]
    if etype in {"assistant", "agent_message", "message", "text", "output_text"}:
        role = str(ev.get("role") or "")
        if role and role not in {"assistant", "model"}:
            return ""
        return (_text_from(ev.get("content"))
                or _text_from(ev.get("text"))
                or _text_from(ev.get("message"))
                or _text_from(ev.get("delta")))
    return ""


# ACP tool-call lifecycle events (grok >= 1.0.30) plus the older spellings.
_TOOL_START_TYPES = {"tool_call", "tool_started", "tool_start", "tool_use",
                     "item.started"}
_TOOL_UPDATE_TYPES = {"tool_call_update"}
_TOOL_DONE_STATUSES = {"completed", "failed", "error", "cancelled"}


def _tool_name_from(ev: dict) -> str:
    return str(ev.get("toolName") or ev.get("title") or ev.get("name")
               or ev.get("tool") or "tool")[:80]


def _usage_from(ev: dict) -> tuple[int, int]:
    usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
    try:
        return (int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0))
    except (TypeError, ValueError):
        return (0, 0)


class GrokBackend(StreamJsonBackend):
    """Runs ``grok -p --output-format streaming-json`` once per turn."""

    def build_cmd(self, session_id: str = "", *, is_new_session: bool = False,
                  model: str = "", effort: str = "") -> list[str]:
        """argv for one headless Grok Build turn (prompt appended as ``-p``)."""
        cmd = [
            self._hook("GROK_BIN", self.required_binary),
            "--always-approve", "--no-alt-screen",
            "--output-format", "streaming-json",
        ]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--reasoning-effort", effort]
        if session_id and not is_new_session:
            cmd += ["--resume", session_id]
        elif session_id:
            cmd += ["--session-id", session_id]
        return cmd

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
        **_kwargs: Any,
    ) -> TurnHandle:
        grok_bin = self._hook("GROK_BIN", self.required_binary)
        if shutil.which(grok_bin) is None:
            raise FileNotFoundError(
                f"`{grok_bin}` not on PATH — install Grok Build "
                f"(https://x.ai/cli) to run Grok-backed agents")
        cwd = pathlib.Path(os.path.expanduser(str(cwd)))
        minted = ""
        if not backend_session_id or is_new_session:
            minted = str(uuid.uuid4())
            backend_session_id = minted
            is_new_session = True
        agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
        persona = (agent or {}).get("persona") or ""
        prompt = apply_voice_preamble(
            text, voice=voice_preamble, persona=persona, session=session)
        cmd = self._hook("build_cmd", self.build_cmd)(
            backend_session_id, is_new_session=is_new_session,
            model=model, effort=effort)
        cmd += ["-p", prompt]
        flag = "resume" if not is_new_session else "new"
        log("grokSpawn", f"cwd={cwd} {flag}={backend_session_id or '∅'} "
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
            minted=bool(minted),
            on_session_init=on_session_init, on_result=on_result,
            on_error=on_error, stream=stream,
            enqueue=enqueue or tts_queue.enqueue,
        )
        return handle

    def _drain_stdout(
        self, *, proc: subprocess.Popen, agent_id: str, session: str, trace_id: str,
        handle: TurnHandle, backend_session_id: str, minted: bool,
        on_session_init, on_result, on_error, stream, enqueue,
    ) -> None:
        st = _TurnState(session_id=backend_session_id)
        try:
            if minted:
                self._bind(st, backend_session_id, on_session_init=on_session_init,
                           on_error=on_error, trace_id=trace_id)
                self._record_state(agent_id, AgentState.THINKING,
                                   {"dispatch": self.runner, "trace_id": trace_id})
            if proc.stdout is not None:
                for raw in proc.stdout:
                    line = raw.strip()
                    if not line or st.failed_error:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    sid = _session_id_from(ev)
                    if sid:
                        self._bind(st, sid, on_session_init=on_session_init,
                                   on_error=on_error, trace_id=trace_id)
                    self._handle_event(
                        ev, st, agent_id=agent_id, session=session,
                        trace_id=trace_id, on_error=on_error, stream=stream,
                        enqueue=enqueue)
            rc = proc.wait()
            if rc != 0 and not st.failed_error:
                err = stderr_text(proc) or f"grok exited rc={rc}"
                log("grokExitErr", f"rc={rc} trace={trace_id or '∅'} "
                                   f"stderr={(err or '')[:500]!r}")
                if on_error is not None:
                    on_error(err)
            elif on_result is not None and not st.failed_error:
                if not st.saw_session and backend_session_id:
                    self._bind(st, backend_session_id, on_session_init=on_session_init,
                               on_error=on_error, trace_id=trace_id)
                self._persist_live_text(st, agent_id=agent_id, session=session,
                                        trace_id=trace_id, stream=stream, force=True)
                self._broadcast(stream, agent_id, session)
                usage = ({"input_tokens": st.tokens_in, "output_tokens": st.tokens_out}
                         if (st.tokens_in or st.tokens_out) else {})
                on_result({"last_agent_message": st.last_agent_message or st.turn_text,
                           "usage": usage})
        except Exception as error:  # noqa: BLE001
            log_exception("grokDrainFail", error, detail=trace_id)
            if on_error is not None and not st.failed_error:
                try:
                    on_error(str(error)[:300])
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if agent_id:
                self.unregister_handle(agent_id, handle)

    def _handle_event(
        self, ev: dict, st: _TurnState, *, agent_id: str, session: str, trace_id: str,
        on_error, stream, enqueue,
    ) -> None:
        etype = str(ev.get("type") or ev.get("event") or "")
        if etype in {"available_commands", "usage_update", "plan"}:
            if etype == "usage_update":
                st.tokens_in, st.tokens_out = _usage_from(ev)
            return
        if etype == "usage":
            st.tokens_in, st.tokens_out = _usage_from(ev)
            return
        if etype == "thought":
            # Reasoning delta: nothing to show, but the agent is visibly busy.
            if st.phase != "thinking":
                st.phase = "thinking"
                self._record_state(agent_id, AgentState.THINKING,
                                   {"dispatch": self.runner, "trace_id": trace_id})
            return
        if etype in _TOOL_START_TYPES:
            # Grok has written the assistant row that carries this call to
            # chat_history.jsonl by now, so the live segment is about to be
            # covered by a durable row: flush it, refresh the pane, and start a
            # fresh segment for the text that follows the tool result.
            self._persist_live_text(st, agent_id=agent_id, session=session,
                                    trace_id=trace_id, stream=stream, force=True)
            st.live_text = ""
            st.persisted_live_text = ""
            st.phase = "tool"
            self._record_state(agent_id, AgentState.TOOL, {
                "dispatch": self.runner, "trace_id": trace_id,
                "tool": _tool_name_from(ev),
            })
            self._broadcast(stream, agent_id, session)
            return
        if etype in _TOOL_UPDATE_TYPES:
            status = str(ev.get("status") or "")
            if status in _TOOL_DONE_STATUSES:
                # The tool_result row has landed; refresh the pane so the card
                # picks up its output and status.
                st.phase = "thinking"
                self._record_state(agent_id, AgentState.THINKING,
                                   {"dispatch": self.runner, "trace_id": trace_id})
                self._broadcast(stream, agent_id, session)
            return
        if etype in {"turn_started", "turn.started"}:
            st.phase = "thinking"
            self._record_state(agent_id, AgentState.THINKING,
                               {"dispatch": self.runner, "trace_id": trace_id})
            return
        if etype in {"error", "turn.failed", "turn_failed"}:
            err = ev.get("error") or ev.get("message") or "grok error"
            if isinstance(err, dict):
                err = str(err.get("message") or err)
            st.failed_error = str(err)
            if on_error is not None:
                on_error(st.failed_error)
            return
        if etype == "end":
            usage_in, usage_out = _usage_from(ev)
            if usage_in or usage_out:
                st.tokens_in, st.tokens_out = usage_in, usage_out
            stop = str(ev.get("stopReason") or "")
            if stop and stop not in {"end_turn", "endTurn", "stop"}:
                log("grokStop", f"reason={stop} trace={trace_id or '∅'}")
            self._persist_live_text(st, agent_id=agent_id, session=session,
                                    trace_id=trace_id, stream=stream, force=True)
            self._broadcast(stream, agent_id, session)
            return
        delta = _assistant_delta(ev)
        if not delta:
            return
        if st.phase != "speaking":
            st.phase = "speaking"
            self._record_state(agent_id, AgentState.THINKING,
                               {"dispatch": self.runner, "trace_id": trace_id})
        st.live_text += delta
        st.turn_text += delta
        st.last_agent_message = st.live_text
        self._speak(st.live_text, st, agent_id=agent_id, session=session,
                    trace_id=trace_id, enqueue=enqueue)
        self._persist_live_text(st, agent_id=agent_id, session=session,
                                trace_id=trace_id, stream=stream)

    def _persist_live_text(self, st: _TurnState, *, agent_id: str, session: str,
                           trace_id: str, stream: Any, force: bool = False) -> None:
        """Write one mutable assistant row at a bounded visual cadence.

        Same contract as the Codex and AGY runners: the row is keyed by the
        turn's trace, so a later transcript import replaces it with the durable
        assistant text once chat_history.jsonl catches up.
        """
        self.persist_live_text(
            st, text=st.live_text, backend_session_id=st.session_id,
            agent_id=agent_id, session=session, trace_id=trace_id, stream=stream,
            force=force,
            interval=self._hook("LIVE_TEXT_INTERVAL_SEC", self.live_text_interval))

    def _bind(self, st: _TurnState, session_id: str, *, on_session_init, on_error,
              trace_id: str) -> None:
        self.bind_session(st, session_id, on_session_init=on_session_init,
                          on_error=on_error, trace_id=trace_id)

    def _speak(self, text: str, st: _TurnState, *, agent_id: str, session: str,
               trace_id: str, enqueue) -> None:
        """Enqueue each completed <speak>…</speak> block of ``text`` once.

        ``text`` is the accumulated segment, so a block only matches once its
        closing tag has streamed in; ``seen_speak`` keeps a later pass over the
        same segment from re-speaking it. Untagged prose stays silent, as with
        the Codex and Claude runners.
        """
        self.speak(text, st, agent_id=agent_id, session=session, trace_id=trace_id,
                   enqueue=enqueue, fail_event="grokSpeakFail",
                   fail_detail=trace_id)

    def _record_state(self, agent_id: str, kind: str, detail: dict[str, Any]) -> None:
        self.record_state(agent_id, kind, detail, event="grokStateFail")

    def _broadcast(self, stream: Any, agent_id: str, session: str) -> None:
        self.broadcast_transcript(stream, agent_id, session)

    # ---- orchestrator routing ----------------------------------------------

    @hooked
    def routing_cmd(self, prompt: str, *, model: str = "", effort: str = "") -> list[str]:
        """argv for one headless Grok Build request with no session (orchestrator)."""
        return self._hook("build_cmd", self.build_cmd)(model=model, effort=effort) + ["-p", prompt]

    @hooked
    def routing_text(self, stdout: str) -> str:
        """Concatenated assistant text of a streaming-json run."""
        text = ""
        for ev in iter_json_dicts(stdout):
            text += _assistant_delta(ev)
        return text
