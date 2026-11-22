"""The voice pool (issue 476, V2b): every new agent and contact ends with a working voice.

"Working" means the resolver the speaking path uses (voice_profile.provider_voice) finds a voice
for the agent under the Host's configured provider AND under its configured fallback. Only the
providers the stored map and the persona lists leave without a voice are filled; every entry a
caller gave is kept. Google and Deepgram need nothing: each has one default voice.

A voice is picked by a walk from sha256(name) through the provider's pool to the first voice no
agent (archived ones included: an archived agent can be restored with its voice) and no contact
speaks with, and that no built-in contact owns (the persona lists and the roster keep their
voices for their own names). When every voice is taken, the least recently used one is reused:
the voice whose holders last spoke (their newest clip, else their creation) longest ago, logged
as voicePoolExhausted. The walk is stable: the same name on the same pool and the same fleet
gets the same voice, and two agents do not share a voice while the pool has a free one.

Cartesia's English pool is the curated list in cartesia_voice_pool.json (built from Cartesia's
catalogue by scripts/build_cartesia_voice_pool.py: native English accents, no novelty voices,
one voice per name, spread by gender, country and description), less the voices today's
catalogue (the last fetch the voice names file records) no longer offers. Another language's
pool is today's catalogue in that language: public voices that are not the account's own, so
a new agent never gets a voice Cartesia has deleted, a voice the owner cloned or a voice of
another language. Only when neither has a voice is it the built-in list.

A helper (role helper) speaks with its parent's voice: it is heard only when the user opens
its chat, and helpers come and go by the dozen, so giving each its own voice would empty the
pool within days (archived holders keep theirs). A Janitor gets none: the apps cannot send it
a message, so it never speaks. Every pick and the insert that stores it run under
one lock (``assigning()``), so two agents or contacts created at once never get the same voice.
"""
from __future__ import annotations

import functools
import hashlib
import json
import pathlib
import threading
from collections import Counter
from typing import Callable, Iterable, Mapping

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
    eligible = sorted(v for v, row in rows.items()
                      if row.get("public") is True and row.get("owner") is False and v not in gone
                      and _language(row.get("language")) == want)
    if not eligible:
        # The fallback to the built-in list is silent otherwise (it hid a whole catalogue
        # read as not public on 21 Nov 2026).
        from .log import log
        log("voicePoolNoCatalogueVoice",
            f"provider={CARTESIA} language={want} eligible=0 total={len(rows)}")
    return eligible


CURATED_PATH = pathlib.Path(__file__).with_name("cartesia_voice_pool.json")


@functools.lru_cache(maxsize=1)
def _curated_rows() -> tuple[dict, ...]:
    try:
        data = json.loads(CURATED_PATH.read_text())
    except (OSError, ValueError):
        return ()
    return tuple(row for row in data.get("voices") or () if isinstance(row, dict) and row.get("id"))


def curated_names() -> dict[str, str]:
    """voice id -> name of every voice in the curated Cartesia pool (for voice_label)."""
    return {str(row["id"]): str(row.get("name") or "") for row in _curated_rows() if row.get("name")}


def _cartesia_curated(language: str = "en") -> list[str]:
    """The curated English pool, less what today's catalogue no longer offers or Cartesia
    deleted. Empty for another language (its pool is the catalogue)."""
    if _language(language) != "en":
        return []
    from . import voice_names
    rows = voice_names.catalogue(CARTESIA)
    gone = voice_names.gone(CARTESIA)
    return [str(row["id"]) for row in _curated_rows()
            if str(row["id"]) not in gone and (rows is None or str(row["id"]) in rows)]


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
        source = (configured or _cartesia_curated(language) or _cartesia_catalogue(language)
                  or list(DEFAULT_CARTESIA_VOICES.values()))
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
    """How many agents (archived ones included) and contacts speak with each voice of ``provider``."""
    counts: Counter = Counter()
    for agent in agents:
        if agent.get("is_janitor"):
            continue
        voice = _speaks(cfg, str(agent.get("voice_id") or ""), str(agent.get("persona") or ""), provider)
        if voice:
            counts[voice] += 1
    for contact in contacts:
        voice = _speaks(cfg, str(contact.get("voice_id") or ""), str(contact.get("name") or ""), provider)
        if voice:
            counts[voice] += 1
    return counts


