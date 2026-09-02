"""OpenCode (``opencode run --format json``) runtime adapter."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import agents as agents_db
from . import tts_queue
from .codex_runner import apply_voice_preamble
from .log import log, log_exception
from .proc_util import attach_stderr_drain, stderr_text
from .process_registry import ProcessRegistry, TurnHandle
from .protocol import AgentState, SSEType, TurnSource
from .voice_markup import spoken_chunks_for_tts


OPENCODE_BIN = "opencode"
_REGISTRY = ProcessRegistry(log_exception=log_exception)


def active_handles(agent_id: str) -> list["TurnHandle"]:
    return _REGISTRY.active_handles(agent_id)


def interrupt(agent_id: str) -> int:
    return _REGISTRY.interrupt(agent_id, event="opencodeInterruptFail")


def build_cmd(session_id: str = "", *, is_new_session: bool = False,
              model: str = "", effort: str = "") -> list[str]:
    cmd = [OPENCODE_BIN, "run", "--format", "json", "--auto"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--variant", effort]
    if session_id and not is_new_session:
        cmd += ["--session", session_id]
    return cmd


@dataclass
class _TurnState:
    session_id: str = ""
    last_agent_message: str = ""
    live_text: str = ""
    failed_error: str = ""
    saw_session: bool = False
    seen_speak: set[str] = field(default_factory=set)


def spawn_turn(
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
    if shutil.which(OPENCODE_BIN) is None:
        raise FileNotFoundError(
            f"`{OPENCODE_BIN}` not on PATH — install OpenCode "
            f"(https://opencode.ai) to run OpenCode-backed agents")
    cwd = pathlib.Path(os.path.expanduser(str(cwd)))
    agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
    persona = (agent or {}).get("persona") or ""
    prompt = apply_voice_preamble(
        text, voice=voice_preamble, persona=persona, session=session)
    cmd = build_cmd(
        backend_session_id, is_new_session=is_new_session,
        model=model, effort=effort)
    cmd.append(prompt)
    flag = "resume" if (backend_session_id and not is_new_session) else "new"
    log("opencodeSpawn", f"cwd={cwd} {flag}={backend_session_id or '∅'} "
                         f"text_len={len(text)} trace={trace_id or '∅'} "
                         f"agent={agent_id or '∅'}")
    env = {**os.environ, "CLAUDE_PWA_SESSION": session}
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        env=env,
    )
    attach_stderr_drain(proc)
    handle = TurnHandle(proc=proc, drain_thread=None)  # type: ignore[arg-type]
    runtime_agent_id = "" if isolated else agent_id
    if runtime_agent_id:
        _REGISTRY.register(runtime_agent_id, handle)
    drain = threading.Thread(
        target=_drain_stdout,
        kwargs=dict(
            proc=proc, agent_id=runtime_agent_id, session=session,
            trace_id=trace_id, handle=handle,
            backend_session_id=backend_session_id,
            on_session_init=on_session_init, on_result=on_result,
            on_error=on_error, stream=stream,
            enqueue=enqueue or tts_queue.enqueue,
        ),
        daemon=True, name=f"opencode-drain-{proc.pid}",
    )
    handle.drain_thread = drain
    drain.start()
    return handle


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


def _drain_stdout(
    *, proc: subprocess.Popen, agent_id: str, session: str, trace_id: str,
    handle: "TurnHandle", backend_session_id: str,
    on_session_init, on_result, on_error, stream, enqueue,
) -> None:
    st = _TurnState(session_id=backend_session_id)
    if backend_session_id:
        _bind(st, backend_session_id, on_session_init=on_session_init,
              on_error=on_error, trace_id=trace_id)
        _record_state(agent_id, AgentState.THINKING,
                      {"dispatch": "opencode", "trace_id": trace_id})
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
                    _bind(st, sid, on_session_init=on_session_init,
                          on_error=on_error, trace_id=trace_id)
                etype = str(ev.get("type") or ev.get("event") or "")
                if etype in {"tool_use", "tool_start", "tool.running",
                             "step_start"}:
                    name = ""
                    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
                    name = str(part.get("tool") or ev.get("tool")
                               or ev.get("name") or "tool")
                    _record_state(agent_id, AgentState.TOOL, {
                        "dispatch": "opencode", "trace_id": trace_id, "tool": name,
                    })
                    _broadcast(stream, agent_id, session)
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
                        _speak(delta, st, agent_id=agent_id, session=session,
                               trace_id=trace_id, enqueue=enqueue)
                        _broadcast(stream, agent_id, session)
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
            _REGISTRY.unregister(agent_id, handle)


def _bind(st: _TurnState, session_id: str, *, on_session_init, on_error,
          trace_id: str) -> None:
    if not session_id or st.saw_session:
        return
    st.session_id = session_id
    st.saw_session = True
    log("opencodeSessionInit", f"sid={session_id} trace={trace_id or '∅'}")
    if on_session_init is not None:
        accepted = on_session_init(session_id)
        if accepted is False:
            st.failed_error = "backend session binding rejected"
            if on_error is not None:
                on_error(st.failed_error)


def _speak(text: str, st: _TurnState, *, agent_id: str, session: str,
           trace_id: str, enqueue) -> None:
    for chunk in spoken_chunks_for_tts(text):
        key = chunk.strip()
        if not key or key in st.seen_speak:
            continue
        st.seen_speak.add(key)
        try:
            enqueue(session=session, agent_id=agent_id, text=chunk,
                    trace_id=trace_id)
        except Exception as error:  # noqa: BLE001
            log_exception("opencodeSpeakFail", error, detail=trace_id)


def _record_state(agent_id: str, kind: str, detail: dict[str, Any]) -> None:
    if not agent_id:
        return
    try:
        agents_db.record_state(agent_id, kind, detail)
    except Exception as error:  # noqa: BLE001
        log_exception("opencodeStateFail", error)


def _broadcast(stream: Any, agent_id: str, session: str) -> None:
    if stream is None or not agent_id:
        return
    try:
        stream.broadcast(SSEType.TRANSCRIPT_UPDATED, {
            "agent_id": agent_id, "session": session,
            "source": TurnSource.PWA,
        })
    except Exception as error:  # noqa: BLE001
        log_exception("opencodeBroadcastFail", error)
