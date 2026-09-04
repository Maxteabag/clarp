"""Per-turn `codex exec` subprocess dispatch — the Codex backend.

This is the Codex analogue of `clarp_runner`, but it does more work,
because Codex has neither Claude Code's hook system nor an inotify
transcript-watcher driving the PWA. Everything the PWA needs is instead
read straight off `codex exec --json`'s single stdout event stream:

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
fire from the drainer thread, mirroring clarp_runner's contract so the
/send handler can treat both backends uniformly.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import agents as agents_db
from . import settings_store
from . import tts_queue
from .config import persona_personality
from .personalities import KEY_ENABLED as PERSONALITIES_ENABLED_KEY
from .voice_markup import (  # noqa: F401 — spoken_for_tts remains re-exported
    spoken_chunks_for_tts,
    spoken_for_tts,
)
from .log import log, log_exception
from .proc_util import attach_stderr_drain, stderr_text
from .process_registry import ProcessRegistry, TurnHandle
from .protocol import AgentState, SSEType, TurnSource


CODEX_BIN = "codex"  # resolved from PATH; tests can monkeypatch.
LIVE_TEXT_INTERVAL_SEC = 0.25

# Same voice-gating convention as the Claude path: only text the agent
# wraps in <speak>…</speak> is spoken; everything else is silent.
_SPEAK_RE = re.compile(r"<speak>(.*?)</speak>", re.DOTALL | re.IGNORECASE)

# Codex (unlike Claude) never receives the PWA's system reminders — Claude
# gets those from the UserPromptSubmit hook, which Codex has no equivalent of.
# So for PWA/native turns we prepend the instructions to the prompt ourselves.
# The head + split sentinels let the history parser strip them back off so the
# user's message renders cleanly.
_VOICE_PREAMBLE_HEAD = "[voice-mode]"
_VOICE_PREAMBLE_SPLIT = "\n\n--- user message ---\n"

# Always-on for app-dispatched turns (every codex/agy turn is one): the
# PWA/iOS client renders plain text but CANNOT show interactive prompts, so
# the agent must ask in prose rather than via a question/choice tool.
_NO_INTERACTIVE_QUESTIONS = (
    "You are connected through a phone/voice app. It shows your text replies "
    "but CANNOT display interactive prompts — no question tools, no "
    "multiple-choice pickers, no approval dialogs. If you need to ask the "
    "user something or offer choices, just write it as plain text and wait "
    "for their next message. Never call a tool whose purpose is to ask the "
    "user a question or request a choice; it won't render and they can't "
    "answer it."
)

# Added only for spoken turns: how the <speak> voice gating works.
_VOICE_INSTRUCTION = (
    "This reply is read aloud over text-to-speech: treat the spoken "
    "<speak>...</speak> blocks like a phone call and the surrounding text like "
    "the screen. Your VERY FIRST output — before any tool call, file read, or "
    "silent thinking — must be a one-line <speak> acknowledgment (e.g. "
    "<speak>On it — checking now.</speak>), because the user is hands-free and "
    "hears only silence until you speak. "
    "After that opening acknowledgment, STAY SILENT while you work: do NOT "
    "narrate routine steps, progress, or each tool call. Most intermediate "
    "steps should carry NO <speak> block at all. Only break the silence "
    "mid-task when the user genuinely needs to hear it right then — a blocker, "
    "an error, a decision that needs their input, or a question you must ask "
    "before continuing. "
    "When you finish, give ONE spoken final summary: say the outcome and "
    "whatever they'd actually want in their ear, judged for listening — "
    "selective, but not a vague headline. Leave out detail that only makes "
    "sense on screen. "
    "Put code, paths, commands, logs, tables, long lists, and detailed "
    "evidence OUTSIDE the tags (shown but not spoken). Say it once: don't "
    "restate your spoken text in the written part."
)

# Added for spoken turns: make the delivery sound human. Fillers are wrapped in
# <vox>…</vox> so they're SPOKEN but stripped from the on-screen text; <break>
# is honoured by the TTS engine and likewise hidden from display.
_NATURAL_SPEECH = (
    "Every spoken response should sound conversational, including confident "
    "and simple answers. Use brief pauses such as <break time=\"350ms\"/> and "
    "occasional fillers naturally throughout; do not reserve them for "
    "uncertainty. Wrap EVERY filler in <vox>…</vox> so it is spoken yet never "
    "shown on screen, e.g. "
    "<vox>um</vox>, <vox>uh</vox>, <vox>hmm</vox>, <vox>like</vox>, "
    "<vox>you know</vox>. Keep very short acknowledgments concise, but give "
    "substantive spoken replies at least one natural conversational cue. Keep it tasteful "
    "— a couple of pauses or fillers, never a stutter-fest; breaks around "
    "300–450ms; spell fillers plainly (um/uh/hmm), never stretched out. These "
    "cues live ONLY inside <speak>; the <vox> wraps and the tags are stripped "
    "from the visible text automatically."
)

# Voice-markup normalization (display strip + TTS unwrap) lives in one place:
# lib.voice_markup. spoken_for_tts is imported above and re-exported so existing
# callers (agy_runner, transcript_streamer) keep importing it from here.


def persona_identity_instruction(persona: str, session: str = "") -> str:
    persona = (persona or "").strip()
    session = (session or "").strip()
    if not persona:
        return ""
    session_hint = f" The app session id for this agent is `{session}`." if session else ""
    identity = (
        f"You are the assistant persona named {persona}.{session_hint} "
        f"When the user addresses {persona}, they are addressing you. "
        f"Do not say that you are Claude, Codex, Gemini, or another base model "
        f"instead of {persona}; keep model/vendor identity secondary unless the "
        f"user explicitly asks about the underlying model."
    )
    custom_personality = ""
    if session:
        try:
            from . import agents as agents_db
            custom_personality = str(
                (agents_db.get_by_session(session) or {}).get("personality") or ""
            ).strip()
        except Exception:
            pass
    personality = (
        custom_personality or persona_personality(persona)
        if settings_store.get_bool(PERSONALITIES_ENABLED_KEY, default=True)
        else ""
    )
    if personality:
        identity = f"{identity} {personality}"
    if session:
        identity = (
            f"{identity} Use the installed `clarp-background-jobs` skill for "
            f"work that continues after your final response. Never set a visible "
            f"status for foreground analysis, tool calls, or builds you are "
            f"awaiting in the current turn. Detached statuses must be 2-3 words "
            f"and under 20 characters."
        )
    return identity


def _preamble(*, voice: bool, identity: str = "") -> str:
    body = app_turn_instructions(voice=voice)
    if identity:
        body = f"{body}\n\n{identity}"
    return f"{_VOICE_PREAMBLE_HEAD} {body}{_VOICE_PREAMBLE_SPLIT}"


def app_turn_instructions(*, voice: bool) -> str:
    """Application constraints for a turn, without user-message wrappers.

    Persistent transports place this in app-server ``additionalContext`` so it
    stays model-visible without polluting the user's transcript message.
    """
    body = _NO_INTERACTIVE_QUESTIONS
    if voice:
        body = f"{body}\n\n{_VOICE_INSTRUCTION}\n\n{_NATURAL_SPEECH}"
    return body


def apply_voice_preamble(text: str, *, voice: bool = True,
                         persona: str = "", session: str = "") -> str:
    """Prepend the app-turn instruction block to a prompt.

    The no-interactive-questions rule is always included (the app can't show
    question UIs); the <speak> voice guidance is added only when `voice` is
    True (a spoken turn). `voice` defaults True so existing callers keep the
    full block."""
    identity = persona_identity_instruction(persona, session)
    if identity:
        return _preamble(voice=voice, identity=identity) + text
    return _preamble(voice=voice) + text


def strip_voice_preamble(text: str) -> str:
    """Inverse of apply_voice_preamble — recover the user's original message
    for the history pane. A no-op if the preamble isn't present."""
    if isinstance(text, str) and text.startswith(_VOICE_PREAMBLE_HEAD):
        i = text.find(_VOICE_PREAMBLE_SPLIT)
        if i != -1:
            return text[i + len(_VOICE_PREAMBLE_SPLIT):]
    return text

