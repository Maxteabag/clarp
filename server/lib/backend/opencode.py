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
from .. import opencode_transcript
from .. import tool_explanation_commands
from .. import tts_queue
from ..log import log, log_exception
from ..proc_util import stderr_text
from ..text_util import truncate
from ..process_registry import TurnHandle
from ..turn_lifecycle import TurnEvent
from ..voice_preamble import apply_voice_preamble
from .base import BackendBrand
from .stream_json import StreamJsonBackend, iter_json_dicts


@dataclass
class _TurnState:
    session_id: str = ""
    last_agent_message: str = ""
    live_text: str = ""
    failed_error: str = ""
    saw_session: bool = False
    seen_speak: set[str] = field(default_factory=set)
    # Text parts of the current step. OpenCode writes one step per model
    # call; the reply is the last step that said anything, not every
    # commentary line of the turn run together.
    step_texts: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    cost: float | None = None


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


def _error_message(error: Any) -> str:
    """OpenCode errors are ``{name, data: {message}}``; show the message."""
    if not isinstance(error, dict):
        return str(error or "")
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    message = str(data.get("message") or error.get("message") or "").strip()
    name = str(error.get("name") or "").strip()
    if message:
        return message
    return f"OpenCode {name}" if name else json.dumps(error)[:300]


