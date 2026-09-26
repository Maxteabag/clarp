"""Per-agent GPT-Live voices and the "put me through to Theo" contact switch.

In an Oracle v2 call the user can ask to talk to an agent directly. The Host
then replaces the upstream GPT-Live session with one that speaks in a voice of
that agent's own (GPT-Live fixes the voice when a session starts), relays the
agent's replies word for word and hands every substantive turn to that agent.
"Back to Oracle" returns to Oracle's own voice. See docs/oracle-v2.md,
"Talking to an agent directly".

Voices. Oracle keeps ``marin``; no agent is ever given it. gpt-live-1
accepted every voice below in a 2026-09-26 session-start probe (see
docs/oracle-v2.md): the documented Live voices and the older Realtime ones.
Each persona is matched to the gender of its Cartesia voice (checked against
the Cartesia catalog the same day). A persona without a mapping gets a
deterministic pick from its gender's pool, or from both pools when the gender
is unknown. ``[oracle.agent_voices]`` in config.toml overrides the mapping.

``switch_request`` is the deterministic pre-check that starts a switch
without a router call. Like ``oracle_relay.classify`` it only accepts a short
turn that is nothing but an explicit switch phrase; anything that also asks
for work ("ask Theo to talk to Lena directly", "put me through to Theo and
ask him about the deploy") keeps its normal route, and the operator router can
still switch through its ``switch_contact`` tool.
"""
from __future__ import annotations

import hashlib
import re

from .oracle_relay import _normalize

ORACLE_VOICE = "marin"
# Documented GPT-Live voices first (the owner's preference), then the older
# Realtime voices gpt-live-1 also accepts. tempo and bossa are skipped: they
# are Portuguese-language voices.
MASCULINE = ("meridian", "vesper", "stone", "ripple", "cinder", "beacon",
             "cedar", "ash", "verse", "ballad", "echo")
FEMININE = ("gleam", "willow", "quartz", "delta", "coral", "sage", "shimmer")
VOICES = frozenset(MASCULINE + FEMININE)

AGENT_VOICES = {
    "Theo": "meridian", "Omar": "vesper", "Caleb": "stone", "Adam": "ripple",
    "Josh": "cinder", "Sam": "beacon", "Marcus": "cedar", "Felix": "ash",
    "Diego": "verse", "Antoni": "ballad", "Mike": "echo",
    "Lena": "gleam", "Nadia": "willow", "Priya": "quartz", "Yuki": "delta",
    "Freya": "coral", "Bella": "sage", "Elli": "shimmer", "Domi": "delta",
}

# Gender of each persona's Cartesia voice (DEFAULT_CARTESIA_VOICES), read
# from the Cartesia catalog on 2026-09-26.
_M, _F = "masculine", "feminine"
GENDERS = {
    "Theo": _M, "Omar": _M, "Caleb": _M, "Adam": _M, "Josh": _M, "Sam": _M,
    "Marcus": _M, "Felix": _M, "Diego": _M, "Antoni": _M, "Mike": _M,
    "Arnold": _M, "Claude": _M, "Codex": _M, "Grok": _M, "Clarp": _M,
    "Axel": _M, "Gordon": _M, "Tigo": _M, "Lezo": _M, "Orion": _M,
    "Cipher": _M, "Spectra": _M, "Echo": _M, "Margrok": _M, "Vance": _M,
    "Fang": _M, "Jax": _M, "Dagger": _M, "Spike": _M, "Noodle": _M,
    "Fathom": _M, "Nautilus": _M, "Trench": _M,
    "Lena": _F, "Nadia": _F, "Priya": _F, "Yuki": _F, "Freya": _F,
    "Bella": _F, "Elli": _F, "Domi": _F, "Rachel": _F, "Gemini": _F,
    "Iris": _F, "Marsy": _F, "Wren": _F, "Vesper": _F, "Avana": _F,
    "Aura": _F, "Lyra": _F, "Nova": _F, "Roxy": _F, "Riot": _F,
    "Raven": _F, "Blaze": _F, "Pip": _F, "Mochi": _F, "Coral": _F, "Luma": _F,
}
_GENDERS = {name.casefold(): gender for name, gender in GENDERS.items()}
_AGENT_VOICES = {name.casefold(): voice for name, voice in AGENT_VOICES.items()}


def _catalog_gender(persona):
    """Gender of the persona's Cartesia voice when the catalog is cached."""
    from . import cartesia_voices, config
    voice_id = config.load().cartesia_voice_for(persona)
    row = cartesia_voices.cached_english_voice(voice_id) if voice_id else None
    gender = str((row or {}).get("gender") or "").casefold()
    return gender if gender in (_M, _F) else None


