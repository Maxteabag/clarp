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
    from lib.transcript_cursor import CursorStoreError, reset_spoken_first_all  # noqa: E402
    from lib.hook_runtime import app_session  # noqa: E402
    from lib.paths import RuntimePaths                # noqa: E402
    from lib.protocol import AgentState, TurnSource   # noqa: E402
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
    # signal — used below to inject the no-interactive-questions rule.
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
        if backend_session_id:
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
        trace_id = (
            marker_info.trace_id
            or _agents.get_trace(agent_id)
            or f"{source}-{int(time.time()*1000):x}"
        )
        _agents.set_trace(agent_id, trace_id)
        _agents.open_turn(agent_id=agent_id,
                          source=source,
                          trace_id=trace_id,
                          synthesize_audio=marker_info.synthesize_audio)
        _agents.record_state(agent_id, AgentState.THINKING,
                             {"source": source,
                              "backend_session_id": backend_session_id})
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
    #   * no-interactive-questions — always, for any app-dispatched turn (the
    #     PWA/iOS client renders text but can't show question/choice tools).
    #   * <speak> voice gating — only on spoken turns.
    context = _build_additional_context(app_dispatched=bool(env_session),
                                        voiced=voiced)
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


def _build_additional_context(*, app_dispatched: bool, voiced: bool) -> str:
    """Compose the UserPromptSubmit additionalContext for this turn.

    The no-interactive-questions rule is included for every app-dispatched
    turn; the <speak> voice guidance is appended only on spoken turns.
    Returns "" when neither applies (e.g. a third-party local terminal)."""
    parts = []
    if app_dispatched:
        parts.append(_NO_INTERACTIVE_QUESTIONS)
    if voiced:
        parts.append(_SPEAK_INSTRUCTIONS)
    return "\n\n".join(parts)


_NO_INTERACTIVE_QUESTIONS = """\
You are connected through a phone/voice app. It shows your text replies but
CANNOT display interactive prompts — no question tools, no multiple-choice
pickers, no permission/approval dialogs. If you need to ask the user a question
or offer choices, just write it as plain text in your reply and wait for their
next message. Never call a tool whose purpose is to ask the user a question or
request a choice (e.g. AskUserQuestion) — it will not render and the user has
no way to answer it.\
"""


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
- Every spoken response should sound conversational, including confident and
  simple answers. Use brief pauses such as <break time="350ms"/> and occasional
  fillers naturally throughout; do not reserve them for uncertainty. Wrap EVERY
  filler in <vox>...</vox> so it is spoken yet never shown on screen, e.g.
  <vox>um</vox>, <vox>uh</vox>, <vox>hmm</vox>, <vox>like</vox>,
  <vox>you know</vox>. Keep very short acknowledgments concise, but give
  substantive spoken replies at least one natural conversational cue.
- Use <speed ratio="0.85"/> to slow slightly while reasoning and
  <speed ratio="1.1"/> once you're sure (ratio 0.6-1.5).
- Keep it tasteful — a couple of pauses or fillers, never a stutter-fest;
  breaks ~300-450ms; spell fillers plainly (um/uh/hmm), never stretched. The
  <vox> wraps and the tags are stripped from the visible text automatically.
"""


if __name__ == "__main__":
    sys.exit(main())
