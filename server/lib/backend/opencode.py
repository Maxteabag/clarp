"""OpenCode: a CLI fronting several model providers.

Turns run live through ``opencode serve`` (see ``opencode_serve``), with
``opencode run --format json`` as the fallback when the server cannot start.
"""
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
from ..proc_util import attach_stderr_drain, stderr_text
from ..text_util import truncate
from ..process_registry import TurnHandle
from ..turn_lifecycle import TurnEvent
from ..voice_preamble import apply_voice_preamble
from .base import BackendBrand
from . import opencode_serve
from .opencode_serve import OpenCodeTurnHandle, ServeClient, ServeError
from .stream_json import StreamJsonBackend, iter_json_dicts, popen_turn


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
    # Live (``opencode serve``) turns only.
    persisted_live_text: str = ""
    last_live_write_at: float = 0.0
    part_types: dict[str, str] = field(default_factory=dict)
    part_texts: dict[str, str] = field(default_factory=dict)
    step_parts: list[str] = field(default_factory=list)
    started_tools: set[str] = field(default_factory=set)
    phase: str = ""
    busy: bool = False
    errors: list[str] = field(default_factory=list)
    # Assistant messages of this turn. Every step opens with a step-start
    # part; the prompt's own message never has one. (``message.updated``
    # arrives after a message's parts, so the role cannot be known first.)
    assistant_messages: set[str] = field(default_factory=set)


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


