#!/usr/bin/env python3
"""UserPromptSubmit hook.

For each prompt the user submits to a Claude Code session:
  1. Resolve which PWA agent fired the hook (by `backend_session_id` from the
     payload first, falling back to the injected app session).
  2. Stamp the live runtime row with the backend_session_id UUID so future
     hook fires can resolve via Claude UUID alone.
  3. Open a `turn` row tagged 'pwa' (if a fresh PWA-voice marker exists)
     or 'local' (otherwise).
  4. Record state='thinking' so /status / /agent-status reflect reality
     without scraping a terminal process.
  5. Reset the per-session "spoken first chunk" flag so the next assistant
     text gets voiced.

Third-party Claude Code instances (not registered in the DB) get a no-op.
"""
import json, pathlib, sys, time
from dataclasses import dataclass

import _clarp_lib  # noqa: F401  — puts Clarp's `lib` on sys.path
try:
    from lib import agents as _agents                # noqa: E402
    from lib import turn_lifecycle                   # noqa: E402
    from lib.transcript_cursor import CursorStoreError, reset_spoken_first_all  # noqa: E402
    from lib.hook_runtime import app_session  # noqa: E402
    from lib.paths import RuntimePaths                # noqa: E402
    from lib.protocol import TurnSource               # noqa: E402
    from lib.timing import HOOK_TIMING                # noqa: E402
except ImportError:
    # claude-pwa not installed on this machine — hook is a no-op.
    sys.exit(0)
try:
    from lib.eventlog import emit as _emit_event  # noqa: E402
except ImportError:
    def _emit_event(*a, **kw): pass

PATHS = RuntimePaths.from_home(pathlib.Path.home())


@dataclass(frozen=True)
class PwaMarker:
    fresh: bool
    trace_id: str = ""
    synthesize_audio: bool = True


def _read_pwa_marker(marker: pathlib.Path, session: str) -> PwaMarker:
    try:
        if not marker.is_file():
            return PwaMarker(False)
        parts = marker.read_text().strip().split()
        if len(parts) < 3 or parts[0] != TurnSource.PWA_VOICE_MARKER:
            return PwaMarker(False)
        if parts[1] != session:
            return PwaMarker(False)
        ts = float(parts[2])
        fresh = time.time() - ts <= HOOK_TIMING.pwa_source_fresh_window_sec
        return PwaMarker(
            fresh,
            parts[3] if fresh and len(parts) >= 4 else "",
            parts[4] != "0" if fresh and len(parts) >= 5 else True,
        )
    except (OSError, ValueError):
        return PwaMarker(False)


def _fresh_pwa_marker(marker: pathlib.Path, session: str) -> bool:
    return _read_pwa_marker(marker, session).fresh


