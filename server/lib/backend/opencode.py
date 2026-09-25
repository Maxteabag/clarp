"""OpenCode (``opencode run --format json``): a JSON-lines CLI fronting several model providers."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
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
    session_id: str = ""
    last_agent_message: str = ""
    live_text: str = ""
    failed_error: str = ""
    saw_session: bool = False
    seen_speak: set[str] = field(default_factory=set)


def _text_from(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "delta", "message"):
            if isinstance(value.get(key), str):
                return value[key]
        part = value.get("part")
        if isinstance(part, dict):
            return _text_from(part)
        content = value.get("content")
        if isinstance(content, list):
            return "\n".join(filter(None, (_text_from(item) for item in content)))
    if isinstance(value, list):
        return "\n".join(filter(None, (_text_from(item) for item in value)))
    return ""


def _session_id_from(ev: dict) -> str:
    for key in ("sessionID", "session_id", "sessionId"):
        value = ev.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for nested_key in ("properties", "session", "data", "part"):
        nested = ev.get(nested_key)
        if isinstance(nested, dict):
            found = _session_id_from(nested)
            if found:
                return found
    return ""


class OpenCodeBackend(StreamJsonBackend):
    """Runs ``opencode run --format json`` once per turn; no compaction or terminal yet."""

    def build_cmd(self, session_id: str = "", *, is_new_session: bool = False,
                  model: str = "", effort: str = "") -> list[str]:
        cmd = [self._hook("OPENCODE_BIN", self.required_binary),
               "run", "--format", "json", "--auto"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--variant", effort]
        if session_id and not is_new_session:
            cmd += ["--session", session_id]
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
        opencode_bin = self._hook("OPENCODE_BIN", self.required_binary)
        if shutil.which(opencode_bin) is None:
            raise FileNotFoundError(
                f"`{opencode_bin}` not on PATH — install OpenCode "
                f"(https://opencode.ai) to run OpenCode-backed agents")
        cwd = pathlib.Path(os.path.expanduser(str(cwd)))
        agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
        persona = (agent or {}).get("persona") or ""
        prompt = apply_voice_preamble(
            text, voice=voice_preamble, persona=persona, session=session)
        cmd = self._hook("build_cmd", self.build_cmd)(
            backend_session_id, is_new_session=is_new_session,
            model=model, effort=effort)
        cmd.append(prompt)
        flag = "resume" if (backend_session_id and not is_new_session) else "new"
        log("opencodeSpawn", f"cwd={cwd} {flag}={backend_session_id or '∅'} "
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

    def _drain_stdout(
        self, *, proc: subprocess.Popen, agent_id: str, session: str, trace_id: str,
        handle: TurnHandle, backend_session_id: str,
        on_session_init, on_result, on_error, stream, enqueue,
    ) -> None:
        # Slice-3 seam: tests monkeypatch ``opencode_runner._record_state``.
        _record_state = self._hook("_record_state", self._record_state)
        st = _TurnState(session_id=backend_session_id)
        if backend_session_id:
            self._bind(st, backend_session_id, on_session_init=on_session_init,
                       on_error=on_error, trace_id=trace_id)
            _record_state(agent_id, AgentState.THINKING,
                          {"dispatch": self.runner, "trace_id": trace_id})
        try:
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
                    etype = str(ev.get("type") or ev.get("event") or "")
                    if etype == "step_start":
                        _record_state(agent_id, AgentState.THINKING, {
                            "dispatch": self.runner, "trace_id": trace_id,
                        })
                        self._broadcast(stream, agent_id, session)
                        continue
                    if etype in {"tool_use", "tool_start", "tool.running"}:
                        name = ""
                        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
                        name = str(part.get("tool") or ev.get("tool")
                                   or ev.get("name") or "tool")
                        _record_state(agent_id, AgentState.TOOL, {
                            "dispatch": self.runner, "trace_id": trace_id, "tool": name,
                        })
                        self._broadcast(stream, agent_id, session)
                        continue
                    if etype in {"error", "session.error"}:
                        err = str(ev.get("error") or ev.get("message") or "opencode error")
                        if isinstance(ev.get("error"), dict):
                            err = str(ev["error"].get("message") or err)
                        st.failed_error = err
                        if on_error is not None:
                            on_error(err)
                        continue
                    if etype in {"text", "message", "assistant", "output"}:
                        role = str(ev.get("role") or "")
                        if role in {"user", "system"}:
                            continue
                        delta = (_text_from(ev.get("part"))
                                 or _text_from(ev.get("text"))
                                 or _text_from(ev.get("content"))
                                 or _text_from(ev.get("delta"))
                                 or _text_from(ev))
                        if delta:
                            st.live_text += delta
                            st.last_agent_message = st.live_text
                            self._speak(delta, st, agent_id=agent_id, session=session,
                                        trace_id=trace_id, enqueue=enqueue)
                            self._broadcast(stream, agent_id, session)
            rc = proc.wait()
            if rc != 0 and not st.failed_error:
                err = stderr_text(proc) or f"opencode exited rc={rc}"
                log("opencodeExitErr", f"rc={rc} trace={trace_id or '∅'} "
                                       f"stderr={(err or '')[:500]!r}")
                if on_error is not None:
                    on_error(err)
            elif on_result is not None and not st.failed_error:
                on_result({"last_agent_message": st.last_agent_message, "usage": {}})
        except Exception as error:  # noqa: BLE001
            log_exception("opencodeDrainFail", error, detail=trace_id)
            if on_error is not None and not st.failed_error:
                try:
                    on_error(str(error)[:300])
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if agent_id:
                self.unregister_handle(agent_id, handle)

    def _bind(self, st: _TurnState, session_id: str, *, on_session_init, on_error,
              trace_id: str) -> None:
        self.bind_session(st, session_id,
                          on_session_init=on_session_init, on_error=on_error,
                          trace_id=trace_id)

    def _speak(self, text: str, st: _TurnState, *, agent_id: str, session: str,
               trace_id: str, enqueue) -> None:
        """Enqueue each ``<speak>`` block of ``text`` as a TTS clip.

        Mirrors the Codex runner: only voice turns speak, only explicitly marked
        regions are spoken, and a block is spoken once per turn even when the
        same text arrives in more than one event.
        """
        self.speak(text, st, agent_id=agent_id, session=session, trace_id=trace_id,
                   enqueue=enqueue, fail_event="opencodeSpeakFail",
                   fail_detail=trace_id)

    def _record_state(self, agent_id: str, kind: str, detail: dict[str, Any]) -> None:
        self.record_state(agent_id, kind, detail, event="opencodeStateFail")

    def _broadcast(self, stream: Any, agent_id: str, session: str) -> None:
        self.broadcast_transcript(stream, agent_id, session)

    # ---- orchestrator routing ----------------------------------------------

    @hooked
    def routing_cmd(self, prompt: str, *, model: str = "", effort: str = "") -> list[str]:
        """argv for one ``opencode run`` request with no session (orchestrator)."""
        return self._hook("build_cmd", self.build_cmd)(model=model, effort=effort) + [prompt]

    @hooked
    def routing_text(self, stdout: str) -> str:
        """Concatenated assistant text of an ``opencode run --format json`` run."""
        text = ""
        for ev in iter_json_dicts(stdout):
            etype = str(ev.get("type") or ev.get("event") or "")
            if etype not in {"text", "message", "assistant", "output"}:
                continue
            if str(ev.get("role") or "") in {"user", "system"}:
                continue
            text += (_text_from(ev.get("part"))
                     or _text_from(ev.get("text"))
                     or _text_from(ev.get("content"))
                     or _text_from(ev.get("delta"))
                     or _text_from(ev))
        return text
