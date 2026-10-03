"""How human and emotional an agent sounds when it speaks (0-10).

`voice.humanness` is the Host-wide default (5 when unset: the conversational
delivery every agent had before this setting existed). `voice.agent_humanness`
maps an agent name, persona or session (lowercased) to its own level, the same
keys as the per-agent transcription settings.

The level resolves to the "natural delivery" part of the spoken-turn
instructions. Prose does not interpolate, so the eleven levels fall into four
bands; the number itself is also stated so the model can lean within a band.
Emotion tags such as [laughing] are suggested only when the agent's clips go
to Gemini TTS, which acts them out; every other engine would read them aloud.
They are written inside <vox>, which every client already hides from the
screen and the TTS path unwraps, so a tag is heard but never shown.
"""
from __future__ import annotations

import json

from . import settings_store

DEFAULT_KEY = "voice.humanness"
AGENT_KEY = "voice.agent_humanness"
MIN_LEVEL = 0
MAX_LEVEL = 10
DEFAULT_LEVEL = 5

# Gemini 3.8 Flash TTS acts these out (tested 2026-10-03 with designed voices).
# Pause tags are left out on purpose: next to another tag they were sometimes
# read aloud ("Long pause").
EMOTION_TAGS = ("laughing", "sigh", "amazed", "curious", "sarcasm",
                "whispering", "shouting")


def _valid(value: object) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and MIN_LEVEL <= value <= MAX_LEVEL)


def default_level() -> int:
    return settings_store.get_int(DEFAULT_KEY, default=DEFAULT_LEVEL,
                                  minimum=MIN_LEVEL, maximum=MAX_LEVEL)


def agent_levels() -> dict[str, int]:
    try:
        raw = json.loads(settings_store.get_text(AGENT_KEY) or "{}")
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): v for k, v in raw.items()
            if str(k).strip() and _valid(v)}


def level_for(agent: dict | None = None, session: str | None = None) -> int:
    """The agent's own level (by name, persona, then session), else the default."""
    if agent is None and session:
        from . import agents as agents_db
        agent = agents_db.get_by_session(session) or {}
    agent = agent or {}
    overrides = agent_levels()
    for key in (agent.get("name"), agent.get("persona"),
                agent.get("session") or session):
        if key and str(key).strip().lower() in overrides:
            return overrides[str(key).strip().lower()]
    return default_level()


def get() -> dict:
    return {"default": default_level(), "agents": agent_levels(),
            "min": MIN_LEVEL, "max": MAX_LEVEL}


def update(data: object) -> dict:
    """Apply `{"default"?: int, "agents"?: {agent: int | null}}`.

    A null or empty agent value removes that override so the agent follows the
    default again. Raises ValueError for anything out of range.
    """
    if not isinstance(data, dict):
        raise ValueError("object required")
    default = data.get("default")
    if "default" in data and not _valid(default):
        raise ValueError(f"default must be an integer {MIN_LEVEL}-{MAX_LEVEL}")
    agents = data.get("agents")
    merged = None
    if agents is not None:
        if not isinstance(agents, dict):
            raise ValueError("agents must be an object of agent -> level")
        merged = agent_levels()
        for agent, level in agents.items():
            key = str(agent).strip().lower()
            if not key:
                raise ValueError("agents keys must be agent names")
            if level in (None, ""):
                merged.pop(key, None)
            elif not _valid(level):
                raise ValueError(f"level for {key} must be an integer "
                                 f"{MIN_LEVEL}-{MAX_LEVEL}")
            else:
                merged[key] = level
    if "default" in data:
        settings_store.set_int(DEFAULT_KEY, default)
    if merged is not None:
        settings_store.set_text(
            AGENT_KEY, json.dumps(merged, sort_keys=True, separators=(",", ":")))
    return get()


def speaks_with_gemini(agent: dict | None) -> bool:
    """True when this agent's clips are synthesized by Gemini TTS."""
    try:
        from . import config, tts_mode
        return tts_mode.route(config.load(), agent).provider == "gemini"
    except Exception:  # noqa: BLE001 - a prompt tweak must never fail a turn
        return False