def main() -> int:
    payload = {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        pass

    backend_session_id = (payload.get("session_id") or "").strip()
    # CLAUDE_PWA_SESSION is set only by the server's clarp dispatcher, so a
    # non-empty value is the authoritative "this turn came through the app"
    # signal — used below to inject the Clarp-skills guidance.
    env_session = app_session()
    session   = env_session

    # When clarp -p dispatches a turn from the server, the child claude
    # receives CLAUDE_PWA_SESSION as its authoritative app identity.
    # If we don't have it but DO have a
    # backend_session_id that's already bound to an agent's runtime,
    # reverse-lookup.
    if not session and backend_session_id:
        try:
            a = _agents.get_by_backend_session(backend_session_id)
            if a and a.get("session"):
                session = a["session"]
        except Exception:
            pass

    # Fresh PWA-voice marker → this turn arrived via the PWA, not a local
    # terminal. Drives the Stop hook's decision to write into PWA audio.
    pwa_fresh = False
    marker = PATHS.source_marker(session)
    marker_info = _read_pwa_marker(marker, session)
    pwa_fresh = marker_info.fresh

    try:
        _emit_event(
            "userprompt_hook", "promptSubmit",
            session=session or None,
            backend_session_id=backend_session_id or None,
            detail={"from_pwa_marker": pwa_fresh},
        )
    except Exception:
        pass

    # Resolve the agent. Third-party Claude Code (not in DB) → no-op.
    try:
        agent = _agents.resolve_for_hook(
            backend_session_id=backend_session_id or None,
            session=session or None,
        )
    except Exception:
        agent = None
    if not agent:
        return 0

    agent_id = agent["agent_id"]
    try:
        # Stamp the live runtime row with the backend_session_id UUID. Idempotent.
        # Only Claude agents converse in a Claude session. A Codex or AGY
        # agent reaches this hook through a Claude fallback model, whose UUID
        # must never replace the agent's own thread.
        claude_agent = (agent.get("backend") or "claude") == "claude"
        if backend_session_id and claude_agent:
            try:
                _agents.bind_backend_session(agent_id, backend_session_id)
            except _agents.SessionAlreadyBound as bind_err:
                # Another agent already owns this UUID — refuse to
                # cross-bind. The hook just bails for this turn rather
                # than silently appending to someone else's transcript.
                try: _emit_event("userprompt_hook", "sessionConflict",
                                 session=session or None,
                                 detail={"err": str(bind_err)})
                except Exception: pass
                return 0
        # Open a turn row. trace_id is "pwa-…" or "local-…" until the
        # /transcribe handler attaches one for PWA-voice turns.
        source = TurnSource.PWA if pwa_fresh else TurnSource.LOCAL
        # Inside a process Clarp started, the dispatcher owns the trace: a
        # prompt that arrives mid-turn (a background-task notification) is
        # part of that turn however old its trace is. Minting a new one here
        # made the runtime take its own running turn for superseded.
        current = _agents.get_trace(
            agent_id, max_age_ms=None if env_session else _agents.TRACE_TTL_MS)
        trace_id = (
            marker_info.trace_id
            or current
            or f"{source}-{int(time.time()*1000):x}"
        )
        if marker_info.trace_id or not (env_session and current):
            if env_session and not marker_info.trace_id:
                try: _emit_event("userprompt_hook", "traceMinted",
                                 session=session or None,
                                 detail={"trace_id": trace_id,
                                         "reason": "no trace for a Clarp-started turn"})
                except Exception: pass
            _agents.set_trace(agent_id, trace_id)
        turn_lifecycle.open_turn(agent_id=agent_id,
                                 source=source,
                                 trace_id=trace_id,
                                 synthesize_audio=marker_info.synthesize_audio)
        turn_lifecycle.hook_transition(
            agent_id, turn_lifecycle.TurnEvent.PROMPT_ADMITTED,
            {"source": source, "backend_session_id": backend_session_id})
    except Exception as e:
        try: _emit_event("userprompt_hook", "dbStateFail",
                         session=session or None,
                         detail={"err": str(e)})
        except Exception: pass

    # Eat the pwa-voice marker + reset "spoken first chunk" flags (voice turns
    # only). `voiced` gates the <speak> guidance below.
    voiced = False
    if pwa_fresh:
        try: marker.unlink()
        except OSError: pass
        if marker_info.synthesize_audio:
            voiced = True
            try:
                reset_spoken_first_all()
            except CursorStoreError as e:
                try: _emit_event("userprompt_hook", "cursorResetFail",
                                 session=session or None,
                                 detail={"err": str(e)})
                except Exception: pass
        # Per-paragraph streaming used to be coaxed out of Claude by
        # injecting a system-prompt directive that asked it to emit a
        # `: __pwa_break__` Bash call between paragraphs. clarp's native
        # `--include-partial-messages` gives us real per-chunk deltas
        # straight from the API stream, so the hack is gone.

    # Inject this app's constraints via the official UserPromptSubmit
    # additionalContext mechanism, as one combined block:
    #   * Clarp-skills guidance — always, for any app-dispatched turn.
    #   * <speak> voice gating — only on spoken turns.
    context = _build_additional_context(app_dispatched=bool(env_session),
                                        voiced=voiced, agent=agent)
    if context:
        try:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }))
        except Exception:
            pass
    return 0


