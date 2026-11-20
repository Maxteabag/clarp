"""The voice pool (issue 476, V2b): every new agent and contact ends with a working voice.

"Working" means the resolver the speaking path uses (voice_profile.provider_voice) finds a voice
for the agent under the Host's configured provider AND under its configured fallback. Only the
providers the stored map and the persona lists leave without a voice are filled; every entry a
caller gave is kept. Google and Deepgram need nothing: each has one default voice.

A voice is picked by a walk from sha256(name) through the provider's pool to the first voice no
active agent and no contact speaks with; when every voice is taken, the least shared one (in the
walk's order), with one log line. The walk is stable: the same name on the same pool and the same
fleet gets the same voice, and two agents do not share a voice while the pool has a free one.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Iterable, Mapping

from .voice import CARTESIA, ELEVENLABS, GEMINI, canonical, voice_map
from .voice_profile import elevenlabs_id_usable, provider_voice

POOL_PROVIDERS = (CARTESIA, ELEVENLABS, GEMINI)
PLACEHOLDER_PREFIX = "auto-"     # a phone's stand-in voice id when the user chose none (J)


def pool(cfg, provider: str) -> list[str]:
    """The voices a new agent may be given for ``provider``, in a fixed order."""
    if provider == CARTESIA:
        from .config import DEFAULT_CARTESIA_VOICES
        configured = list(getattr(cfg, "cartesia_pool", ()) or ())
        return list(dict.fromkeys(v for v in (configured or DEFAULT_CARTESIA_VOICES.values()) if v))
    if provider == ELEVENLABS:
        from .voices import VOICE_CATALOG
        return [row["id"] for row in VOICE_CATALOG]
    if provider == GEMINI:
        from .voice_catalog import GEMINI_VOICES
        return [name for name, _ in GEMINI_VOICES]
    return []


def needed_providers(cfg, name: str) -> list[str]:
    """The providers a voice must exist for: the configured one, its fallback, and an
    override's provider for this name. Only those a pool can serve."""
    override = getattr(cfg, "tts_override_for", None)
    found = [getattr(cfg, "tts_provider", ""), getattr(cfg, "tts_fallback", ""),
             str((override({"persona": name}) if callable(override) else {}).get("provider") or "")]
    return [p for p in dict.fromkeys(found) if p in POOL_PROVIDERS]


def _speaks(cfg, raw: str, name: str, provider: str) -> str | None:
    """The voice ``provider`` would be handed for an agent named ``name`` storing ``raw``
    (persona lists included), or None when it has none of its own."""
    choice = provider_voice(cfg, raw, {"persona": name}, None, provider)
    if choice.origin != "own":
        return None
    if provider == ELEVENLABS and not elevenlabs_id_usable(choice.voice):
        return None
    return choice.voice


def users(cfg, provider: str, agents: Iterable[Mapping], contacts: Iterable[Mapping]) -> Counter:
    """How many active agents and contacts speak with each voice of ``provider``."""
    counts: Counter = Counter()
    for agent in agents:
        if agent.get("archived_at") or agent.get("is_janitor"):
            continue
        voice = _speaks(cfg, str(agent.get("voice_id") or ""), str(agent.get("persona") or ""), provider)
        if voice:
            counts[voice] += 1
    for contact in contacts:
        voice = _speaks(cfg, str(contact.get("voice_id") or ""), str(contact.get("name") or ""), provider)
        if voice:
            counts[voice] += 1
    return counts


def pick(cfg, provider: str, name: str, taken: Counter) -> str | None:
    voices = pool(cfg, provider)
    if not voices:
        return None
    start = int(hashlib.sha256(name.strip().casefold().encode()).hexdigest(), 16) % len(voices)
    order = voices[start:] + voices[:start]
    for voice in order:
        if not taken.get(voice):
            return voice
    least = min(order, key=lambda voice: taken.get(voice, 0))
    from .log import log
    log("voicePoolExhausted", f"provider={provider} pool={len(voices)} shared_by={taken.get(least, 0)}")
    return least


def without_placeholders(raw: str | None) -> str:
    """``raw`` in the one stored shape, without a phone's auto-<uuid> stand-in voices."""
    mapping = {p: v for p, v in voice_map(canonical(raw)).items() if not str(v).startswith(PLACEHOLDER_PREFIX)}
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"))


def assign(cfg, name: str, stored: str | None, *, agents: Iterable[Mapping] | None = None,
           contacts: Iterable[Mapping] | None = None) -> str:
    """``stored`` (any shape) with a pool voice added for each needed provider it, and the
    persona lists, leave without one. Reads the fleet once when it must pick."""
    raw = without_placeholders(stored)
    mapping = voice_map(raw)
    missing = [p for p in needed_providers(cfg, name) if not _speaks(cfg, raw, name, p)]
    if not missing:
        # Nothing to add: the value is kept as it was given (a placeholder only is dropped).
        return stored if raw == canonical(stored) or not (stored or "").strip() else raw
    if agents is None or contacts is None:
        from . import agents as agents_db, personas
        agents = agents_db.list_agents() if agents is None else agents
        contacts = personas.list_all() if contacts is None else contacts
    agents, contacts = list(agents), list(contacts)
    for provider in missing:
        voice = pick(cfg, provider, name, users(cfg, provider, agents, contacts))
        if voice:
            mapping[provider] = voice
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"))


def pinned_before_rename(cfg, agent: Mapping) -> str:
    """An agent's stored voice with the voices it speaks with today through its current
    persona name written in, so a rename never silences it."""
    raw = canonical(str(agent.get("voice_id") or ""))
    mapping = voice_map(raw)
    name = str(agent.get("persona") or "")
    for provider in needed_providers(cfg, name):
        if provider in mapping:
            continue
        voice = _speaks(cfg, raw, name, provider)
        if voice:
            mapping[provider] = voice
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"))
