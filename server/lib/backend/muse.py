"""Muse Code (``muse exec --json``): Meta's coding CLI, one headless run per turn.

docs/architecture/muse-backend.md is the contract: the wire it reads, how a
session id is minted and continued, and where the transcript lives.
"""
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
from .. import muse_transcript
from .. import tts_queue
from ..log import log, log_exception
from ..proc_util import stderr_text
from ..process_registry import TurnHandle
from ..turn_lifecycle import TurnEvent
from ..voice_preamble import apply_voice_preamble
from .base import BackendBrand
from .stream_json import StreamJsonBackend, iter_json_dicts

# Terminal states that end a run without an answer. A stop (Clarp's own, or
# the user's) is not a failure: like Codex and Claude it leaves no error row.
_FAILED = {"failed", "aborted"}
_STOPPED = {"cancelled", "interrupted"}


@dataclass
class _TurnState:
    """Accumulators for one turn's drainer thread.

    ``live_text`` is the assistant text since the last tool result (Muse
    commits one assistant message per model call, so a tool call ends the
    segment); ``turn_text`` is every segment of the turn.
    """
    session_id: str = ""
    last_agent_message: str = ""
    live_text: str = ""
    turn_text: str = ""
    failed_error: str = ""
    stopped: bool = False
    saw_session: bool = False
    phase: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    allowance: dict = field(default_factory=dict)
    seen_speak: set[str] = field(default_factory=set)
    persisted_live_text: str = ""
    last_live_write_at: float = 0.0


def _payload(ev: dict) -> dict:
    payload = ev.get("payload")
    return payload if isinstance(payload, dict) else {}


def _usage_from(payload: dict) -> tuple[int, int]:
    tokens_in = tokens_out = 0
    for row in payload.get("models") or []:
        if not isinstance(row, dict):
            continue
        try:
            tokens_in += int(row.get("input_tokens") or 0)
            tokens_out += int(row.get("output_tokens") or 0)
        except (TypeError, ValueError):
            continue
    return tokens_in, tokens_out


def _tool_name(payload: dict) -> str:
    facts = payload.get("correlation_facts")
    facts = facts if isinstance(facts, dict) else {}
    return str(facts.get("tool_name") or payload.get("tool_name") or "tool")[:80]