# Live registry: agent_id → list of running TurnHandle objects, so /stop
# can interrupt in-flight turns. Mirrors clarp_runner._ACTIVE.
_REGISTRY = ProcessRegistry(log_exception=log_exception)


def active_handles(agent_id: str) -> list["TurnHandle"]:
    return _REGISTRY.active_handles(agent_id)


def interrupt(agent_id: str) -> int:
    """SIGTERM every in-flight codex turn for an agent. Idempotent."""
    return _REGISTRY.interrupt(agent_id, event="codexInterruptFail")


def _register(agent_id: str, h: "TurnHandle") -> None:
    _REGISTRY.register(agent_id, h)


def _unregister(agent_id: str, h: "TurnHandle") -> None:
    _REGISTRY.unregister(agent_id, h)


def build_cmd(session_id: str = "", *, is_new_session: bool = False,
              model: str = "", reasoning_effort: str = "",
              isolated: bool = False) -> list[str]:
    """argv for one `codex exec` turn (prompt appended by the caller).

    Fresh turn   →  codex exec --json --dangerously-bypass-approvals-and-sandbox
    Known session→  …same flags… resume <uuid>

    Unlike Claude (`--session-id <uuid>` lets us pre-pick the id), Codex
    assigns the conversation UUID itself and reports it in the
    `session_meta` event — so a fresh turn carries no id and we bind
    whatever Codex hands back. `is_new_session` is accepted for a uniform
    signature with clarp_runner.build_cmd but only `session_id` decides
    resume vs fresh.

    `model` / `reasoning_effort` are optional latency knobs (config
    [agents]). Empty → Codex defaults. Lowering reasoning effort is the
    biggest lever on time-to-first-word for hands-free voice turns.
    """
    base = [CODEX_BIN, "exec", "--json"]
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