def _build_additional_context(*, app_dispatched: bool, voiced: bool,
                              agent: dict | None = None) -> str:
    """Compose the UserPromptSubmit additionalContext for this turn.

    The Clarp-skills guidance is included for every app-dispatched turn; the <speak> voice guidance is appended only on spoken turns.
    Returns "" when neither applies (e.g. a third-party local terminal)."""
    parts = []
    guidance = _clarp_guidance() if app_dispatched else ""
    if guidance:
        parts.append(guidance)
    identity = _identity(agent) if app_dispatched else ""
    if identity:
        parts.append(identity)
    if voiced:
        parts.append(_SPEAK_INSTRUCTIONS + _natural_delivery(agent))
    header = _group_call_header(agent) if app_dispatched else ""
    if header:
        parts.append(header)
    return "\n\n".join(parts)


def _identity(agent: dict | None) -> str:
    """Who this turn belongs to (lib.clarp_guidance.identity_line), or ""."""
    agent = agent or {}
    try:
        from lib.clarp_guidance import identity_line
    except ImportError:
        return ""
    return identity_line(str(agent.get("persona") or ""), str(agent.get("session") or ""))


def _group_call_header(agent: dict | None) -> str:
    """The agent's group-call header (lib.group_calls), or ""."""
    session = str((agent or {}).get("session") or "")
    if not session:
        return ""
    try:
        from lib import group_calls
        return group_calls.prompt_header(session)
    except Exception:
        return ""


def _natural_delivery(agent: dict | None) -> str:
    """Natural-delivery guidance at this agent's humanness level (0-10)."""
    try:
        from lib import voice_humanness
    except ImportError:
        return ""
    try:
        return voice_humanness.guidance_for(agent or {})
    except Exception:
        return voice_humanness.guidance(voice_humanness.DEFAULT_LEVEL)


def _clarp_guidance() -> str:
    """The shared app-turn guidance (lib.clarp_guidance), one source for all providers."""
    try:
        from lib.clarp_guidance import CLARP_SKILLS_GUIDANCE
    except ImportError:
        return ""
    return CLARP_SKILLS_GUIDANCE


_SPEAK_INSTRUCTIONS = """\
This turn arrived via the user's phone over voice. The voice channel
is gated: only text wrapped in <speak>...</speak> tags is read aloud
through ElevenLabs. Everything else is silent (still visible in the
PWA's conversation history, but not spoken).

Guidelines:
- Match the spoken/written split to the reply's size — do NOT always write two
  versions:
  * SHORT, simple replies (a sentence or two — casual chat, a quick answer):
    put the ENTIRE reply inside one <speak>...</speak> block and write nothing
    after it, so what's heard and what's shown are identical. Do NOT author a
    second, differently-worded written version of a short reply.
  * LONG or complex replies (a real deep-dive — lists, code, multiple points,
    detail): keep the <speak> a short compressed gist (the headline + the one
    thing they most need in their ear, a sentence or two) and put the full
    detail AFTER the </speak>. The screen carries the rest.
- Your VERY FIRST output — before ANY tool call, file read, or silent
  thinking — MUST be a one-line <speak> acknowledgment (e.g.
  <speak>On it — checking now.</speak>). The user is hands-free and
  hears nothing but silence until you speak, so NEVER run a tool before
  that spoken line lands. Acknowledge out loud first, then do the work.
- Do NOT wrap intermediate narration like "Now let me read the file"
  in <speak> tags — that's exactly the chatter the gating exists to
  silence. Leave it as plain text.
- Code blocks, file paths, commands, logs, tables, long lists, detailed
  evidence, and anything visual belongs outside <speak> tags.
- Decisive test before you write ANY text after </speak>: would that text carry
  materially MORE than the spoken line — code, a list, file paths, specifics,
  real detail you'd skip aloud? If it would just restate the spoken gist at
  similar length and content, DON'T write it — put the whole reply in the single
  <speak> block. Two near-duplicate halves (a spoken sentence and a written
  sentence saying the same thing) is the exact failure to avoid.

Natural delivery (inside <speak> only):
"""


if __name__ == "__main__":
    sys.exit(main())