def _tool_detail(agent_id: str, part: dict) -> tuple[str, dict[str, Any]]:
    """The shared tool name and bounded input for a tool part's live state."""
    raw_name = str(part.get("tool") or part.get("name") or "tool")
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    raw_input = state.get("input") if isinstance(state.get("input"), dict) else {}
    name = opencode_transcript._TOOL_NAMES.get(raw_name, raw_name)
    tool_input = {
        opencode_transcript._ARG_NAMES.get(str(k), str(k)):
            truncate(v, 400) if isinstance(v, str) else v
        for k, v in raw_input.items()
        if isinstance(v, (str, int, float, bool))}
    if name == "Bash" and raw_input.get("command"):
        # As Codex does: the apps clip the command; the explainer completes
        # it from here.
        tool_explanation_commands.remember(agent_id, str(raw_input["command"]))
    return name, tool_input


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
    """One ``opencode serve`` per turn, followed live; ``opencode run`` as the
    fallback. No compaction or terminal yet."""
    # --- catalogue data ---------------------------------------------------
    id = 'opencode'
    label = 'OpenCode'
    required_binary = 'opencode'
    aliases = ('open-code', 'opencode-ai')
    badge = 'BackendOpenCode'
    detail = 'Runs on OpenCode.'
    symbol = 'chevron.left.forwardslash.chevron.right'
    brand = BackendBrand('#16352b', '#0b1c16', '#5ee4b5', '#1f8a65')
    # OpenCode's reasoning variants, least to most. Each model supports its
    # own subset (``opencode models --verbose``), so the catalogue lists
    # efforts per model; ``--variant`` / the prompt's ``variant`` take them.
    efforts = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
    effort_scope = 'model'
    runner = 'opencode'
    config_model_field = 'opencode_model'
    config_effort_field = 'opencode_effort'
    # Shown only when ``opencode models`` cannot be read. OpenCode Zen's own
    # model first (no provider key needed), then models the common providers
    # listed on 2026-09-28.
    fallback_models = (
        ('opencode/big-pickle', 'Big Pickle'),
        ('huggingface/deepseek-ai/DeepSeek-V4.1-Flash', 'DeepSeek V4.1 Flash (Hugging Face)'),
        ('fireworks-ai/accounts/fireworks/models/kimi-k3', 'Kimi K3 (Fireworks)'),
        ('huggingface/zai-org/GLM-5.3', 'GLM-5.3 (Hugging Face)'),
    )


    transcript = opencode_transcript
    #: Follow turns live through ``opencode serve``. ``CLARP_OPENCODE_LIVE=0``
    #: in the runtime's environment forces the ``opencode run`` path.
    live = True

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
                  model: str = "", effort: str = "", thinking: bool = False) -> list[str]:
        cmd = [self.required_binary,
               "run", "--format", "json", "--auto"]
        if thinking:
            # Emit `reasoning` events. How much a model reasons is its
            # variant (effort); this only stops run from hiding it.
            cmd.append("--thinking")
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
        flag = "resume" if (backend_session_id and not is_new_session) else "new"
        live = self.live and os.environ.get("CLARP_OPENCODE_LIVE", "1") != "0"
        log("opencodeSpawn", f"cwd={cwd} {flag}={backend_session_id or '∅'} "
                             f"text_len={len(text)} trace={trace_id or '∅'} "
                             f"agent={agent_id or '∅'} live={int(live)}")
        run_cmd = self.build_cmd(
            backend_session_id, is_new_session=is_new_session,
            model=model, effort=effort, thinking=True) + [prompt]
        runtime_agent_id = "" if isolated else agent_id
        callbacks = dict(
            agent_id=runtime_agent_id, session=session,
            trace_id=trace_id, backend_session_id=backend_session_id,
            on_session_init=on_session_init, on_result=on_result,
            on_error=on_error, stream=stream,
            enqueue=enqueue or tts_queue.enqueue,
        )
        if not live:
            proc, handle = self.launch(run_cmd, cwd=cwd, session=session,
                                       env_extra=self.turn_env())
            self.register_handle(runtime_agent_id, handle)
            self.start_drain(handle, self._drain_stdout, proc=proc,
                             handle=handle, **callbacks)
            return handle
        password = opencode_serve.server_password()
        proc = popen_turn(
            opencode_serve.serve_cmd(opencode_bin), cwd=cwd, session=session,
            stdin=subprocess.PIPE,
            env_extra={**self.turn_env(), "OPENCODE_SERVER_PASSWORD": password})
        attach_stderr_drain(proc)
        handle = OpenCodeTurnHandle(
            proc=proc, drain_thread=None,
            process_group=proc.pid if os.name == "posix" else None)
        self.register_handle(runtime_agent_id, handle)
        self.start_drain(
            handle, self._drain_serve, handle=handle, cwd=cwd,
            password=password, prompt=prompt, model=model, effort=effort,
            resume=bool(backend_session_id and not is_new_session),
            run_cmd=run_cmd, **callbacks)
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
                    if etype == "reasoning":
                        # Model thinking: busy, as Codex reasoning items are;
                        # never chat text.
                        self._transition(agent_id, TurnEvent.TEXT_STREAMED, {
                            "dispatch": self.runner, "trace_id": trace_id,
                        })
                        continue
                    if etype == "step_start":
                        st.step_texts = []
                        self._transition(agent_id, TurnEvent.TEXT_STREAMED, {
                            "dispatch": self.runner, "trace_id": trace_id,
                        })
                        self._broadcast(stream, agent_id, session)
                        continue
                    if etype in {"tool_use", "tool_start", "tool.running"}:
                        name, tool_input = _tool_detail(agent_id, part or ev)
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

    # ---- live turns (opencode serve) ---------------------------------------

    def _drain_serve(
        self, *, handle: OpenCodeTurnHandle, cwd: pathlib.Path, password: str,
        prompt: str, model: str, effort: str, resume: bool, run_cmd: list[str],
        agent_id: str, session: str, trace_id: str, backend_session_id: str,
        on_session_init, on_result, on_error, stream, enqueue,
    ) -> None:
        """Run one turn against the per-turn server and follow its events."""
        proc = handle.proc
        callbacks = dict(
            agent_id=agent_id, session=session, trace_id=trace_id,
            backend_session_id=backend_session_id,
            on_session_init=on_session_init, on_result=on_result,
            on_error=on_error, stream=stream, enqueue=enqueue)
        url = opencode_serve.wait_for_url(proc)
        if not url:
            self._close_server(handle)
            if handle.stop_requested:
                self._finish_stopped(handle, agent_id=agent_id, on_error=on_error)
                return
            log("opencodeServeFallback",
                f"trace={trace_id or '∅'} rc={proc.poll()} "
                f"stderr={stderr_text(proc)[:300]!r}")
            self._fall_back_to_run(handle, run_cmd, cwd=cwd, **callbacks)
            return
        client = ServeClient(url, password, str(cwd))
        st = _TurnState(session_id=backend_session_id if resume else "")
        try:
            sid = self._open_session(client, st, resume=resume,
                                     backend_session_id=backend_session_id,
                                     on_session_init=on_session_init,
                                     on_error=on_error, trace_id=trace_id)
            if not sid:
                return
            handle.abort = lambda: client.request(
                "POST", f"/session/{sid}/abort",
                timeout=opencode_serve.ABORT_TIMEOUT)
            self._transition(agent_id, TurnEvent.SPAWN_STARTED,
                             {"dispatch": self.runner, "trace_id": trace_id})
            events = client.open_events()
            body: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
            pair = opencode_serve.split_model(model)
            if pair:
                body["model"] = pair
            if effort:
                body["variant"] = effort
            client.request("POST", f"/session/{sid}/prompt_async", body)
            sessions = {sid}
            finished = False
            for event in events:
                if self._on_event(event, st, client=client, sid=sid,
                                  sessions=sessions, **callbacks):
                    finished = True
                    break
            if handle.stop_requested:
                self._finish_stopped(handle, agent_id=agent_id, on_error=on_error)
            elif not finished:
                detail = stderr_text(proc).strip()
                self._fail(st, on_error, "OpenCode server stopped before the turn "
                           "finished" + (f": {detail[-300:]}" if detail else ""))
            elif st.errors:
                self._fail(st, on_error, "\n".join(st.errors))
            elif on_result is not None:
                on_result(self._result(st))
        except Exception as error:  # noqa: BLE001
            if handle.stop_requested:
                self._finish_stopped(handle, agent_id=agent_id, on_error=on_error)
            else:
                log_exception("opencodeServeFail", error, detail=trace_id)
                self._fail(st, on_error, str(error)[:300])
        finally:
            handle.abort = None
            self._close_server(handle)
            if agent_id:
                self.unregister_handle(agent_id, handle)

    def _open_session(self, client: ServeClient, st: _TurnState, *, resume: bool,
                      backend_session_id: str, on_session_init, on_error,
                      trace_id: str) -> str:
        """Bind the turn's session: the resumed one, or a new one."""
        if resume:
            try:
                client.request("GET", f"/session/{backend_session_id}")
            except ServeError:
                self._fail(st, on_error,
                           f"OpenCode session {backend_session_id} not found")
                return ""
            sid = backend_session_id
        else:
            created = client.request(
                "POST", "/session",
                {"permission": opencode_serve.SESSION_PERMISSION})
            sid = str((created or {}).get("id") or "") if isinstance(created, dict) else ""
            if not sid:
                self._fail(st, on_error, "OpenCode did not create a session")
                return ""
        self._bind(st, sid, on_session_init=on_session_init, on_error=on_error,
                   trace_id=trace_id)
        return "" if st.failed_error else sid

    def _on_event(self, event: dict, st: _TurnState, *, client: ServeClient,
                  sid: str, sessions: set[str], agent_id: str, session: str,
                  trace_id: str, stream, enqueue, **_callbacks) -> bool:
        """Apply one server event; True when the turn is over."""
        etype = str(event.get("type") or "")
        props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
        if etype == "session.created":
            info = props.get("info") if isinstance(props.get("info"), dict) else {}
            if info.get("parentID") in sessions and info.get("id"):
                # A task sub-agent; its permission prompts are answered too.
                sessions.add(str(info["id"]))
            return False
        if etype == "message.part.updated":
            part = props.get("part") if isinstance(props.get("part"), dict) else {}
            if part.get("type") == "step-start" and part.get("sessionID") == sid:
                st.assistant_messages.add(str(part.get("messageID") or ""))
            if part.get("sessionID") == sid and \
                    part.get("messageID") in st.assistant_messages:
                self._on_part(part, st, agent_id=agent_id, session=session,
                              trace_id=trace_id, stream=stream, enqueue=enqueue)
            return False
        if etype == "message.part.delta":
            if props.get("sessionID") == sid and props.get("field") == "text":
                self._on_delta(props, st, agent_id=agent_id, session=session,
                               trace_id=trace_id, stream=stream, enqueue=enqueue)
            return False
        if etype == "session.error":
            if props.get("sessionID") == sid and props.get("error"):
                st.errors.append(_error_message(props["error"]))
            return False
        if etype == "session.status":
            status = props.get("status") if isinstance(props.get("status"), dict) else {}
            if props.get("sessionID") != sid:
                return False
            if status.get("type") in {"busy", "retry"}:
                st.busy = True
                return False
            return status.get("type") == "idle" and st.busy
        if etype == "session.idle":
            return props.get("sessionID") == sid and st.busy
        if etype == "permission.asked":
            # What `opencode run --auto` does: approve once. Everything else is
            # already allowed or denied by the user's permission config.
            if props.get("sessionID") in sessions and props.get("id"):
                client.request("POST", f"/permission/{props['id']}/reply",
                               {"reply": "once"})
            return False
        if etype == "question.asked":
            # Denied by permission; should one still be asked, nobody is there
            # to answer it, and waiting would hang the turn.
            if props.get("sessionID") in sessions and props.get("id"):
                client.request("POST", f"/question/{props['id']}/reject")
            return False
        return False

    def _on_part(self, part: dict, st: _TurnState, *, agent_id: str, session: str,
                 trace_id: str, stream, enqueue) -> None:
        part_id = str(part.get("id") or "")
        ptype = str(part.get("type") or "")
        if part_id:
            st.part_types[part_id] = ptype
        st.busy = True
        if ptype == "step-start":
            st.step_parts = []
            self._phase(st, "thinking", agent_id, trace_id)
            self._broadcast(stream, agent_id, session)
        elif ptype == "step-finish":
            _add_usage(st, part)
        elif ptype == "reasoning":
            # Model thinking keeps the agent busy (as Codex reasoning items
            # do) and is never shown as chat text.
            self._phase(st, "thinking", agent_id, trace_id)
        elif ptype == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = str(state.get("status") or "")
            if status in {"running", "completed", "error"} and part_id not in st.started_tools:
                st.started_tools.add(part_id)
                name, tool_input = _tool_detail(agent_id, part)
                st.phase = "tool"
                self._transition(agent_id, TurnEvent.TOOL_STARTED, {
                    "dispatch": self.runner, "trace_id": trace_id, "tool": name,
                    "input": tool_input,
                })
                self._broadcast(stream, agent_id, session)
            if status in {"completed", "error"}:
                self._broadcast(stream, agent_id, session)
        elif ptype == "text" and not part.get("synthetic") and not part.get("ignored"):
            st.part_texts[part_id] = str(part.get("text") or "")
            ended = bool((part.get("time") or {}).get("end")) \
                if isinstance(part.get("time"), dict) else False
            self._on_text(part_id, st, ended=ended, agent_id=agent_id,
                          session=session, trace_id=trace_id, stream=stream,
                          enqueue=enqueue)

    def _on_delta(self, props: dict, st: _TurnState, *, agent_id: str, session: str,
                  trace_id: str, stream, enqueue) -> None:
        part_id = str(props.get("partID") or "")
        delta = props.get("delta")
        if not part_id or not isinstance(delta, str):
            return
        if st.part_types.get(part_id) == "reasoning":
            self._phase(st, "thinking", agent_id, trace_id)
        elif st.part_types.get(part_id) == "text":
            st.part_texts[part_id] = st.part_texts.get(part_id, "") + delta
            self._on_text(part_id, st, ended=False, agent_id=agent_id,
                          session=session, trace_id=trace_id, stream=stream,
                          enqueue=enqueue)

    def _on_text(self, part_id: str, st: _TurnState, *, ended: bool, agent_id: str,
                 session: str, trace_id: str, stream, enqueue) -> None:
        """Speak finished ``<speak>`` blocks and show the text as it streams."""
        text = st.part_texts.get(part_id, "")
        if part_id not in st.step_parts:
            st.step_parts.append(part_id)
        st.last_agent_message = "\n\n".join(filter(None, (
            st.part_texts.get(pid, "").strip() for pid in st.step_parts)))
        self._phase(st, "thinking", agent_id, trace_id)
        self._speak(text, st, agent_id=agent_id, session=session,
                    trace_id=trace_id, enqueue=enqueue)
        self.persist_live_text(
            st, text=text.strip(), backend_session_id=st.session_id,
            agent_id=agent_id, session=session, trace_id=trace_id,
            stream=stream, force=ended)
        if ended:
            self._broadcast(stream, agent_id, session)

    def _phase(self, st: _TurnState, phase: str, agent_id: str, trace_id: str) -> None:
        """TEXT_STREAMED once per switch back from a tool, not per token."""
        if st.phase == phase:
            return
        st.phase = phase
        self._transition(agent_id, TurnEvent.TEXT_STREAMED,
                         {"dispatch": self.runner, "trace_id": trace_id})

    @staticmethod
    def _result(st: _TurnState) -> dict[str, Any]:
        result: dict[str, Any] = {"last_agent_message": st.last_agent_message,
                                  "usage": dict(st.usage)}
        if st.cost is not None:
            result["total_cost_usd"] = st.cost
        return result

    @staticmethod
    def _fail(st: _TurnState, on_error, message: str) -> None:
        if st.failed_error:
            return
        st.failed_error = message
        if on_error is not None:
            on_error(message)

    @staticmethod
    def _finish_stopped(handle: OpenCodeTurnHandle, *, agent_id: str, on_error) -> None:
        """A stopped turn ends as a stopped ``opencode run`` does: an error the
        host has already superseded (its Stop cleared the turn's slot)."""
        log("opencodeServeStopped", f"agent={agent_id or '∅'} pid={handle.pid}")
        if on_error is not None:
            try:
                on_error("OpenCode turn killed by signal (stopped)")
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _close_server(handle: OpenCodeTurnHandle) -> None:
        """End the server: close the supervisor's stdin, then make sure."""
        proc = handle.proc
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            handle.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log("opencodeServeKillFail", f"pid={proc.pid}")

    def _fall_back_to_run(self, handle: OpenCodeTurnHandle, run_cmd: list[str], *,
                          cwd: pathlib.Path, session: str, **callbacks: Any) -> None:
        """Run the turn with ``opencode run`` on the same handle, so a stop
        and the registry keep working."""
        proc = popen_turn(run_cmd, cwd=cwd, session=session,
                          env_extra=self.turn_env())
        attach_stderr_drain(proc)
        handle.proc = proc
        handle.process_group = proc.pid if os.name == "posix" else None
        if handle.stop_requested:
            # Stopped while the swap was in flight.
            handle.terminate()
        self._drain_stdout(proc=proc, handle=handle, session=session, **callbacks)

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