@dataclass
class _TurnState:
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
) -> TurnHandle:
    """Spawn `codex exec` for ONE turn and drive progress off its stdout.

    Signature is a superset of clarp_runner.spawn_turn: the extra
    `stream` (SSE broadcaster) and `enqueue` (TTS sink, defaults to
    tts_queue.enqueue) let the Codex backend reproduce the state/TTS/
    history side-effects that Claude gets from hooks + the transcript
    watcher.

    Raises FileNotFoundError if `codex` isn't on PATH.
    """
    if shutil.which(CODEX_BIN) is None:
        raise FileNotFoundError(
            f"`{CODEX_BIN}` not on PATH — install the Codex CLI "
            f"(npm i -g @openai/codex) to run Codex-backed agents"
        )
    # Defensive: never hand Popen a literal "~" — the OS won't expand it and
    # the spawn fails with FileNotFoundError. Agents created with the default
    # working dir store the bare tilde.
    cwd = pathlib.Path(os.path.expanduser(str(cwd)))
    cmd = build_cmd(backend_session_id, is_new_session=is_new_session,
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
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={**os.environ, "CLAUDE_PWA_SESSION": session},
    )
    attach_stderr_drain(proc)
    handle = TurnHandle(proc=proc, drain_thread=None)   # type: ignore[arg-type]
    runtime_agent_id = "" if isolated else agent_id
    if runtime_agent_id:
        _register(runtime_agent_id, handle)
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
        daemon=True,
        name=f"codex-drain-{proc.pid}",
    )
    handle.drain_thread = drain
    drain.start()
    return handle


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


# ---- the drainer -------------------------------------------------------

