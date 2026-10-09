"""App-turn prompt preamble shared by the CLI-backed runners.

Codex, AGY, Grok and OpenCode never receive the PWA's system reminders
(Claude gets those from the UserPromptSubmit hook), so for app-dispatched
turns the runners prepend these instructions to the prompt themselves. The
transcript parsers strip the block back off with ``strip_voice_preamble``.
"""
from __future__ import annotations

from . import settings_store
from .clarp_guidance import CLARP_SKILLS_GUIDANCE
from .config import persona_personality
from .personalities import KEY_ENABLED as PERSONALITIES_ENABLED_KEY


# Codex (unlike Claude) never receives the PWA's system reminders — Claude
# gets those from the UserPromptSubmit hook, which Codex has no equivalent of.
# So for PWA/native turns we prepend the instructions to the prompt ourselves.
# The head + split sentinels let the history parser strip them back off so the
# user's message renders cleanly.
_VOICE_PREAMBLE_HEAD = "[voice-mode]"
_VOICE_PREAMBLE_SPLIT = "\n\n--- user message ---\n"

# Always-on for app-dispatched turns (lib.clarp_guidance).
_APP_TURN_GUIDANCE = CLARP_SKILLS_GUIDANCE

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
    "When that summary runs long, split it across consecutive <speak> blocks "
    "of about a minute each rather than one long one: each block is "
    "synthesized as its own clip, so the first starts playing while the rest "
    "is still being written, and no single clip is long enough to be cut off "
    "at a provider limit. "
    "Put code, paths, commands, logs, tables, long lists, and detailed "
    "evidence OUTSIDE the tags (shown but not spoken). Say it once: don't "
    "restate your spoken text in the written part."
)

# The natural-delivery part of a spoken turn (fillers in <vox>, pauses, and
# emotion tags for Gemini voices) depends on the agent's humanness level; see
# lib.voice_humanness.

# Voice-markup normalization (display strip + TTS unwrap) lives in one place:
# lib.voice_markup. spoken_for_tts is imported above and re-exported so existing
# callers (the AGY backend, transcript_streamer) keep importing it from here.


def persona_identity_instruction(persona: str, session: str = "") -> str:
    """The persona's name plus its personality text; nothing else.

    Session ids reach hooks and skills through CLAUDE_PWA_SESSION, and installed
    skills advertise themselves through their own descriptions, so neither
    belongs in the prompt.
    """
    persona = (persona or "").strip()
    session = (session or "").strip()
    if not persona:
        return ""
    identity = f"You are {persona}."
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
    return identity


def _preamble(*, voice: bool, identity: str = "", session: str = "") -> str:
    body = app_turn_instructions(voice=voice, session=session)
    if identity:
        body = f"{body}\n\n{identity}"
    return f"{_VOICE_PREAMBLE_HEAD} {body}{_VOICE_PREAMBLE_SPLIT}"


def app_turn_instructions(*, voice: bool, session: str = "") -> str:
    """Application constraints for a turn, without user-message wrappers.

    Persistent transports place this in app-server ``additionalContext`` so it
    stays model-visible without polluting the user's transcript message.

    `session` selects that agent's own narration level; omitting it keeps the
    quiet default, so a caller with no session in hand is unchanged.
    """
    body = _APP_TURN_GUIDANCE
    if voice:
        body = f"{body}\n\n{voice_instructions(session)}"
    header = _group_call_header(session)
    if header:
        body = f"{body}\n\n{header}"
    return body


def _group_call_header(session: str) -> str:
    """Who else is in the agent's group call and what they said (lib.group_calls)."""
    if not session:
        return ""
    from . import group_calls
    return group_calls.prompt_header(session)


def voice_instructions(session: str = "") -> str:
    """Only the spoken-turn part, for a turn that already has the rest."""
    body = f"{_VOICE_INSTRUCTION}\n\n{_natural_speech(session)}"
    narration = _narration_clause(session)
    if narration:
        body = f"{body}\n\n{narration}"
    return body


def _natural_speech(session: str) -> str:
    """Natural-delivery guidance at the agent's humanness level."""
    from . import voice_humanness
    try:
        return voice_humanness.guidance_for(session=session or None)
    except Exception:  # noqa: BLE001 - a prompt tweak must never fail a turn
        return voice_humanness.guidance(voice_humanness.DEFAULT_LEVEL)


def _narration_clause(session: str) -> str:
    """The agent's own how-much-to-narrate instruction, or "" when quiet."""
    if not session:
        return ""
    try:
        from . import agents as agents_db, voice_verbosity

        level = (agents_db.get_by_session(session) or {}).get("voice_verbosity")
        return voice_verbosity.narration_clause(level)
    except Exception:  # noqa: BLE001 - a prompt tweak must never fail a turn
        return ""


def apply_voice_preamble(text: str, *, voice: bool = True,
                         persona: str = "", session: str = "") -> str:
    """Prepend the app-turn instruction block to a prompt.

    The Clarp-skills guidance is always included; the <speak> voice guidance is added
    only when `voice` is True (a spoken turn). `voice` defaults True so existing callers keep the
    full block."""
    identity = persona_identity_instruction(persona, session)
    if identity:
        return _preamble(voice=voice, identity=identity, session=session) + text
    return _preamble(voice=voice, session=session) + text


def strip_voice_preamble(text: str) -> str:
    """Inverse of apply_voice_preamble — recover the user's original message
    for the history pane. A no-op if the preamble isn't present."""
    if not isinstance(text, str):
        return text
    # OpenCode stores the prompt argument wrapped in double quotes.
    quoted = text.startswith('"' + _VOICE_PREAMBLE_HEAD) and text.endswith('"')
    body = text[1:-1] if quoted else text
    if body.startswith(_VOICE_PREAMBLE_HEAD):
        i = body.find(_VOICE_PREAMBLE_SPLIT)
        if i != -1:
            return body[i + len(_VOICE_PREAMBLE_SPLIT):]
    return text
