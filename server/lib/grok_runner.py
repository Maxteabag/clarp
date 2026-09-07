"""Grok Build (``grok -p``) stream-json runtime adapter."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import threading
import uuid
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


GROK_BIN = "grok"
LIVE_TEXT_INTERVAL_SEC = 0.25
_REGISTRY = ProcessRegistry(log_exception=log_exception)


def active_handles(agent_id: str) -> list["TurnHandle"]:
    return _REGISTRY.active_handles(agent_id)


def interrupt(agent_id: str) -> int:
    return _REGISTRY.interrupt(agent_id, event="grokInterruptFail")


def build_cmd(session_id: str = "", *, is_new_session: bool = False,
              model: str = "", effort: str = "") -> list[str]:
    """argv for one headless Grok Build turn (prompt appended as ``-p``)."""
    cmd = [
        GROK_BIN, "--always-approve", "--no-alt-screen",
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
    if shutil.which(GROK_BIN) is None:
        raise FileNotFoundError(
            f"`{GROK_BIN}` not on PATH — install Grok Build "
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
    cmd = build_cmd(
        backend_session_id, is_new_session=is_new_session,
        model=model, effort=effort)
    cmd += ["-p", prompt]
    flag = "resume" if not is_new_session else "new"
    log("grokSpawn", f"cwd={cwd} {flag}={backend_session_id or '∅'} "
                     f"text_len={len(text)} trace={trace_id or '∅'} "
                     f"agent={agent_id or '∅'}")
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=(os.name == "posix"),
        env={**os.environ, "CLAUDE_PWA_SESSION": session},
    )
    attach_stderr_drain(proc)
    handle = TurnHandle(
        proc=proc, drain_thread=None,
        process_group=proc.pid if os.name == "posix" else None)  # type: ignore[arg-type]
    runtime_agent_id = "" if isolated else agent_id
    if runtime_agent_id:
        _REGISTRY.register(runtime_agent_id, handle)
    drain = threading.Thread(
        target=_drain_stdout,
        kwargs=dict(
            proc=proc, agent_id=runtime_agent_id, session=session,
            trace_id=trace_id, handle=handle,
            backend_session_id=backend_session_id,
            minted=bool(minted),
            on_session_init=on_session_init, on_result=on_result,
            on_error=on_error, stream=stream,
            enqueue=enqueue or tts_queue.enqueue,
        ),
        daemon=True, name=f"grok-drain-{proc.pid}",
    )
    handle.drain_thread = drain
    drain.start()
    return handle


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
    if etype in {"assistant", "agent_message", "message", "text", "output_text"}:
        role = str(ev.get("role") or "")
        if role and role not in {"assistant", "model"}:
            return ""
        return (_text_from(ev.get("content"))
                or _text_from(ev.get("text"))
                or _text_from(ev.get("message"))
                or _text_from(ev.get("delta")))
    if etype in {"tool_started", "tool_start", "tool_use"}:
        return ""
    return ""


def _drain_stdout(
    *, proc: subprocess.Popen, agent_id: str, session: str, trace_id: str,
    handle: "TurnHandle", backend_session_id: str, minted: bool,
    on_session_init, on_result, on_error, stream, enqueue,
) -> None:
    st = _TurnState(session_id=backend_session_id)
    try:
        if minted:
            _bind(st, backend_session_id, on_session_init=on_session_init,
                  on_error=on_error, trace_id=trace_id)
            _record_state(agent_id, AgentState.THINKING,
                          {"dispatch": "grok", "trace_id": trace_id})
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
                if etype in {"tool_started", "tool_start", "tool_use",
                             "item.started"}:
                    _record_state(agent_id, AgentState.TOOL, {
                        "dispatch": "grok", "trace_id": trace_id,
                        "tool": str(ev.get("name") or ev.get("tool") or "tool"),
                    })
                    _broadcast(stream, agent_id, session)
                    continue
                if etype in {"turn_started", "turn.started"}:
                    _record_state(agent_id, AgentState.THINKING,
                                  {"dispatch": "grok", "trace_id": trace_id})
                    continue
                if etype in {"error", "turn.failed", "turn_failed"}:
                    err = str(ev.get("error") or ev.get("message") or "grok error")
                    st.failed_error = err
                    if on_error is not None:
                        on_error(err)
                    continue
                delta = _assistant_delta(ev)
                if delta:
                    st.live_text += delta
                    st.last_agent_message = st.live_text
                    _speak(delta, st, agent_id=agent_id, session=session,
                           trace_id=trace_id, enqueue=enqueue)
                    _broadcast(stream, agent_id, session)
        rc = proc.wait()
        if rc != 0 and not st.failed_error:
            err = stderr_text(proc) or f"grok exited rc={rc}"
            log("grokExitErr", f"rc={rc} trace={trace_id or '∅'} "
                               f"stderr={(err or '')[:500]!r}")
            if on_error is not None:
                on_error(err)
        elif on_result is not None and not st.failed_error:
            if not st.saw_session and backend_session_id:
                _bind(st, backend_session_id, on_session_init=on_session_init,
                      on_error=on_error, trace_id=trace_id)
            on_result({"last_agent_message": st.last_agent_message, "usage": {}})
    except Exception as error:  # noqa: BLE001
        log_exception("grokDrainFail", error, detail=trace_id)
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
    log("grokSessionInit", f"sid={session_id} trace={trace_id or '∅'}")
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
            log_exception("grokSpeakFail", error, detail=trace_id)


def _record_state(agent_id: str, kind: str, detail: dict[str, Any]) -> None:
    if not agent_id:
        return
    try:
        agents_db.record_state(agent_id, kind, detail)
    except Exception as error:  # noqa: BLE001
        log_exception("grokStateFail", error)


def _broadcast(stream: Any, agent_id: str, session: str) -> None:
    if stream is None or not agent_id:
        return
    try:
        stream.broadcast(SSEType.TRANSCRIPT_UPDATED, {
            "agent_id": agent_id, "session": session,
            "source": TurnSource.PWA,
        })
    except Exception as error:  # noqa: BLE001
        log_exception("grokBroadcastFail", error)


# ---- orchestrator routing -------------------------------------------------

def routing_cmd(prompt: str, *, model: str = "", effort: str = "") -> list[str]:
    """argv for one headless Grok Build request with no session (orchestrator)."""
    return build_cmd(model=model, effort=effort) + ["-p", prompt]


def routing_text(stdout: str) -> str:
    """Concatenated assistant text of a streaming-json run."""
    text = ""
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            text += _assistant_delta(ev)
    return text