def _drain_stdout(
    *,
    proc: subprocess.Popen,
    agent_id: str,
    session: str,
    trace_id: str,
    backend_session_id: str,
    handle: "TurnHandle",
    on_session_init: Optional[Callable[[str], None]],
    on_result: Optional[Callable[[dict], None]],
    on_error: Optional[Callable[[str], None]],
    stream: Any,
    enqueue: Callable[..., int],
) -> None:
    """Background thread: parse codex stream-json and fire side-effects.

    Swallows every exception per-line — a drainer crash must never take
    down a server thread."""
    st = _TurnState(live_backend_session_id=backend_session_id)
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
                _handle_event(ev, st, agent_id=agent_id,
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
            _unregister(agent_id, handle)


def _handle_event(
    ev: dict, st: _TurnState, *,
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
        _record_state(agent_id, AgentState.THINKING,
                      {"dispatch": "codex", "trace_id": trace_id})
        return
    if etype in ("item.started", "item.updated", "item.completed"):
        item = ev.get("item")
        if isinstance(item, dict):
            _handle_item(etype, item, st, agent_id=agent_id,
                         session=session, trace_id=trace_id,
                         stream=stream, enqueue=enqueue)
        return
    if etype == "turn.completed":
        usage = ev.get("usage")
        if isinstance(usage, dict):
            st.tokens_in = int(usage.get("input_tokens") or st.tokens_in or 0)
            st.tokens_out = int(usage.get("output_tokens") or st.tokens_out or 0)
        _persist_live_text(
            st, agent_id=agent_id, session=session, trace_id=trace_id,
            stream=stream, force=True)
        _broadcast_transcript(stream, agent_id, session)
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
        _record_state(agent_id, AgentState.THINKING,
                      {"dispatch": "codex", "trace_id": trace_id})
        return

    if inner in ("function_call", "custom_tool_call", "web_search_call",
                 "mcp_tool_call_begin", "exec_command_begin"):
        name = str(payload.get("name") or payload.get("tool") or "tool")
        _record_state(agent_id, AgentState.TOOL,
                      {"dispatch": "codex", "tool": name, "trace_id": trace_id})
        _broadcast_transcript(stream, agent_id, session)
        return

    if inner in ("function_call_output", "custom_tool_call_output",
                 "patch_apply_end", "exec_command_end", "web_search_end"):
        # A tool finished — refresh the history pane; state goes back to
        # THINKING until the next agent_message / tool / task_complete.
        _broadcast_transcript(stream, agent_id, session)
        return

    if inner == "context_compacted":
        _record_state(agent_id, AgentState.COMPACTING,
                      {"dispatch": "codex", "trace_id": trace_id})
        return

    if inner == "turn_aborted":
        _record_state(agent_id, AgentState.IDLE,
                      {"dispatch": "codex", "aborted": True,
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
        _persist_live_text(
            st, text=text, agent_id=agent_id, session=session,
            trace_id=trace_id, stream=stream)
        _speak(text, st, agent_id=agent_id, session=session,
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
            _speak(msg, st, agent_id=agent_id, session=session,
                   trace_id=trace_id, enqueue=enqueue)
        _persist_live_text(
            st, agent_id=agent_id, session=session, trace_id=trace_id,
            stream=stream, force=True)
        _broadcast_transcript(stream, agent_id, session)
        return


# Item types codex emits inside item.* events that represent the agent
# doing work (vs. speaking). Any of these flips the agent to TOOL.
_TOOL_ITEM_TYPES = {
    "command_execution", "function_call", "local_shell_call", "tool_call",
    "file_change", "patch_apply", "mcp_tool_call", "dynamic_tool_call",
    "collab_tool_call", "collab_agent_tool_call", "web_search",
    "web_search_call", "image_view", "image_generation",
}


def _handle_item(etype: str, item: dict, st: _TurnState, *,
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
        _persist_live_text(
            st, text=text, agent_id=agent_id, session=session,
            trace_id=trace_id, stream=stream,
            force=etype == "item.completed")
        # Only speak the completed block — item.updated may carry partials.
        if etype == "item.completed":
            _speak(text, st, agent_id=agent_id, session=session,
                   trace_id=trace_id, enqueue=enqueue)
        return

    if itype == "reasoning":
        # Model thinking — keep the agent in a busy state, but nothing to
        # speak or render.
        _record_state(agent_id, AgentState.THINKING,
                      {"dispatch": "codex", "trace_id": trace_id})
        return

    if itype in _TOOL_ITEM_TYPES:
        name = str(item.get("command") or item.get("name") or itype)
        _record_state(agent_id, AgentState.TOOL,
                      {"dispatch": "codex", "tool": name[:80],
                       "trace_id": trace_id})
        _broadcast_transcript(stream, agent_id, session)
        return

    # Unknown item kind — just refresh the history pane.
    _broadcast_transcript(stream, agent_id, session)


def _persist_live_text(
    st: _TurnState,
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
    if not agent_id or not st.pending_live_text:
        return
    if st.pending_live_text == st.persisted_live_text:
        return
    now = time.monotonic()
    if not force and st.last_live_write_at > 0:
        if now - st.last_live_write_at < LIVE_TEXT_INTERVAL_SEC:
            return
    backend_session_id = (
        st.live_backend_session_id
        or agents_db.live_backend_session(agent_id)
    )
    if not backend_session_id:
        return
    try:
        row = agents_db.upsert_live_assistant_message(
            agent_id=agent_id,
            backend_session_id=backend_session_id,
            trace_id=trace_id,
            text=st.pending_live_text,
        )
        st.last_live_write_at = now
        st.persisted_live_text = st.pending_live_text
        if row and row.get("changed"):
            _broadcast_transcript(stream, agent_id, session)
    except Exception as e:  # noqa: BLE001
        log_exception("codexLivePartialFail", e, detail=trace_id or agent_id)


def _speak(text: str, st: _TurnState, *, agent_id: str, session: str,
           trace_id: str, enqueue: Callable[..., int]) -> None:
    """Extract <speak>…</speak> regions and enqueue each as a TTS clip.

    De-duplicates against blocks already spoken this turn (the same region
    can appear in a streamed agent_message and again in task_complete's
    last_agent_message)."""
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
    trace = agents_db.get_trace(agent_id) or trace_id or None
    for block in blocks:
        key = block.strip()
        if key in st.seen_speak:
            continue
        st.seen_speak.add(key)
        for index, chunk in enumerate(spoken_chunks_for_tts(block)):
            payload_text = chunk
            # Persona prefix only the first chunk of each explicit speak block.
            if (index == 0 and persona and agent_id != focused
                    and not chunk.lower().startswith(persona.lower())):
                payload_text = f"{persona} here. {chunk}"
            try:
                qid = enqueue(
                    agent_id=agent_id,
                    text=payload_text,
                    voice_id=voice_id,
                    session=session,
                    source=TurnSource.PWA,
                    trace_id=trace,
                    synthesize_audio=True,
                )
                st.spoke_any = True
                log("codexEnqueue", f"agent={agent_id} qid={qid} chars={len(chunk)}")
            except Exception as e:                 # noqa: BLE001
                log_exception("codexEnqueueFail", e, detail=str(agent_id))


def _record_state(agent_id: str, kind: str, detail: dict | None = None) -> None:
    if not agent_id:
        return
    try:
        agents_db.record_state(agent_id, kind, detail)
    except Exception as e:                     # noqa: BLE001
        log_exception("codexRecordStateFail", e, detail=f"{agent_id}:{kind}")


def _broadcast_transcript(stream: Any, agent_id: str, session: str) -> None:
    if stream is None or not agent_id:
        return
    try:
        stream.broadcast({
            "type": SSEType.TRANSCRIPT_UPDATED,
            "agent_id": agent_id,
            "session": session,
        })
    except Exception as e:                     # noqa: BLE001
        log_exception("codexBroadcastFail", e, detail=agent_id)


# ---- orchestrator routing -------------------------------------------------

def routing_cmd(prompt: str, *, model: str = "", effort: str = "") -> list[str]:
    """argv for one ephemeral ``codex exec --json`` request (orchestrator)."""
    cmd = build_cmd(model=model, reasoning_effort=effort, isolated=True)
    cmd.append(prompt)
    return cmd


def routing_text(stdout: str) -> str:
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