def gender_for(persona):
    key = str(persona or "").strip().casefold()
    if key in _GENDERS:
        return _GENDERS[key]
    # Anonymous agents are named "<Archetype>-<hex>" and sound like the archetype.
    base = key.split("-", 1)[0]
    if base in _GENDERS:
        return _GENDERS[base]
    return _catalog_gender(persona) if key else None


def voice_for(persona, overrides=None):
    """The GPT-Live voice an agent speaks with; never Oracle's."""
    key = str(persona or "").strip().casefold()
    wanted = {str(k).casefold(): str(v).strip().casefold() for k, v in (overrides or {}).items()}.get(key)
    if wanted:
        if wanted in VOICES:
            return wanted
        from .log import log
        log("oracleAgentVoiceIgnored", f"persona={persona} voice={wanted} reason="
            + ("oracle's own voice" if wanted == ORACLE_VOICE else "not a known GPT-Live voice"))
    if key in _AGENT_VOICES:
        return _AGENT_VOICES[key]
    gender = gender_for(persona)
    pool = MASCULINE if gender == _M else FEMININE if gender == _F else MASCULINE + FEMININE
    return pool[int(hashlib.sha256(key.encode()).hexdigest(), 16) % len(pool)]


def contact_instructions(persona):
    """Instructions for the session that speaks as one agent."""
    return f"""
You are the voice of {persona}, a Clarp agent, in a live voice call. The user
asked to talk to {persona} directly, so the Host now speaks {persona}'s replies
in your voice. You are not Oracle: never claim to be Oracle, never speak as
Oracle, and never describe yourself as a relay or an interface.
You do not do {persona}'s work yourself. Hand every substantive question,
request, correction, clarification and status question to the Host, keeping
the user's words; the Host sends it to {persona}. Only brief social
acknowledgements and voice stop/interrupt controls stay with you.
When {persona}'s reply arrives, speak it word for word in the first person as
{persona}, without summarising or adding to it. Long replies arrive in numbered
parts; until a part says it is the end, more remains. Replies from any other
agent are theirs: attribute them by name.
Say a request was sent only after the Host provides an actual admission
receipt; an accepted request is not a finished one.
When the user asks to go back to Oracle or to talk to someone else, hand that
to the Host.
"""


_MAX_WORDS = 12
_TAIL = r"(?: please| now| thanks| thank you)*"
_LEAD = r"(?:(?:can|could|would|will) you |please )?"
_NAME = r"(?P<name>[a-z0-9][a-z0-9-]*)"
_AGENT_PATTERNS = [
    re.compile(_LEAD + r"(?:put|patch|connect|transfer|send|get) me (?:straight |directly |back )?(?:through )?"
               r"(?:to|with) " + _NAME + r"(?: directly| direct)?(?: again)?" + _TAIL),
    re.compile(r"(?:(?:let me|can i|could i|may i|i want to|i'd like to|i would like to|i wanna|i need to) )?"
               r"(?:talk|speak) (?:directly )?(?:to|with) " + _NAME + r" (?:directly|direct)" + _TAIL),
    re.compile(r"(?:(?:let me|can i|could i|may i|i want to|i'd like to|i would like to|i wanna|i need to) )?"
               r"(?:talk|speak) directly (?:to|with) " + _NAME + _TAIL),
    re.compile(_LEAD + r"(?:switch|hand|pass) me (?:over )?to " + _NAME + _TAIL),
]
_ORACLE_PATTERNS = [
    re.compile(r"(?:(?:go|come|switch|take me|put me|get me|bring me|send me|hand me|switch me|patch me|connect me) )?"
               r"back(?: through)? to (?:the )?oracle" + _TAIL),
    re.compile(_LEAD + r"switch (?:me )?back" + _TAIL),
    re.compile(r"(?:(?:let me|can i|could i|i want to|i'd like to|i wanna) )?(?:talk|speak) (?:to|with) (?:the )?oracle"
               r"(?: again)?" + _TAIL),
    re.compile(r"(?:i want |give me |i'd like )(?:the )?oracle back" + _TAIL),
]


def switch_request(text):
    """("agent", name), ("oracle", None) or None for one user turn."""
    value = _normalize(text)
    if not value or len(value.split()) > _MAX_WORDS:
        return None
    for pattern in _ORACLE_PATTERNS:
        if pattern.fullmatch(value):
            return "oracle", None
    for pattern in _AGENT_PATTERNS:
        match = pattern.fullmatch(value)
        if match:
            name = match.group("name")
            if name in ("oracle", "the"):
                return "oracle", None
            if name in ("him", "her", "them", "me", "you", "it", "someone", "somebody"):
                return None
            return "agent", name
    return None
