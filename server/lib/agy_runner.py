"""AGY 1.1.21 headless stream-json runtime adapter."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import agents as agents_db
from . import agy_transcript
from . import provider_capabilities
from . import server_identity
from . import tts_queue
from .codex_runner import (
    _SPEAK_RE,
    apply_voice_preamble,
    spoken_chunks_for_tts,
    spoken_for_tts,
)
from .log import log, log_exception
from .proc_util import attach_stderr_drain, stderr_text
from .process_registry import ProcessRegistry, TurnHandle
from .protocol import AgentState, SSEType, TurnSource


AGY_BIN = "agy"  # resolved from PATH; tests can monkeypatch.
LIVE_TEXT_INTERVAL_SEC = 0.25
_CLEAN_STATUSES = {"SUCCESS"}
_SECRET_VALUE = re.compile(
    r"(?i)(authorization|token|password|passwd|secret|api[_-]?key)\s*[:=]\s*[^\s]+")

# Conversation id as agy logs it: "conversation=<uuid>" or
# "Created conversation <uuid>".
_CONV_RE = re.compile(
    r"conversation[=\s]+([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")

_REGISTRY = ProcessRegistry(log_exception=log_exception)


def active_handles(agent_id: str) -> list["TurnHandle"]:
    return _REGISTRY.active_handles(agent_id)


def interrupt(agent_id: str) -> int:
    """SIGTERM every in-flight agy turn for an agent. Idempotent."""
    return _REGISTRY.interrupt(agent_id, event="agyInterruptFail")


def _register(agent_id: str, h: "TurnHandle") -> None:
    _REGISTRY.register(agent_id, h)


def _unregister(agent_id: str, h: "TurnHandle") -> None:
    _REGISTRY.unregister(agent_id, h)


def build_cmd(conversation_id: str = "", *,
              is_new_session: bool = False, model: str = "",
              effort: str = "") -> list[str]:
    """argv (sans prompt) for one agy turn. The prompt is the VALUE of
    `--print` (a string flag) and is appended by spawn_turn as
    `--print=<prompt>`; `--log-file` is appended too.

    CRITICAL: `--print`/`-p` consumes the next token as its prompt value, so
    `agy -p --dangerously-skip-permissions <prompt>` makes agy treat
    "--dangerously-skip-permissions" as the prompt. Keep the prompt bound to
    --print via the `=` form and never leave it as a bare positional.

    Resume passes `--conversation <id>`; a fresh turn mints a new
    conversation whose id we recover from the log. `is_new_session` is
    accepted for signature parity; only conversation_id decides resume.

    Model values must be present in the last observed or bundled fallback
    catalog. ``4.8`` is rejected before spawn because it is not a catalog id."""
    cmd = [AGY_BIN, "--dangerously-skip-permissions",
           "--output-format", "stream-json"]
    if model and effort:
        raise ValueError(
            "AGY model-specific effort compatibility is unknown; "
            "use effort only with the provider-default model")
    if model:
        if not provider_capabilities.is_dispatchable_agy_model(model):
            raise ValueError(f"unavailable AGY model: {model!r}")
        cmd += ["--model", model]
    if effort:
        effort = effort.strip().lower()
        if effort not in {"low", "medium", "high"}:
            raise ValueError(f"invalid AGY effort: {effort!r}")
        cmd += ["--effort", effort]
    if conversation_id and not is_new_session:
        cmd += ["--conversation", conversation_id]
    return cmd


@dataclass
class _TurnState:
    seen_speak: set = field(default_factory=set)
    seen_usage_steps: set[str] = field(default_factory=set)
    event_hashes: dict[int, str] = field(default_factory=dict)
    seen_event_payloads: set[str] = field(default_factory=set)
    turn_usage: dict[str, int] = field(default_factory=dict)
    usage_refs: list[str] = field(default_factory=list)
    conversation_id: str = ""
    expected_conversation_id: str = ""
    live_text: str = ""
    persisted_live_text: str = ""
    last_live_write_at: float = 0.0
    terminal: str = ""
    pending_result: dict[str, Any] | None = None
    pending_error: str = ""
    callback_delivered: bool = False
    max_revision: int = 0
    turn_duration_seconds: float = 0.0
    evidence_scope: dict[str, Any] = field(default_factory=dict)
    baseline_snapshot: dict[str, Any] = field(default_factory=dict)


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
    run_if_owned: Optional[Callable[[Callable[[], None]], bool]] = None,
    isolated: bool = False,
) -> TurnHandle:
    """Spawn one AGY turn and normalize its NDJSON stream asynchronously."""
    if shutil.which(AGY_BIN) is None:
        raise FileNotFoundError(
            f"`{AGY_BIN}` not on PATH — install the antigravity CLI to run "
            f"agy-backed agents")
    cwd = pathlib.Path(os.path.expanduser(str(cwd)))
    # Every agy turn is app-dispatched: always prepend the
    # no-interactive-questions rule, plus the <speak> voice guidance on
    # spoken turns. (Same instruction block as the Codex backend.)
    agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
    persona = (agent or {}).get("persona") or ""
    prompt = apply_voice_preamble(
        text,
        voice=voice_preamble,
        persona=persona,
        session=session,
    )
    cmd = build_cmd(backend_session_id, is_new_session=is_new_session,
                    model=model, effort=effort)
    fd, log_path = tempfile.mkstemp(prefix="agy-", suffix=".log")
    os.close(fd)
    # Prompt is --print's value (the `=` form keeps a prompt that starts with
    # '-' from being mistaken for a flag). --print also triggers print mode.
    cmd += ["--log-file", log_path, f"--print={prompt}"]
    flag = "resume" if (backend_session_id and not is_new_session) else "new"
    log("agySpawn", f"cwd={cwd} {flag}={backend_session_id or '∅'} "
                    f"text_len={len(text)} trace={trace_id or '∅'} "
                    f"agent={agent_id or '∅'}")
    # Agy is busy from the moment we dispatch — no hook will say so. The
    # dispatcher gate makes the THINKING write and Popen one owner-atomic
    # admission, including retries. A stop/preemption before this point starts
    # neither state nor process.
    runtime_agent_id = "" if isolated else agent_id
    owner_gate = run_if_owned or (lambda action: (action(), True)[1])
    process: list[subprocess.Popen] = []

    def admit_spawn() -> None:
        if runtime_agent_id:
            _record_state(runtime_agent_id, AgentState.THINKING,
                          {"dispatch": "agy", "trace_id": trace_id})
        process.append(subprocess.Popen(
            cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=(os.name == "posix"),
            env={**os.environ, "CLAUDE_PWA_SESSION": session},
        ))
    try:
        admitted = owner_gate(admit_spawn)
        if not admitted or not process:
            raise RuntimeError("AGY turn ownership lost before process spawn")
        proc = process[0]
    except Exception:
        try:
            os.unlink(log_path)
        except OSError:
            pass
        raise
    attach_stderr_drain(proc)
    handle = TurnHandle(
        proc=proc, drain_thread=None,
        process_group=proc.pid if os.name == "posix" else None)   # type: ignore[arg-type]
    if runtime_agent_id:
        _register(runtime_agent_id, handle)
    drain = threading.Thread(
        target=_drain_stream,
        kwargs=dict(
            proc=proc, log_path=log_path, agent_id=runtime_agent_id,
            session=session, trace_id=trace_id, handle=handle,
            on_session_init=on_session_init, on_result=on_result,
            on_error=on_error, stream=stream,
            enqueue=enqueue or tts_queue.enqueue,
            expected_conversation_id=(
                backend_session_id if backend_session_id and not is_new_session
                else ""),
            run_if_owned=owner_gate,
        ),
        daemon=True, name=f"agy-drain-{proc.pid}",
    )
    handle.drain_thread = drain
    drain.start()
    return handle


def _drain_stream(
    *, proc: subprocess.Popen, log_path: str, agent_id: str,
    session: str, trace_id: str, handle: "TurnHandle",
    on_session_init, on_result, on_error, stream, enqueue,
    expected_conversation_id: str,
    run_if_owned,
) -> None:
    """Drain NDJSON with a strict exactly-one terminal callback latch."""
    st = _TurnState(expected_conversation_id=expected_conversation_id)
    try:
        st.evidence_scope = _turn_evidence_scope(agent_id, trace_id)
        revision = 0
        if proc.stdout is not None:
            for raw_line in proc.stdout:
                if st.terminal:
                    continue
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # bounded stdout noise is only terminal if no result
                if not isinstance(event, dict):
                    continue
                revision += 1
                try:
                    if not run_if_owned(lambda: _handle_event(
                            event, revision, st,
                            agent_id=agent_id, session=session, trace_id=trace_id,
                            on_session_init=on_session_init, on_result=on_result,
                            on_error=on_error, stream=stream, enqueue=enqueue)):
                        st.terminal = "superseded"
                except Exception as error:  # noqa: BLE001
                    _finish_error(
                        _runner_error(f"parser error: {str(error)[:300]}"), st,
                        on_error=on_error)
        rc = proc.wait()
        err = stderr_text(proc).strip()
        if st.terminal == "result" and rc != 0:
            st.terminal = "error"
            st.pending_result = None
            st.pending_error = _runner_error(err or f"process exited rc={rc}")
        if not st.terminal:
            conv_id = st.conversation_id or _conversation_id_from_log(log_path)
            if conv_id:
                if not run_if_owned(lambda: _bind_session(
                        conv_id, st, trace_id=trace_id,
                        on_session_init=on_session_init)):
                    st.terminal = "superseded"
                    return
            if rc != 0:
                _finish_error(_runner_error(err or f"process exited rc={rc}"), st,
                              on_error=on_error)
            else:
                _finish_error(_runner_error(
                    "parser error: missing valid stream-json result"),
                              st, on_error=on_error)
        if st.terminal == "result":
            if not run_if_owned(lambda: _finalize_success(
                    st, agent_id=agent_id, session=session,
                    trace_id=trace_id, stream=stream, enqueue=enqueue)):
                return
        elif st.terminal == "error":
            if not run_if_owned(lambda: _restore_failed_turn(
                    st, agent_id=agent_id, session=session,
                    trace_id=trace_id, stream=stream)):
                return
        if not run_if_owned(lambda: _deliver_terminal(
                st, on_result=on_result, on_error=on_error)):
            return
    except Exception as error:  # noqa: BLE001
        log_exception("agyDrainFail", error, detail=trace_id)
        if proc.poll() is None:
            try:
                handle.terminate()
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    handle.kill()
                except Exception:  # noqa: BLE001
                    pass
        if st.terminal == "result":
            st.terminal = "error"
            st.pending_result = None
            st.pending_error = _runner_error(
                f"drain error after result: {str(error)[:300]}")
        else:
            _finish_error(_runner_error(f"parser error: {str(error)[:300]}"), st,
                          on_error=on_error)
        if not run_if_owned(lambda: _restore_failed_turn(
                st, agent_id=agent_id, session=session,
                trace_id=trace_id, stream=stream)):
            return
        run_if_owned(lambda: _deliver_terminal(
            st, on_result=on_result, on_error=on_error))
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass
        if agent_id:
            _unregister(agent_id, handle)


def _handle_event(
    event: dict[str, Any], revision: int, st: _TurnState, *,
    agent_id: str, session: str, trace_id: str,
    on_session_init, on_result, on_error, stream, enqueue,
) -> None:
    evidence = _provider_evidence(event, revision, st)
    if evidence is None or st.terminal:
        return
    event_type = event.get("event")
    if not isinstance(event_type, str):
        return
    if event_type == "init":
        payload = event.get("init")
        if not isinstance(payload, dict):
            raise ValueError("init payload must be an object")
        conv_id = _event_conversation_id(event)
        _bind_session(conv_id, st, trace_id=trace_id,
                      on_session_init=on_session_init)
        _record_state(agent_id, AgentState.THINKING, {
            "dispatch": "agy", "agy_raw_evidence": evidence,
            "provider_init": {
                "model": payload.get("model") if isinstance(payload.get("model"), str) else None,
                "agent": payload.get("agent") if isinstance(payload.get("agent"), str) else None,
            }, "trace_id": trace_id,
        })
        return
    if event_type == "step_update":
        update = event.get("step_update")
        if not isinstance(update, dict):
            raise ValueError("step_update payload must be an object")
        conv_id = _event_conversation_id(event)
        _bind_session(conv_id, st, trace_id=trace_id,
                      on_session_init=on_session_init)
        _handle_step(update, evidence, st, agent_id=agent_id,
                     session=session, trace_id=trace_id, stream=stream)
        return
    if event_type == "result":
        _handle_result(
            event.get("result"), evidence, st,
            agent_id=agent_id, session=session, trace_id=trace_id,
            on_session_init=on_session_init, on_result=on_result,
            on_error=on_error, stream=stream, enqueue=enqueue,
        )


def _handle_step(update: dict[str, Any], evidence: dict[str, Any],
                 st: _TurnState, *, agent_id: str, session: str,
                 trace_id: str, stream: Any) -> None:
    step_type = str(update.get("step_type") or "")
    state = str(update.get("state") or "").upper()
    if state == "DONE":
        _capture_step_usage(update, evidence, st)
    if step_type == "agent_response":
        delta = update.get("text_delta")
        if delta is not None and not isinstance(delta, str):
            raise ValueError("agent text_delta must be a string")
        if delta:
            st.live_text += delta
            _persist_live_text(st, agent_id=agent_id, session=session,
                               trace_id=trace_id, stream=stream)
        _record_state(agent_id, AgentState.THINKING, {
            "dispatch": "agy", "agy_raw_evidence": evidence,
            "step_index": update.get("step_index"), "step_state": state,
            "trace_id": trace_id,
        })
        return
    if step_type == "tool":
        tool_info = update.get("tool_info")
        tool_info = tool_info if isinstance(tool_info, dict) else {}
        raw_name = update.get("tool_name") or tool_info.get("name") or "tool"
        name = _canonical_tool_name(str(raw_name))
        inp = _canonical_tool_input(name, tool_info.get("parameters"))
        status = "error" if tool_info.get("error") else (
            "ok" if state == "DONE" else "running")
        detail = {
            "dispatch": "agy", "tool": name, "input": inp,
            "status": status, "agy_raw_evidence": evidence,
            "step_index": update.get("step_index"), "step_state": state,
            "subagent_info": update.get("subagent_info")
                if isinstance(update.get("subagent_info"), dict) else None,
            "trace_id": trace_id,
        }
        _record_state(agent_id, AgentState.TOOL, detail)
        _broadcast_transcript(stream, agent_id, session)
        if state == "DONE":
            _record_state(agent_id, AgentState.THINKING, {
                "dispatch": "agy", "agy_raw_evidence": evidence,
                "trace_id": trace_id,
            })
        return
    if step_type == "checkpoint":
        _record_state(agent_id, AgentState.THINKING, {
            "dispatch": "agy", "agy_raw_evidence": evidence,
            "trace_id": trace_id,
        })


def _handle_result(result: Any, evidence: dict[str, Any], st: _TurnState, *,
                   agent_id: str, session: str, trace_id: str,
                   on_session_init, on_result, on_error, stream, enqueue) -> None:
    if not isinstance(result, dict):
        _finish_error(_runner_error("parser error: result must be an object"), st,
                      on_error=on_error)
        return
    conv_id = result.get("conversation_id")
    if not isinstance(conv_id, str) or not conv_id.strip():
        _finish_error(_runner_error("parser error: result conversation_id missing"), st,
                      on_error=on_error)
        return
    try:
        _bind_session(conv_id.strip(), st, trace_id=trace_id,
                      on_session_init=on_session_init)
    except ValueError as error:
        _finish_error(_runner_error(f"parser error: {error}"), st, on_error=on_error)
        return
    status = result.get("status")
    error_value = result.get("error")
    if not isinstance(status, str):
        _finish_error(_runner_error("parser error: result status missing"), st,
                      on_error=on_error)
        return
    if error_value is not None and not isinstance(error_value, str):
        _finish_error(_runner_error("parser error: result error must be a string"), st,
                      on_error=on_error)
        return
    error_text = (error_value or "").strip()
    if status.upper() not in _CLEAN_STATUSES or error_text:
        _finish_error(_runner_error(
            f"status={status}: {error_text or 'provider turn failed'}"), st,
            on_error=on_error)
        return
    response = result.get("response")
    if not isinstance(response, str):
        _finish_error(_runner_error("parser error: result.response must be a string"), st,
                      on_error=on_error)
        return
    terminal_duration = result.get("duration_seconds")
    if (terminal_duration is not None
            and (isinstance(terminal_duration, bool)
                 or not isinstance(terminal_duration, (int, float))
                 or terminal_duration < 0)):
        _finish_error(_runner_error(
            "parser error: result.duration_seconds must be non-negative"), st,
            on_error=on_error)
        return
    st.live_text = response  # authoritative, committed only after rc==0
    cumulative = _normalize_usage(result.get("usage"))
    raw_usage = {
        "internal_schema": "agy_reported_usage_aggregate_v1",
        "noncanonical": True,
        "normalized_step_sum": dict(st.turn_usage),
        "step_event_refs": list(st.usage_refs),
        "step_duration_seconds_sum": st.turn_duration_seconds,
        "terminal_values": cumulative,
        "terminal_local_stream_revision": evidence["source_revision"],
        "terminal_event_ref": evidence["provider_event_ref"],
    }
    event = {
        "last_agent_message": response,
        "agy_raw_evidence": evidence,
        "agy_reported_usage": raw_usage,
    }
    if terminal_duration is not None:
        event["duration_ms"] = int(round(float(terminal_duration) * 1000))
    _finish_result(event, st, on_result=on_result)


def _finalize_success(st: _TurnState, *, agent_id: str, session: str,
                      trace_id: str, stream: Any, enqueue) -> None:
    response = str((st.pending_result or {}).get("last_agent_message") or "")
    st.live_text = response
    if st.baseline_snapshot is None:
        raise RuntimeError("AGY turn authority baseline missing")
    status = "success" if response.strip() else "empty"
    committed = agents_db.commit_agy_assistant_turn(
        agent_id=agent_id, backend_session_id=st.conversation_id,
        trace_id=trace_id, snapshot=st.baseline_snapshot,
        terminal_status=status, text=response)
    if committed is None:
        raise RuntimeError("turn ownership lost during AGY finalization")
    if "id" in committed:
        agents_db.apply_final_assistant_side_effects(
            agent_id=agent_id, backend_session_id=st.conversation_id,
            trace_id=trace_id, row=committed)
        st.persisted_live_text = response
    else:
        st.persisted_live_text = ""
    _speak(response, st, agent_id=agent_id, session=session,
           trace_id=trace_id, enqueue=enqueue)
    _broadcast_transcript(stream, agent_id, session)


def _retract_live_text(st: _TurnState, *, agent_id: str, session: str,
                       trace_id: str, stream: Any) -> None:
    if agent_id and st.conversation_id and agents_db.delete_live_assistant_message(
            agent_id=agent_id, backend_session_id=st.conversation_id,
            trace_id=trace_id):
        st.persisted_live_text = ""
        _broadcast_transcript(stream, agent_id, session)


def _restore_failed_turn(st: _TurnState, *, agent_id: str, session: str,
                         trace_id: str, stream: Any) -> None:
    if st.baseline_snapshot is not None and agents_db.commit_agy_assistant_turn(
            agent_id=agent_id, backend_session_id=st.conversation_id,
            trace_id=trace_id, snapshot=st.baseline_snapshot,
            terminal_status="error") is not None:
        st.persisted_live_text = ""
        _broadcast_transcript(stream, agent_id, session)
        return
    _retract_live_text(st, agent_id=agent_id, session=session,
                       trace_id=trace_id, stream=stream)


def _runner_error(detail: str) -> str:
    # error_classify checks quota/transient before runner-exit, so generic 429
    # remains transient while otherwise-unknown parser/provider errors surface.
    return f"agy exited rc=1: {detail[:900]}"


def _conversation_id_from_log(log_path: str) -> str:
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _CONV_RE.search(line)
                if m:
                    return m.group(1)
    except OSError as e:
        log_exception("agyLogReadFail", e, detail=log_path)
    return ""


def _turn_evidence_scope(agent_id: str, trace_id: str) -> dict[str, Any]:
    computer_id = server_identity.get_server_info()["server_id"]
    row = None
    if agent_id:
        row = agents_db.conn().execute(
            """SELECT turn_id, runtime_id, trace_id FROM turns
                 WHERE agent_id = ? AND (? = '' OR trace_id = ?)
                 ORDER BY started_at DESC, turn_id DESC LIMIT 1""",
            (agent_id, trace_id, trace_id),
        ).fetchone()
    turn_id = int(row["turn_id"]) if row else None
    return {
        "agent_id": agent_id,
        "computer_id": computer_id,
        "provider_instance_id": f"{computer_id}:agy",
        "account_auth_generation": None,
        "turn_execution_id": (
            f"{computer_id}:turn:{turn_id}" if turn_id is not None
            else f"{computer_id}:turn:trace:{trace_id or agent_id or 'isolated'}"
        ),
        "runtime_id": (
            f"{computer_id}:runtime:{int(row['runtime_id'])}"
            if row and row["runtime_id"] is not None else None
        ),
        "request_trace_id": trace_id or None,
    }


def _event_conversation_id(event: dict[str, Any]) -> str:
    direct = event.get("conversation_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for key in ("init", "step_update", "result"):
        payload = event.get(key)
        if isinstance(payload, dict):
            value = payload.get("conversation_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _provider_evidence(event: dict[str, Any], revision: int,
                       st: _TurnState) -> dict[str, Any] | None:
    canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    conv_id = _event_conversation_id(event) or st.conversation_id
    payload_key = hashlib.sha256("|".join([
        st.evidence_scope.get("provider_instance_id") or "",
        st.evidence_scope.get("turn_execution_id") or "",
        conv_id, digest,
    ]).encode()).hexdigest()
    dedupe_payload = _event_has_stable_replay_identity(event)
    if dedupe_payload and payload_key in st.seen_event_payloads:
        return None
    prior = st.event_hashes.get(revision)
    if prior == digest:
        return None
    if prior is not None and prior != digest:
        raise ValueError(f"conflicting AGY event at source revision {revision}")
    if revision < st.max_revision:
        return None
    st.event_hashes[revision] = digest
    if dedupe_payload:
        st.seen_event_payloads.add(payload_key)
    st.max_revision = max(st.max_revision, revision)
    ref_input = "|".join([
        st.evidence_scope.get("provider_instance_id") or "",
        str(st.evidence_scope.get("account_auth_generation") or "unknown"),
        st.evidence_scope.get("turn_execution_id") or "",
        conv_id, digest, "" if dedupe_payload else str(revision),
    ])
    return {
        **st.evidence_scope,
        "internal_schema": "agy_stream_evidence_v1",
        "conversation_id": conv_id or None,
        "source_revision": revision,
        "source_revision_kind": "local_ndjson_order",
        "provider_event_ref": "agy:" + hashlib.sha256(ref_input.encode()).hexdigest(),
        "payload_hash": digest,
        "producer": "agy-cli",
        "producer_version": None,
        "observed_at": int(time.time() * 1000),
        "authority": "provider_reported",
        "integrity": "local_subprocess_stdout",
    }


def _event_has_stable_replay_identity(event: dict[str, Any]) -> bool:
    event_type = event.get("event")
    if event_type in {"init", "result"}:
        return True
    update = event.get("step_update")
    if not isinstance(update, dict):
        return False
    # DONE is unique per provider step. Tool state is also keyed by step/state;
    # ACTIVE assistant deltas remain ordered and may legitimately repeat text.
    return update.get("state") == "DONE" or update.get("step_type") == "tool"


def _bind_session(conversation_id: str, st: _TurnState, *, trace_id: str,
                  on_session_init) -> None:
    if not conversation_id:
        raise ValueError("conversation_id missing")
    if st.expected_conversation_id and conversation_id != st.expected_conversation_id:
        raise ValueError("conversation_id does not match resumed turn")
    if st.conversation_id and conversation_id != st.conversation_id:
        raise ValueError("conversation_id changed within one turn")
    if st.conversation_id:
        return
    st.conversation_id = conversation_id
    if on_session_init is not None:
        accepted = on_session_init(conversation_id)
        if accepted is False:
            st.conversation_id = ""
            raise ValueError("conversation binding rejected")
    agent_id = st.evidence_scope.get("agent_id") or ""
    if agent_id:
        observed_assistant_count = 0
        transcript = agy_transcript.find_latest_jsonl(conversation_id)
        if transcript is not None:
            try:
                observed_assistant_count = sum(
                    turn.get("role") == "assistant"
                    for turn in agy_transcript.parse_turns(transcript))
            except OSError:
                pass
        st.baseline_snapshot = agents_db.begin_agy_assistant_turn(
            agent_id=agent_id, backend_session_id=conversation_id,
            trace_id=trace_id,
            observed_assistant_count=observed_assistant_count)
        if st.baseline_snapshot is None:
            raise ValueError("turn ownership lost before AGY binding")


def _persist_live_text(st: _TurnState, *, agent_id: str, session: str,
                       trace_id: str, stream: Any, force: bool = False) -> None:
    if not agent_id or not st.conversation_id or not st.live_text.strip():
        return
    active_trace = agents_db.active_turn_trace(agent_id)
    if trace_id and active_trace and active_trace != trace_id:
        return
    if st.live_text == st.persisted_live_text:
        return
    now = time.monotonic()
    if not force and st.last_live_write_at and (
            now - st.last_live_write_at < LIVE_TEXT_INTERVAL_SEC):
        return
    try:
        row = agents_db.upsert_live_assistant_message(
            agent_id=agent_id, backend_session_id=st.conversation_id,
            trace_id=trace_id, text=st.live_text,
        )
        st.persisted_live_text = st.live_text
        st.last_live_write_at = now
        if row and row.get("changed"):
            _broadcast_transcript(stream, agent_id, session)
    except Exception as error:  # noqa: BLE001
        log_exception("agyLivePartialFail", error, detail=trace_id or agent_id)


def _normalize_usage(value: Any) -> dict[str, int]:
    usage = value if isinstance(value, dict) else {}
    mapping = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "thinking_tokens": "thinking_tokens",
        "cache_read_tokens": "cache_read_input_tokens",
        "total_tokens": "total_tokens",
    }
    out: dict[str, int] = {}
    for source, target in mapping.items():
        raw = usage.get(source)
        if isinstance(raw, bool):
            continue
        try:
            out[target] = int(raw)
        except (TypeError, ValueError):
            continue
    return out


def _capture_step_usage(update: dict[str, Any], evidence: dict[str, Any],
                        st: _TurnState) -> None:
    usage = _normalize_usage(update.get("usage"))
    duration = update.get("duration_seconds")
    step_index = update.get("step_index")
    step_key = str(step_index) if step_index is not None else evidence["provider_event_ref"]
    if step_key in st.seen_usage_steps:
        return
    st.seen_usage_steps.add(step_key)
    if usage:
        st.usage_refs.append(evidence["provider_event_ref"])
        for key, amount in usage.items():
            st.turn_usage[key] = st.turn_usage.get(key, 0) + amount
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        st.turn_duration_seconds += max(0.0, float(duration))


def _finish_result(event: dict[str, Any], st: _TurnState, *, on_result) -> None:
    if st.terminal:
        return
    st.terminal = "result"
    st.pending_result = event


def _finish_error(message: str, st: _TurnState, *, on_error) -> None:
    if st.terminal:
        return
    st.terminal = "error"
    st.pending_error = message[:1000]


def _deliver_terminal(st: _TurnState, *, on_result, on_error) -> None:
    if st.callback_delivered or not st.terminal:
        return
    st.callback_delivered = True
    if st.terminal == "result" and on_result is not None:
        try:
            on_result(st.pending_result or {})
        except Exception as error:  # noqa: BLE001
            log_exception("agyOnResultFail", error)
    elif st.terminal == "error" and on_error is not None:
        try:
            on_error(st.pending_error)
        except Exception as error:  # noqa: BLE001
            log_exception("agyOnErrorFail", error)


_TOOL_NAMES = {
    "run_command": "Bash", "view_file": "Read", "write_to_file": "Write",
    "replace_file_content": "Edit", "grep_search": "Grep",
    "list_dir": "LS", "search_web": "WebSearch",
    "read_url_content": "WebFetch",
}


def _canonical_tool_name(value: str) -> str:
    return _TOOL_NAMES.get(value, value or "tool")


def _canonical_tool_input(name: str, value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}

    def clean(item: Any, limit: int = 180) -> str:
        text = str(item or "")[:limit]
        return _SECRET_VALUE.sub(r"\1=[redacted]", text)

    if name == "Bash":
        return {
            "command": clean(raw.get("command") or raw.get("CommandLine")),
            "description": clean(raw.get("description"), 120),
        }
    if name in {"Read", "Write", "Edit"}:
        return {"file_path": clean(
            raw.get("file_path") or raw.get("FilePath") or raw.get("path"), 240)}
    if name in {"Grep", "Glob"}:
        return {"pattern": clean(raw.get("pattern"), 100),
                "path": clean(raw.get("path"), 240)}
    if name in {"WebSearch", "WebFetch"}:
        return {"query": clean(raw.get("query"), 130),
                "url": clean(raw.get("url"), 180)}
    return {}


def _speak(text: str, st: _TurnState, *, agent_id: str, session: str,
           trace_id: str, enqueue: Callable[..., int]) -> None:
    if not agents_db.latest_turn_synthesize_audio(agent_id):
        return
    blocks = [spoken_for_tts(m.group(1).strip()) for m in _SPEAK_RE.finditer(text)]
    blocks = [b for b in blocks if b]
    if not blocks:
        return
    a = agents_db.get_by_agent_id(agent_id)
    if a is None:
        return
    persona = a.get("persona") or ""
    voice_id = a.get("voice_id") or ""
    focused = agents_db.get_focus()
    trace = trace_id or None
    for block in blocks:
        if block in st.seen_speak:
            continue
        st.seen_speak.add(block)
        for index, chunk in enumerate(spoken_chunks_for_tts(block)):
            payload_text = chunk
            if (index == 0 and persona and agent_id != focused
                    and not chunk.lower().startswith(persona.lower())):
                payload_text = f"{persona} here. {chunk}"
            try:
                qid = enqueue(agent_id=agent_id, text=payload_text,
                              voice_id=voice_id, session=session,
                              source=TurnSource.PWA,
                              trace_id=trace, synthesize_audio=True)
                log("agyEnqueue", f"agent={agent_id} qid={qid} chars={len(chunk)}")
            except Exception as e:                             # noqa: BLE001
                log_exception("agyEnqueueFail", e, detail=str(agent_id))


def _record_state(agent_id: str, kind: str, detail: dict | None = None) -> None:
    if not agent_id:
        return
    try:
        agents_db.record_state(agent_id, kind, detail)
    except Exception as e:                                 # noqa: BLE001
        log_exception("agyRecordStateFail", e, detail=f"{agent_id}:{kind}")


def _broadcast_transcript(stream: Any, agent_id: str, session: str) -> None:
    if stream is None or not agent_id:
        return
    try:
        stream.broadcast({"type": SSEType.TRANSCRIPT_UPDATED,
                          "agent_id": agent_id, "session": session})
    except Exception as e:                                 # noqa: BLE001
        log_exception("agyBroadcastFail", e, detail=agent_id)


# ---- orchestrator routing -------------------------------------------------

def routing_cmd(prompt: str, *, model: str = "", effort: str = "") -> list[str]:
    """argv for one isolated AGY request (orchestrator).

    Plain output (no stream-json) so the reply is the router's JSON, and no
    catalogue admission: the orchestrator's model pin was validated on save.
    The prompt stays bound to ``--print=`` (see ``build_cmd``).
    """
    cmd = [AGY_BIN, "--dangerously-skip-permissions"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    cmd.append(f"--print={prompt}")
    return cmd


def routing_text(stdout: str) -> str:
    """The reply text of a ``routing_cmd`` run."""
    return stdout or ""