_NEUTRAL = (
    "Speak plainly and neutrally: clear, complete sentences with no fillers, "
    "no hesitations and no emotion cues. Brief pauses such as "
    "<break time=\"350ms\"/> are fine where a listener needs a beat."
)

_LIGHT = (
    "Sound relaxed and conversational but tidy. Use brief pauses such as "
    "<break time=\"350ms\"/> and, now and then, a single filler wrapped in "
    "<vox>…</vox> (spoken, never shown on screen), e.g. <vox>so</vox>, "
    "<vox>hmm</vox>. Most replies need none; never more than one per reply."
)

# Level 5 is the delivery every agent had before this setting existed.
_CONVERSATIONAL = (
    "Every spoken response should sound conversational, including confident "
    "and simple answers. Use brief pauses such as <break time=\"350ms\"/> and "
    "occasional fillers naturally throughout; do not reserve them for "
    "uncertainty. Wrap EVERY filler in <vox>…</vox> so it is spoken yet never "
    "shown on screen, e.g. "
    "<vox>um</vox>, <vox>uh</vox>, <vox>hmm</vox>, <vox>like</vox>, "
    "<vox>you know</vox>. Keep very short acknowledgments concise, but give "
    "substantive spoken replies at least one natural conversational cue. Keep it tasteful "
    "— a couple of pauses or fillers, never a stutter-fest; breaks around "
    "300–450ms; spell fillers plainly (um/uh/hmm), never stretched out."
)

_HUMAN = (
    "Sound like a real person thinking out loud on a phone call, warm and "
    "emotionally present. Use frequent natural fillers, each wrapped in "
    "<vox>…</vox> so it is spoken yet never shown on screen: <vox>um</vox>, "
    "<vox>uh</vox>, <vox>hmm</vox>, <vox>like</vox>, <vox>you know</vox>, "
    "<vox>I mean</vox>, <vox>so</vox>, <vox>okay so</vox>. Let the rhythm be "
    "spoken, not written: short fragments, a hesitation before the hard part, "
    "the occasional self-correction (<vox>wait, no</vox> — <vox>actually</vox>), "
    "comma-paced phrasing, brief pauses such as <break time=\"350ms\"/>. Let "
    "real feeling through: pleased when something works, wry when it is "
    "silly, a little rueful when it broke. Spell fillers plainly, never "
    "stretched out. The content still comes first: every fact, number and "
    "decision must be as clear and complete as in a plain reply — the "
    "humanness is in the delivery, never instead of the substance."
)

_GEMINI_TAGS = (
    "This agent's voice acts out emotion tags. Where a reaction is genuine, "
    "add one, wrapped in <vox> so it is heard but not shown: "
    + ", ".join(f"<vox>[{tag}]</vox>" for tag in EMOTION_TAGS)
    + ". One tag at a time, never two side by side, and only a few per reply. "
    "[whispering] and [shouting] colour the words after them; use them "
    "rarely. Never write pause tags such as [short pause] or [long pause] "
    "(they get read aloud) and never instruction prefixes like \"Say this "
    "excitedly:\" (also read aloud)."
)

_TAIL = ("These cues live ONLY inside <speak>; the <vox> wraps and the tags "
         "are stripped from the visible text automatically.")


def _band(level: int) -> str:
    if level <= 0:
        return _NEUTRAL
    if level <= 3:
        return _LIGHT
    if level <= 6:
        return _CONVERSATIONAL
    return _HUMAN


def guidance(level: object, *, gemini: bool = False) -> str:
    """The natural-delivery instructions for `level` (clamped to 0-10)."""
    try:
        level = max(MIN_LEVEL, min(MAX_LEVEL, int(level)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        level = DEFAULT_LEVEL
    parts = [f"Humanness: {level}/10.", _band(level)]
    if gemini and level >= 4:
        parts.append(_GEMINI_TAGS)
    if level > 0:
        parts.append(_TAIL)
    return " ".join(parts)


def guidance_for(agent: dict | None = None, session: str | None = None) -> str:
    """`guidance` at this agent's level and with its TTS provider."""
    if agent is None and session:
        from . import agents as agents_db
        agent = agents_db.get_by_session(session) or {}
    return guidance(level_for(agent, session), gemini=speaks_with_gemini(agent))