class MuseBackend(StreamJsonBackend):
    """Runs ``muse exec --json --session-id <id>`` once per turn."""
    # --- catalogue data ---------------------------------------------------
    id = 'muse'
    label = 'Muse Code'
    required_binary = 'muse'
    aliases = ('muse-code', 'musecode')
    badge = 'BackendMuse'
    detail = 'Runs on Meta Muse Code.'
    symbol = 'sparkles'
    brand = BackendBrand('#1c2b4a', '#0d1526', '#8fb4ff', '#2b4a8a')
    efforts = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
    runner = 'muse'
    config_model_field = 'muse_model'
    config_effort_field = 'muse_effort'
    model_family = 'muse'
    fallback_models = (
        ('muse-wonder-1.0', 'Muse Wonder 1.0'),
    )

    transcript = muse_transcript

    # --- argv ---------------------------------------------------------------

    def build_cmd(self, session_id: str = "", *, model: str = "", effort: str = "") -> list[str]:
        """argv for one headless turn; the prompt follows as ``-- <prompt>``.

        ``--session-id`` both creates a session and continues it, so a new
        agent and a resumed one differ only in whether the id was just
        minted. ``--yolo`` turns approval and the sandbox off for the run,
        the posture every Clarp backend runs with.
        """
        cmd = [self.required_binary, "exec", "--json", "--yolo"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--reasoning-effort", effort]
        if session_id:
            cmd += ["--session-id", session_id]
        return cmd

    # --- the turn -------------------------------------------------------------

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
        muse_bin = self.required_binary
        if shutil.which(muse_bin) is None:
            raise FileNotFoundError(
                f"`{muse_bin}` not on PATH — install Muse Code "
                f"(https://api.meta.ai/muse-launcher.sh) to run Muse-backed agents")
        cwd = pathlib.Path(os.path.expanduser(str(cwd)))
        minted = ""
        if not backend_session_id or is_new_session:
            minted = str(uuid.uuid4())
            backend_session_id = minted
        agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
        persona = (agent or {}).get("persona") or ""
        prompt = apply_voice_preamble(text, voice=voice_preamble, persona=persona, session=session)
        cmd = self.build_cmd(backend_session_id, model=model, effort=effort) + ["--", prompt]
        flag = "new" if minted else "resume"
        log("museSpawn", f"cwd={cwd} {flag}={backend_session_id} text_len={len(text)} "
                         f"trace={trace_id or '∅'} agent={agent_id or '∅'}")
        runtime_agent_id = "" if isolated else agent_id
        from .. import provider_background_jobs as provider_jobs
        turn_token, identity = provider_jobs.identity_env(runtime_agent_id)
        proc, handle = self.launch(cmd, cwd=cwd, session=session, env_extra=identity)
        provider_jobs.record_identity(turn_token, agent_id=runtime_agent_id,
                                      provider=self.id, pid=proc.pid)
        self.register_handle(runtime_agent_id, handle)
        self.start_drain(
            handle, self._drain_stdout,
            proc=proc, agent_id=runtime_agent_id, session=session,
            trace_id=trace_id, handle=handle,
            backend_session_id=backend_session_id, minted=bool(minted),
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
            # The id is Clarp's own, so the agent is bound to it before Muse
            # writes a line: a crash mid-run still resumes the same session.
            self.bind_session(st, backend_session_id, on_session_init=on_session_init,
                              on_error=on_error, trace_id=trace_id)
            if minted:
                self._transition(agent_id, TurnEvent.SPAWN_STARTED,
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
                    if isinstance(ev, dict):
                        self._handle_event(ev, st, agent_id=agent_id, session=session,
                                           trace_id=trace_id, on_error=on_error,
                                           stream=stream, enqueue=enqueue)
            rc = proc.wait()
            if rc != 0 and not st.failed_error and not st.stopped:
                err = stderr_text(proc) or f"muse exited rc={rc}"
                log("museExitErr", f"rc={rc} trace={trace_id or '∅'} stderr={(err or '')[:500]!r}")
                if on_error is not None:
                    on_error(err)
            elif on_result is not None and not st.failed_error:
                self._persist_live_text(st, agent_id=agent_id, session=session,
                                        trace_id=trace_id, stream=stream, force=True)
                self._broadcast(stream, agent_id, session)
                usage = ({"input_tokens": st.tokens_in, "output_tokens": st.tokens_out}
                         if (st.tokens_in or st.tokens_out) else {})
                on_result({"last_agent_message": st.last_agent_message or st.turn_text,
                           "usage": usage})
        except Exception as error:  # noqa: BLE001
            log_exception("museDrainFail", error, detail=trace_id)
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
        kind = str(ev.get("payload_type") or "")
        payload = _payload(ev)
        if kind == "run.model.configured":
            st.model = str(payload.get("model_id") or "")
            return
        if kind == "run.lifecycle.started":
            st.phase = "thinking"
            self._transition(agent_id, TurnEvent.TEXT_STREAMED,
                             {"dispatch": self.runner, "trace_id": trace_id, "phase": "thinking"})
            return
        if kind == "run.usage":
            tokens_in, tokens_out = _usage_from(payload)
            if tokens_in or tokens_out:
                st.tokens_in, st.tokens_out = tokens_in, tokens_out
            allowance = payload.get("allowance")
            if isinstance(allowance, dict):
                st.allowance = allowance
            return
        if kind == "tool.result":
            # The assistant message that called this tool is committed to
            # session.jsonl by now: flush the live segment so the durable row
            # takes over, show the tool, and refresh the history pane.
            self._persist_live_text(st, agent_id=agent_id, session=session,
                                    trace_id=trace_id, stream=stream, force=True)
            st.live_text = ""
            st.persisted_live_text = ""
            st.phase = "tool"
            self._transition(agent_id, TurnEvent.TOOL_STARTED,
                             {"dispatch": self.runner, "trace_id": trace_id,
                              "tool": _tool_name(payload)})
            self._broadcast(stream, agent_id, session)
            return
        if kind.startswith("run.terminal."):
            terminal = str(payload.get("terminal") or kind.rsplit(".", 1)[-1])
            if terminal in _STOPPED:
                st.stopped = True
            elif terminal in _FAILED:
                reason = str(payload.get("reason") or "").strip()
                st.failed_error = (f"Muse Code {terminal}: {reason}" if reason
                                   else f"Muse Code {terminal}")
                if on_error is not None:
                    on_error(st.failed_error)
                return
            self._persist_live_text(st, agent_id=agent_id, session=session,
                                    trace_id=trace_id, stream=stream, force=True)
            self._broadcast(stream, agent_id, session)
            return
        if kind != "run.output.delta":
            return
        delta = str(payload.get("text") or "")
        if not delta:
            return
        if st.phase != "speaking":
            st.phase = "speaking"
            self._transition(agent_id, TurnEvent.TEXT_STREAMED,
                             {"dispatch": self.runner, "trace_id": trace_id, "phase": "responding"})
        st.live_text += delta
        st.turn_text += delta
        st.last_agent_message = st.live_text
        self.speak(st.live_text, st, agent_id=agent_id, session=session, trace_id=trace_id,
                   enqueue=enqueue, fail_event="museSpeakFail", fail_detail=trace_id)
        self._persist_live_text(st, agent_id=agent_id, session=session,
                                trace_id=trace_id, stream=stream)

    def _persist_live_text(self, st: _TurnState, *, agent_id: str, session: str,
                           trace_id: str, stream: Any, force: bool = False) -> None:
        """One mutable assistant row at a bounded cadence, keyed by the turn's
        trace; the transcript import replaces it once session.jsonl has the
        committed message (same contract as the Codex, AGY and Grok runners)."""
        self.persist_live_text(st, text=st.live_text, backend_session_id=st.session_id,
                               agent_id=agent_id, session=session, trace_id=trace_id,
                               stream=stream, force=force, interval=self.live_text_interval)

    def _transition(self, agent_id: str, turn_event: str, detail: dict[str, Any]) -> None:
        self.transition(agent_id, turn_event, detail, log_event="museStateFail")

    def _broadcast(self, stream: Any, agent_id: str, session: str) -> None:
        self.broadcast_transcript(stream, agent_id, session)

    # --- interactive terminal ----------------------------------------------

    def terminal_argv(self, session_id: str) -> list[str]:
        """``muse resume <id>``, or a fresh TUI when there is no session yet."""
        argv = [self.required_binary]
        if session_id:
            argv += ["resume", session_id]
        return argv

    # ---- orchestrator routing ----------------------------------------------

    def routing_cmd(self, prompt: str, *, model: str = "", effort: str = "") -> list[str]:
        """argv for one headless request with no stored session (orchestrator)."""
        return self.build_cmd(model=model, effort=effort) + ["--no-session-log", "--", prompt]

    def routing_text(self, stdout: str) -> str:
        """The run's answer: its terminal text, else the concatenated deltas."""
        deltas = ""
        for ev in iter_json_dicts(stdout):
            kind = str(ev.get("payload_type") or "")
            payload = _payload(ev)
            if kind == "run.terminal.completed" and payload.get("text"):
                return str(payload["text"])
            if kind == "run.output.delta":
                deltas += str(payload.get("text") or "")
        return deltas

    # No ``compaction``: Muse compacts its own context (``--context-compaction-*``)
    # and a typed ``/compact`` in ``muse resume`` is not verified, so the
    # catalogue says compaction is unsupported.