def last_used(cfg, provider: str, agents: Iterable[Mapping], contacts: Iterable[Mapping],
              spoke: Mapping[str, int] | None = None) -> dict[str, int]:
    """voice -> when one of its holders last used it (ms): an agent's newest clip, else its
    creation; a contact's creation. ``spoke``: agent id -> newest clip (read when None)."""
    if spoke is None:
        spoke = _last_clips()
    out: dict[str, int] = {}
    for agent in agents:
        if agent.get("is_janitor"):
            continue
        voice = _speaks(cfg, str(agent.get("voice_id") or ""), str(agent.get("persona") or ""), provider)
        if voice:
            when = int(spoke.get(str(agent.get("agent_id") or "")) or agent.get("created_at") or 0)
            out[voice] = max(out.get(voice, 0), when)
    for contact in contacts:
        voice = _speaks(cfg, str(contact.get("voice_id") or ""), str(contact.get("name") or ""), provider)
        if voice:
            out[voice] = max(out.get(voice, 0), int(contact.get("created_at") or 0))
    return out


def _last_clips() -> dict[str, int]:
    """agent id -> its newest clip's time (ms); one grouped read of the clips index."""
    try:
        from .db import conn
        rows = conn().execute("SELECT agent_id, MAX(created_at) FROM clips GROUP BY agent_id").fetchall()
    except Exception:  # noqa: BLE001 - recency only orders a reuse; creation times still do
        return {}
    return {str(agent_id): int(when or 0) for agent_id, when in rows}


def pick(cfg, provider: str, name: str, taken: Counter, *, keep: set[str] | frozenset = frozenset(),
         language: str = "en", recency: Callable[[], Mapping[str, int]] | None = None) -> str | None:
    """The walk. ``keep``: voices never handed out while another is free (built-in contacts'
    own); ``recency``: voice -> last used (ms), read only when every voice is taken."""
    voices = pool(cfg, provider, language)
    if not voices:
        return None
    start = int(hashlib.sha256(name.strip().casefold().encode()).hexdigest(), 16) % len(voices)
    order = [v for v in voices[start:] + voices[:start] if v not in keep] or voices[start:] + voices[:start]
    for voice in order:
        if not taken.get(voice):
            return voice
    used = dict(recency() if recency else {})
    # The least recently used voice; among equals the least shared, then the walk's order.
    least = min(order, key=lambda voice: (used.get(voice, 0), taken.get(voice, 0)))
    from .log import log
    log("voicePoolExhausted", f"provider={provider} pool={len(voices)} reused={least} "
        f"last_used_ms={used.get(least, 0)} shared_by={taken.get(least, 0)}")
    return least


def without_placeholders(raw: str | None) -> str:
    """``raw`` in the one stored shape, without a phone's auto-<uuid> stand-in voices."""
    mapping = {p: v for p, v in voice_map(canonical(raw)).items() if not str(v).startswith(PLACEHOLDER_PREFIX)}
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"))


def assign(cfg, name: str, stored: str | None, *, agents: Iterable[Mapping] | None = None,
           contacts: Iterable[Mapping] | None = None) -> str:
    """``stored`` (any shape) in the one stored shape, with a pool voice added for each needed
    provider it, and the persona lists, leave without one. Reads the fleet once when it must
    pick. Always the one shape: the missing providers are judged on it, and a bare Cartesia
    uuid kept bare would be read as an ElevenLabs id and leave the agent silent."""
    raw = without_placeholders(stored)
    mapping = voice_map(raw)
    missing = [p for p in needed_providers(cfg, name) if not _speaks(cfg, raw, name, p)]
    if not missing:
        return raw
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
                     language=language,
                     recency=functools.partial(last_used, cfg, provider, agents, contacts))
        if voice:
            mapping[provider] = voice
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"))


def inherited(cfg, name: str, stored: str | None, parent: Mapping | None) -> str:
    """A helper's stored voice (any shape) in the one shape, with its parent's voice written
    in for each needed provider it has none for: the voice the parent is heard with (its
    stored map or its contact's), so the helper sounds like the agent it works for."""
    raw = without_placeholders(stored)
    if not parent:
        return raw
    mapping = voice_map(raw)
    parent_raw, parent_name = str(parent.get("voice_id") or ""), str(parent.get("persona") or "")
    for provider in needed_providers(cfg, name):
        if _speaks(cfg, raw, name, provider):
            continue
        voice = _speaks(cfg, parent_raw, parent_name, provider)
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
