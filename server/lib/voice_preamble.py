"""App-turn prompt preamble shared by the CLI-backed runners.

Codex, AGY, Grok and OpenCode never receive the PWA's system reminders
(Claude gets those from the UserPromptSubmit hook), so for app-dispatched
turns the runners prepend these instructions to the prompt themselves. The
transcript parsers strip the block back off with ``strip_voice_preamble``.
"""
from __future__ import annotations

from . import settings_store
from .config import persona_personality
from .personalities import KEY_ENABLED as PERSONALITIES_ENABLED_KEY


# Codex (unlike Claude) never receives the PWA's system reminders — Claude
# gets those from the UserPromptSubmit hook, which Codex has no equivalent of.
# So for PWA/native turns we prepend the instructions to the prompt ourselves.
# The head + split sentinels let the history parser strip them back off so the
# user's message renders cleanly.
_VOICE_PREAMBLE_HEAD = "[voice-mode]"
_VOICE_PREAMBLE_SPLIT = "\n\n--- user message ---\n"

# Always-on for app-dispatched turns: CLI question UIs are unavailable,
# while supporting Hosts can publish durable questions to the native inbox.
_NO_INTERACTIVE_QUESTIONS = (
    "You are connected through a phone/voice app. It cannot display CLI "
    "interactive prompts, question tools, multiple-choice pickers, or approval "
    "dialogs. Never call AskUserQuestion, request_user_input, or similar CLI "
    "popup tools; they will not render. For a material clarification, use the "
    "clarp-decisions skill's documented clarp-agent-artifacts question helper "
    "when the Host supports native questions. These durable Clarp artifacts "
    "are answered in Updates or the conversation, not in a CLI popup. For "
    "explicit authorization, use the skill's decision helper and wait for "
    "approval. If native questions are unavailable, ask in ordinary text and "
    "wait for the user's reply. Make routine implementation choices yourself "
    "and continue independent work while awaiting a necessary answer. Never "
    "self-resolve a question or approval; a preference or custom answer is "
    "not blanket authorization."
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
    "When that summary runs long, split it across consecutive <speak> blocks "
    "of about a minute each rather than one long one: each block is "
    "synthesized as its own clip, so the first starts playing while the rest "
    "is still being written, and no single clip is long enough to be cut off "
    "at a provider limit. "
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
    body = _NO_INTERACTIVE_QUESTIONS
    if voice:
        body = f"{body}\n\n{_VOICE_INSTRUCTION}\n\n{_NATURAL_SPEECH}"
        narration = _narration_clause(session)
        if narration:
            body = f"{body}\n\n{narration}"
    return body


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

    The CLI-question restriction is always included (native question artifacts
    remain available on supporting Hosts); the <speak> voice guidance is added
    only when `voice` is True (a spoken turn). `voice` defaults True so existing callers keep the
    full block."""
    identity = persona_identity_instruction(persona, session)
    if identity:
        return _preamble(voice=voice, identity=identity, session=session) + text
    return _preamble(voice=voice, session=session) + text


def strip_voice_preamble(text: str) -> str:
    """Inverse of apply_voice_preamble — recover the user's original message
    for the history pane. A no-op if the preamble isn't present."""
    if isinstance(text, str) and text.startswith(_VOICE_PREAMBLE_HEAD):
        i = text.find(_VOICE_PREAMBLE_SPLIT)
        if i != -1:
            return text[i + len(_VOICE_PREAMBLE_SPLIT):]
    return text