def _add_usage(st: _TurnState, part: dict) -> None:
    """Sum one ``step_finish`` part's tokens and cost into the turn's totals,
    in the field names ``turn_dispatch._result_detail`` reads."""
    tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    for key, value in (("input_tokens", tokens.get("input")),
                       ("output_tokens", tokens.get("output")),
                       ("cache_read_input_tokens", cache.get("read")),
                       ("cache_creation_input_tokens", cache.get("write"))):
        if isinstance(value, (int, float)):
            st.usage[key] = st.usage.get(key, 0) + int(value)
    if isinstance(part.get("cost"), (int, float)):
        st.cost = (st.cost or 0.0) + float(part["cost"])


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
    # --- catalogue data ---------------------------------------------------
    id = 'opencode'
    label = 'OpenCode'
    required_binary = 'opencode'
    aliases = ('open-code', 'opencode-ai')
    badge = 'BackendOpenCode'
    detail = 'Runs on OpenCode.'
    symbol = 'chevron.left.forwardslash.chevron.right'
    brand = BackendBrand('#16352b', '#0b1c16', '#5ee4b5', '#1f8a65')
    efforts = ('low', 'medium', 'high', 'max')
    runner = 'opencode'
    config_model_field = 'opencode_model'
    config_effort_field = 'opencode_effort'
    fallback_models = (
        ('opencode/gpt-5.4', 'GPT-5.4'),
        ('anthropic/claude-sonnet-4-5', 'Claude Sonnet 4.5'),
        ('openai/gpt-5.4', 'GPT-5.4 (OpenAI)'),
    )


    transcript = opencode_transcript

    @staticmethod
    def turn_env() -> dict[str, str]:
        """Deny OpenCode's own ``question`` tool. ``opencode run`` has nobody to
        answer it, so the call ends "dismissed" (or waits); agents ask through
        Clarp's durable questions instead. ``OPENCODE_PERMISSION`` rules apply
        after the user's config, so an allow-all ``permission`` stays as it is."""
        rules: Any = {}
        try:
            rules = json.loads(os.environ.get("OPENCODE_PERMISSION") or "{}")
        except json.JSONDecodeError:
            rules = {}
        if not isinstance(rules, dict):
            rules = {}
        rules.setdefault("question", "deny")
        return {"OPENCODE_PERMISSION": json.dumps(rules)}

    def build_cmd(self, session_id: str = "", *, is_new_session: bool = False,
                  model: str = "", effort: str = "") -> list[str]:
        cmd = [self.required_binary,
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
        opencode_bin = self.required_binary
        if shutil.which(opencode_bin) is None:
            raise FileNotFoundError(
                f"`{opencode_bin}` not on PATH — install OpenCode "
                f"(https://opencode.ai) to run OpenCode-backed agents")
        cwd = pathlib.Path(os.path.expanduser(str(cwd)))
        agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
        persona = (agent or {}).get("persona") or ""
        prompt = apply_voice_preamble(
            text, voice=voice_preamble, persona=persona, session=session)
        cmd = self.build_cmd(
            backend_session_id, is_new_session=is_new_session,
            model=model, effort=effort)
        cmd.append(prompt)
        flag = "resume" if (backend_session_id and not is_new_session) else "new"
        log("opencodeSpawn", f"cwd={cwd} {flag}={backend_session_id or '∅'} "
                             f"text_len={len(text)} trace={trace_id or '∅'} "
                             f"agent={agent_id or '∅'}")
        proc, handle = self.launch(cmd, cwd=cwd, session=session,
                                   env_extra=self.turn_env())
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
        st = _TurnState(session_id=backend_session_id)
        if backend_session_id:
            self._bind(st, backend_session_id, on_session_init=on_session_init,
                       on_error=on_error, trace_id=trace_id)
            self._transition(agent_id, TurnEvent.SPAWN_STARTED,
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
                    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
                    if etype == "step_finish":
                        _add_usage(st, part)
                        continue
                    if etype == "step_start":
                        st.step_texts = []
                        self._transition(agent_id, TurnEvent.TEXT_STREAMED, {
                            "dispatch": self.runner, "trace_id": trace_id,
                        })
                        self._broadcast(stream, agent_id, session)
                        continue
                    if etype in {"tool_use", "tool_start", "tool.running"}:
                        raw_name = str(part.get("tool") or ev.get("tool")
                                       or ev.get("name") or "tool")
                        state = part.get("state") if isinstance(part.get("state"), dict) else {}
                        raw_input = state.get("input") if isinstance(state.get("input"), dict) else {}
                        name = opencode_transcript._TOOL_NAMES.get(raw_name, raw_name)
                        tool_input = {
                            opencode_transcript._ARG_NAMES.get(str(k), str(k)):
                                truncate(v, 400) if isinstance(v, str) else v
                            for k, v in raw_input.items()
                            if isinstance(v, (str, int, float, bool))}
                        if name == "Bash" and tool_input.get("command"):
                            # As Codex does: the apps clip the command; the
                            # explainer completes it from here.
                            tool_explanation_commands.remember(
                                agent_id, str(tool_input["command"]))
                        self._transition(agent_id, TurnEvent.TOOL_STARTED, {
                            "dispatch": self.runner, "trace_id": trace_id, "tool": name,
                            "input": tool_input,
                        })
                        self._broadcast(stream, agent_id, session)
                        continue
                    if etype in {"error", "session.error"}:
                        err = (_error_message(ev.get("error"))
                               or str(ev.get("message") or "opencode error"))
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
                            st.step_texts.append(delta.strip())
                            st.last_agent_message = "\n\n".join(
                                filter(None, st.step_texts))
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
                result: dict[str, Any] = {"last_agent_message": st.last_agent_message,
                                          "usage": dict(st.usage)}
                if st.cost is not None:
                    result["total_cost_usd"] = st.cost
                on_result(result)
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

    def _transition(self, agent_id: str, turn_event: str, detail: dict[str, Any]) -> None:
        self.transition(agent_id, turn_event, detail, log_event="opencodeStateFail")

    def _broadcast(self, stream: Any, agent_id: str, session: str) -> None:
        self.broadcast_transcript(stream, agent_id, session)

    # ---- orchestrator routing ----------------------------------------------

    def routing_cmd(self, prompt: str, *, model: str = "", effort: str = "") -> list[str]:
        """argv for one ``opencode run`` request with no session (orchestrator)."""
        return self.build_cmd(model=model, effort=effort) + [prompt]

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
