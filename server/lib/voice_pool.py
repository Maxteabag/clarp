"""The voice pool (issue 476, V2b): every new agent and contact ends with a working voice.

"Working" means the resolver the speaking path uses (voice_profile.provider_voice) finds a voice
for the agent under the Host's configured provider AND under its configured fallback. Only the
providers the stored map and the persona lists leave without a voice are filled; every entry a
caller gave is kept. Google and Deepgram need nothing: each has one default voice.

A voice is picked by a walk from sha256(name) through the provider's pool to the first voice no
active agent and no contact speaks with, and that no built-in contact owns (the persona lists and
the roster keep their voices for their own names); when every voice is taken, the least shared one
(in the walk's order). The walk is stable: the same name on the same pool and the same fleet gets
the same voice, and two agents do not share a voice while the pool has a free one.

Cartesia's default pool is TODAY's catalogue (the last fetch the voice names file records):
public voices that are not the account's own, in the agent's speech language, so a new agent
never gets a built-in contact's voice, a voice Cartesia has deleted, a voice the owner cloned or
a voice of another language; only when no catalogue has been fetched yet, or it has no such
voice, is it the built-in list. Every pick and the insert that stores it run under
one lock (``assigning()``), so two agents or contacts created at once never get the same voice.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from typing import Iterable, Mapping

from .voice import CARTESIA, ELEVENLABS, GEMINI, canonical, voice_map
from .voice_profile import elevenlabs_id_usable, provider_voice

POOL_PROVIDERS = (CARTESIA, ELEVENLABS, GEMINI)
PLACEHOLDER_PREFIX = "auto-"     # a phone's stand-in voice id when the user chose none (J)
_ASSIGN_LOCK = threading.RLock()


def assigning() -> threading.RLock:
    """The one lock every pool pick and the insert that stores it run under (agents,
    contacts, contact assignment). Reentrant: a contact minted inside an agent's creation."""
    return _ASSIGN_LOCK


_SAME_LANGUAGE = {"nb": "no", "nn": "no"}      # Norwegian Bokmål and Nynorsk are spoken as "no"


def _language(code: str | None) -> str:
    base = str(code or "").strip().lower().replace("_", "-").split("-")[0]
    return _SAME_LANGUAGE.get(base, base)


def speech_language(name: str) -> str:
    """The language a new agent or contact named ``name`` is spoken in: the first code of its
    language set, else the Host's (tts_registry.agent_speech_language, R168), else "en"."""
    from .tts_registry import agent_speech_language
    return _language(agent_speech_language({"persona": name, "name": name})) or "en"


def _cartesia_catalogue(language: str = "en") -> list[str]:
    """The Cartesia voices a new agent may get TODAY: rows of the last catalogue fetch (the
    voice names file records them as Cartesia sent them) that are public, not the account's
    own (a voice the owner cloned is never handed to anyone), in ``language``, and not deleted;
    never a voice only remembered by name; never a fetch."""
    from . import voice_names
    rows = voice_names.catalogue(CARTESIA)
    if not rows:
        return []
    gone = voice_names.gone(CARTESIA)
    want = _language(language)
    return sorted(v for v, row in rows.items()
                  if row.get("public") and not row.get("owner") and v not in gone
                  and _language(row.get("language")) == want)


def reserved(cfg, provider: str) -> set[str]:
    """Voices the built-in contacts keep for their own names (the persona lists, the roster)."""
    if provider == CARTESIA:
        return {v for v in (getattr(cfg, "cartesia_voices", {}) or {}).values() if v}
    if provider == GEMINI:
        return {v for v in (getattr(cfg, "gemini_voices", {}) or {}).values() if v}
    if provider == ELEVENLABS:
        return {v for v in (getattr(cfg, "roster", {}) or {}).values() if v}
    return set()


def pool(cfg, provider: str, language: str = "en") -> list[str]:
    """The voices a new agent may be given for ``provider``, in a fixed order; Cartesia's in
    the agent's ``language``."""
    if provider == CARTESIA:
        from .config import DEFAULT_CARTESIA_VOICES
        configured = list(getattr(cfg, "cartesia_pool", ()) or ())
        source = configured or _cartesia_catalogue(language) or list(DEFAULT_CARTESIA_VOICES.values())
        return list(dict.fromkeys(v for v in source if v))
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


def pick(cfg, provider: str, name: str, taken: Counter, *, keep: set[str] | frozenset = frozenset(),
         active: Counter | None = None, language: str = "en") -> str | None:
    """The walk. ``keep``: voices never handed out while another is free (built-in contacts'
    own); ``active``: users that are active agents and custom contacts, for the log line."""
    voices = pool(cfg, provider, language)
    if not voices:
        return None
    start = int(hashlib.sha256(name.strip().casefold().encode()).hexdigest(), 16) % len(voices)
    order = [v for v in voices[start:] + voices[:start] if v not in keep] or voices[start:] + voices[:start]
    for voice in order:
        if not taken.get(voice):
            return voice
    least = min(order, key=lambda voice: taken.get(voice, 0))
    if active is None or all(active.get(voice) for voice in voices):
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
    custom = [c for c in contacts if not c.get("builtin")]
    language = speech_language(name)
    for provider in missing:
        voice = pick(cfg, provider, name, users(cfg, provider, agents, contacts),
                     keep=reserved(cfg, provider) | set(users(cfg, provider, [], contacts) - users(cfg, provider, [], custom)),
                     active=users(cfg, provider, agents, custom), language=language)
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
