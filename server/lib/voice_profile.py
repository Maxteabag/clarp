"""What an agent sounds like and how it is heard: one resolver (issues 475, 476).

The speaking path (``tts_registry``, ``tts_worker``) picks each clip's voice
with ``provider_voice``, and the hearing path plans its engines with
``stt_router.plan_engines``. The ``voice_profile`` an app shows is built from
the same two calls, so it says what the Host really does, silence and sharing
included.

``HostView`` holds everything that is the same for every agent: the
configuration, the transcription settings (read once), key presence and the
names of voices already in memory. A snapshot builds one per build and then
resolves each agent with no further query, file read or fetch.

``origin`` and ``silent_reason`` are open sets (docs/protocol.md): a client
shows plain words for a value it does not know.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import tts_mode
from .voice import CARTESIA, DEEPGRAM, ELEVENLABS, GEMINI, GOOGLE, resolve_voice

OWN = "own"
DEFAULT = "default"
NO_VOICE = "no_voice"
VOICE_UNAVAILABLE = "voice_unavailable"   # the agent has a voice of its own the provider refuses
PROVIDER_OFF = "provider_off"
NOT_CONFIGURED = "not_configured"
VOICELESS_ROLE = "voiceless_role"     # a role that is never heard (a Janitor's turns are silent)
PROVIDER_FAILED = "provider_failed"   # an outage: the provider did not make the clip
HOST_FAILED = "host_failed"           # the Host itself failed while making the clip
BUILTIN_PROVIDERS = frozenset({CARTESIA, ELEVENLABS, GEMINI, DEEPGRAM, GOOGLE})


@dataclass(frozen=True)
class VoiceChoice:
    """The voice a provider is handed for one agent, and where it came from."""
    voice: str | None
    origin: str | None = None     # OWN | DEFAULT | None (no voice)


def _route_voice(route: tts_mode.Route | None, provider: str) -> str | None:
    """A per-agent override voice applies only to its own provider."""
    if route is not None and provider == route.provider and route.voice:
        return route.voice
    return None


def provider_voice(cfg, raw_voice: str | None, agent: Mapping | None,
                   route: tts_mode.Route | None, provider: str, *,
                   adapter_default: str | None = None) -> VoiceChoice:
    """The voice ``provider`` is handed for this agent's clip. The speaking
    path calls this for every provider it tries; the profile calls it too.

    "Own" is the agent's override, its stored voice, or its contact's voice in
    the Host's persona lists; "default" is a provider's one fallback voice,
    which every agent without its own shares.
    """
    persona = str((agent or {}).get("persona") or "")
    saved = _route_voice(route, provider) or resolve_voice(raw_voice, provider)
    if provider == CARTESIA:
        voice = saved or cfg.cartesia_voice_for(persona)
        return VoiceChoice(voice, OWN if voice else None)
    if provider == ELEVENLABS:
        # A stored value without an ElevenLabs entry is handed over as it is.
        voice = saved or raw_voice
        return VoiceChoice(voice, OWN if voice else None)
    if provider == GEMINI:
        cartesia = provider_voice(cfg, raw_voice, agent, route, CARTESIA).voice
        voice = (saved or cfg.gemini_voice_for(persona)
                 or cfg.gemini_voice_for_cartesia(cartesia))
        if voice:
            return VoiceChoice(voice, OWN)
        if cfg.tts_override_for(agent).get("provider") == GEMINI and cfg.gemini_voice:
            return VoiceChoice(cfg.gemini_voice, DEFAULT)
        return VoiceChoice(None)
    if provider == GOOGLE:
        from . import google_tts
        if saved:
            return VoiceChoice(saved, OWN)
        if raw_voice and raw_voice.strip() in google_tts.VOICES:
            return VoiceChoice(raw_voice.strip(), OWN)
        return VoiceChoice(google_tts.DEFAULT_VOICE, DEFAULT)
    if provider == DEEPGRAM:
        if saved:
            return VoiceChoice(saved, OWN)
        return VoiceChoice(cfg.deepgram_model, DEFAULT if cfg.deepgram_model else None)
    # An installed custom adapter.
    if saved:
        return VoiceChoice(saved, OWN)
    return VoiceChoice(adapter_default, DEFAULT if adapter_default else None)


def elevenlabs_id_usable(voice: str | None) -> bool:
    """Whether ElevenLabs can take ``voice`` as a voice id. A JSON text, a
    Cartesia uuid or an empty value is handed over today and refused there."""
    value = (voice or "").strip()
    return bool(value) and not value.startswith("{") and not any(c.isspace() for c in value) \
        and not (len(value) == 36 and value.count("-") == 4)


# ---------------------------------------------------------------- Host view

TRANSCRIPTION_KEYS = ("transcription.engine", "transcription.language",
                      "transcription.agent_engines", "transcription.agent_languages")


@dataclass
class HostView:
    """Everything the profile needs that is the same for every agent."""
    cfg: Any
    keys: dict[str, bool]
    google_refusal: str | None
    adapters: dict[str, dict]              # custom adapter id -> {"name", "default_voice"}
    labels: dict[str, dict[str, str]]      # provider -> voice id -> human name
    engine: str                            # transcription.engine
    language: str                          # transcription.language (validated)
    agent_engines: dict[str, str]
    agent_languages: dict[str, str]
    gone: dict[str, frozenset] = field(default_factory=dict)   # provider -> voices it deleted (voice_names)
    _version: str = field(default="", repr=False)

    @classmethod
    def build(cls, cfg=None, stored: Mapping[str, str] | None = None) -> "HostView":
        """``stored``: a settings read that included TRANSCRIPTION_KEYS (an
        absent key is unset); without it this reads them, once. The rest is
        configuration and memory: no per-agent work."""
        from . import config as config_module
        from . import settings_store, stt_language, stt_providers
        cfg = cfg if cfg is not None else config_module.load()
        if stored is None:
            stored = settings_store.get_many(list(TRANSCRIPTION_KEYS))
        providers = _configured_providers(cfg)
        keys = _key_presence(cfg, providers)
        google_refusal = None
        if GOOGLE in providers:
            from . import google_tts
            google_refusal = google_tts.collision_code(cfg)
        adapters = _adapters(providers)
        return cls(
            cfg=cfg, keys=keys, google_refusal=google_refusal, adapters=adapters,
            labels=_labels(cfg),
            engine=(stored.get("transcription.engine") or stt_providers.LOCAL_ENGINE),
            language=stt_language.parse_language(stored.get("transcription.language")),
            agent_engines=stt_language.parse_map(stored.get("transcription.agent_engines")),
            agent_languages=stt_language.parse_agent_languages(
                stored.get("transcription.agent_languages")),
            gone=_gone(),
        )

    @property
    def version(self) -> str:
        """Changes whenever a Host-wide input of any agent's profile changes."""
        if not self._version:
            self._version = fingerprint(self.cfg, extra={
                "keys": self.keys, "google_refusal": self.google_refusal,
                "adapters": sorted(self.adapters), "engine": self.engine,
                "language": self.language, "agent_engines": self.agent_engines,
                "agent_languages": self.agent_languages, "names": _names_signature()})
        return self._version


def _gone() -> dict[str, frozenset]:
    from . import voice_names
    return {p: voice_names.gone(p) for p in (CARTESIA, ELEVENLABS)}


def _names_signature():
    from . import voice_names
    return voice_names.signature()


def config_inputs(cfg) -> dict:
    """The configuration that any agent's voice or hearing depends on."""
    return {
        "tts_provider": cfg.tts_provider, "tts_fallback": cfg.tts_fallback,
        "tts_agents": cfg.tts_agent_overrides, "cartesia_voices": cfg.cartesia_voices,
        "gemini_voices": cfg.gemini_voices, "gemini_voice": cfg.gemini_voice,
        "gemini_vertex_voices": getattr(cfg, "gemini_vertex_voices", {}),
        "deepgram_model": cfg.deepgram_model,
        "whisper": [cfg.whisper_provider, cfg.whisper_model, cfg.whisper_enabled],
        "keys": {p: bool(k) for p, k in (
            (CARTESIA, cfg.cartesia_key()), (ELEVENLABS, cfg.eleven_key()),
            (GEMINI, cfg.gemini_tts_key()), (DEEPGRAM, cfg.deepgram_key()))},
    }


def fingerprint(cfg, extra: Mapping | None = None) -> str:
    blob = json.dumps({"config": config_inputs(cfg), **(extra or {})},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _configured_providers(cfg) -> set[str]:
    found = {cfg.tts_provider, cfg.tts_fallback}
    found.update(str(entry.get("provider") or "") for entry in cfg.tts_agent_overrides.values())
    return {p for p in found if p and p != "none"}


def _key_presence(cfg, providers: set[str]) -> dict[str, bool]:
    keys = {CARTESIA: bool(cfg.cartesia_key()), ELEVENLABS: bool(cfg.eleven_key()),
            GEMINI: bool(cfg.gemini_tts_key()), DEEPGRAM: bool(cfg.deepgram_key())}
    if GOOGLE in providers:
        from . import google_tts
        keys[GOOGLE] = not google_tts.credentials(cfg).error
    return keys


def _adapters(providers: set[str]) -> dict[str, dict]:
    custom = providers - BUILTIN_PROVIDERS
    if not custom:
        return {}
    from .custom_tts_adapters import discover
    from .tts_providers import VALID_IDS
    found = {}
    for manifest in discover(reserved_ids=VALID_IDS):
        if manifest.id in custom:
            found[manifest.id] = {"name": getattr(manifest, "name", manifest.id) or manifest.id,
                                  "default_voice": getattr(manifest, "default_voice", "") or ""}
    return found


def _labels(cfg) -> dict[str, dict[str, str]]:
    """Names of voices from code tables, configuration, the names file (voice_names) and
    what the pickers fetched; never a fetch, never a query."""
    from . import cartesia_voices, elevenlabs_voices, google_tts, voice_names
    from .voice_catalog import AURA2_VOICES, FLUX_VOICES, gemini_voices
    from .voices import VOICE_CATALOG
    eleven = {row["id"]: row["label"].split(" — ", 1)[0] for row in VOICE_CATALOG}
    eleven.update(voice_names.names(ELEVENLABS))
    eleven.update(elevenlabs_voices.cached_names())
    # Gemini: in the voice picker's own words (voice_catalog.gemini_voices): a prebuilt voice
    # by its name, a Voice Design voice as "<Persona> (designed)"; a Vertex id is mapped back
    # to the same words through [gemini_tts.vertex_voices]; an id in no map has none.
    gemini = {vid: label for vid, label, _ in gemini_voices(cfg)}
    api_of = getattr(cfg, "gemini_voices", {}) or {}
    for persona, vertex_id in (getattr(cfg, "gemini_vertex_voices", {}) or {}).items():
        if vertex_id and vertex_id not in gemini:
            gemini[vertex_id] = gemini.get(api_of.get(persona, ""), f"{persona} (designed)")
    return {
        CARTESIA: {**voice_names.names(CARTESIA), **cartesia_voices.cached_names()},
        ELEVENLABS: eleven,
        GEMINI: gemini,
        GOOGLE: {v: v.rsplit("-", 1)[1] for v in google_tts.VOICES},
        DEEPGRAM: {vid: name for vid, name, _ in (*FLUX_VOICES, *AURA2_VOICES)},
    }


# ---------------------------------------------------------------- speech

def _provider_label(host: HostView, provider: str) -> str:
    from .tts_providers import CATALOG
    row = next((item for item in CATALOG if item["id"] == provider), None)
    if row is not None:
        return row["name"]
    return str((host.adapters.get(provider) or {}).get("name") or provider)


def _streaming(provider: str) -> bool:
    from .tts_providers import CATALOG
    row = next((item for item in CATALOG if item["id"] == provider), None)
    return bool(row and row.get("streaming"))


def _voice_label(host: HostView, provider: str, voice: str | None) -> str | None:
    """A real name or None, never an empty string (apps show their own words for None)."""
    if not voice:
        return None
    label = host.labels.get(provider, {}).get(voice)
    return (label or "").strip() or None


def _attempt(host: HostView, agent: Mapping, route: tts_mode.Route, provider: str) -> dict:
    """What ``provider`` would say for this agent, or why it would be silent."""
    cfg = host.cfg
    adapter = host.adapters.get(provider)
    choice = provider_voice(cfg, agent.get("voice_id"), agent, route, provider,
                            adapter_default=(adapter or {}).get("default_voice"))
    silent = None
    deleted = bool(choice.voice) and choice.voice in host.gone.get(provider, ())
    if provider == CARTESIA:
        silent = (NO_VOICE if not choice.voice else VOICE_UNAVAILABLE if deleted
                  else None if host.keys.get(CARTESIA) else NOT_CONFIGURED)
    elif provider == ELEVENLABS:
        silent = (NOT_CONFIGURED if not host.keys.get(ELEVENLABS)
                  else NO_VOICE if not elevenlabs_id_usable(choice.voice)
                  else VOICE_UNAVAILABLE if deleted else None)
    elif provider == GEMINI:
        silent = NO_VOICE if not choice.voice else (None if host.keys.get(GEMINI) else NOT_CONFIGURED)
    elif provider == GOOGLE:
        from . import google_tts
        silent = (NOT_CONFIGURED if host.google_refusal
                  else VOICE_UNAVAILABLE if choice.voice not in google_tts.VOICES
                  else None if host.keys.get(GOOGLE) else NOT_CONFIGURED)
    elif provider == DEEPGRAM:
        silent = None if host.keys.get(DEEPGRAM) else NOT_CONFIGURED
    elif adapter is None:
        silent = NOT_CONFIGURED
    usable = silent is None
    return {
        "provider": provider,
        "provider_label": _provider_label(host, provider),
        "voice_id": choice.voice if usable else None,
        "voice_label": _voice_label(host, provider, choice.voice) if usable else None,
        "origin": choice.origin if usable else None,
        "streaming": _streaming(provider),
        "silent_reason": silent,
    }


def speech(host: HostView, agent: Mapping) -> dict:
    """The provider and voice this agent's next clip is spoken with, as the
    worker routes it (tts_mode.route, then the configured fallback before any
    audio), or the reason nothing is heard."""
    route = tts_mode.route(host.cfg, dict(agent))
    if agent.get("is_janitor"):
        # A Janitor speaks to nobody by design (its turns are dispatched silent), and
        # gets no voice from the pool: say that, not "no voice".
        return {"provider": route.provider, "provider_label": _provider_label(host, route.provider),
                "voice_id": None, "voice_label": None, "origin": None,
                "streaming": _streaming(route.provider), "silent_reason": VOICELESS_ROLE}
    # The worker ends every clip when the Host's provider is "none", before it reads
    # any per-agent override (tts_worker.synth_one): the override is never heard.
    if host.cfg.tts_provider == "none" or route.provider == "none":
        return {"provider": "none", "provider_label": _provider_label(host, "none"),
                "voice_id": None, "voice_label": None, "origin": None,
                "streaming": False, "silent_reason": PROVIDER_OFF}
    primary = _attempt(host, agent, route, route.provider)
    if primary["silent_reason"] and route.fallback:
        fallback = _attempt(host, agent, route, route.fallback)
        if not fallback["silent_reason"]:
            return fallback
    return primary


# ---------------------------------------------------------------- hearing

def _engine_row(engine_id: str) -> dict:
    from . import stt_providers
    from .transcription_catalog import model_by_id
    provider, _, model = engine_id.partition(":")
    row = stt_providers._model_row(engine_id)
    label = (row or {}).get("name") or (model_by_id(engine_id) or {}).get("name") or engine_id
    return {"id": engine_id, "provider": provider, "model": model, "label": label}


def hearing(host: HostView, agent: Mapping) -> dict:
    """The engines a recording for this agent is tried with, in order, and the
    languages the first one is handed, as transcription_pipeline plans them
    for a clip the phone sends without a model of its own."""
    from . import stt_language, stt_providers, stt_router
    cfg = host.cfg
    own_engines = stt_language.split_engines(stt_language.match(host.agent_engines, agent))
    cloud = host.engine if stt_providers.is_cloud_model(host.engine) else ""
    plan = stt_router.plan_engines(requested_model="", agent_engines=own_engines,
                                   cloud_engine=cloud, long_model="", in_call=False)
    local = f"{cfg.whisper_provider}:{cfg.whisper_model}" if cfg.whisper_enabled else ""
    # Each engine once, as the pipeline plans them (stt_router.distinct_engines, R175).
    engines, _skipped = stt_router.distinct_engines(
        [e for e in [plan.model or local, *plan.agent_fallbacks] if e], local or None)
    engine_origin = OWN if own_engines else DEFAULT
    own_language = stt_language.match(host.agent_languages, agent)
    language_origin = OWN if own_language else DEFAULT
    codes = [c for c in (own_language or host.language).split(",") if c]
    first = engines[0] if engines else ""
    if first and not stt_providers.is_cloud_model(first):
        # A local model's language is fixed when it loads (stt.WhisperSTT):
        # English for a .en model, detected otherwise; the agent's is not used.
        codes = ["en"] if first.endswith(".en") else []
        language_origin = DEFAULT
    elif stt_providers.provider_of(first) == "elevenlabs" and len(codes) > 1:
        codes = []          # Scribe is handed no language for a set: it detects (stt_providers)
    # The set chosen for this agent (else the Host's), whatever the first engine is handed.
    configured = [c for c in (own_language or host.language).split(",") if c]
    device_may_override = engine_origin == DEFAULT
    # What a hosted engine chosen on the phone is handed for this agent
    # (stt_providers.transcribe: the agent's own set, else the Host's).
    device_cloud = ([c for c in (own_language or host.language).split(",") if c]
                    if device_may_override else None)
    return {
        "engines": [_engine_row(e) for e in engines],
        "engine_origin": engine_origin,
        "device_may_override": device_may_override,
        "device_cloud_languages": device_cloud,
        "languages": codes,
        "language_origin": language_origin,
        "configured_languages": configured,
    }


def profile(host: HostView, agent: Mapping) -> dict:
    return {"speech": speech(host, agent), "hearing": hearing(host, agent)}


def defaults(host: HostView) -> dict:
    """What a new agent gets before it exists. Its voice is known only once it is
    created (its contact's, else a free one from the pool: voice_pool), so speech
    names the provider alone."""
    provider = host.cfg.tts_provider
    return {
        "speech": {"provider": provider, "provider_label": _provider_label(host, provider),
                   "voice_id": None, "voice_label": None, "origin": DEFAULT,
                   "streaming": _streaming(provider), "assignment": "pool",
                   "silent_reason": PROVIDER_OFF if provider == "none" else None},
        "hearing": hearing(host, {}),
    }


class Projection:
    """Every agent's profile for one snapshot build: one HostView, one pass
    over the active agents for ``shared_with``, and no per-agent query."""

    def __init__(self, host: HostView, agent_rows: list[Mapping]):
        self.host = host
        self._active = {a["agent_id"]: profile(host, a) for a in agent_rows
                        if not a.get("archived_at") and not a.get("is_janitor")}
        self._users: dict[tuple[str, str], int] = {}
        for item in self._active.values():
            key = _voice_key(item)
            if key:
                self._users[key] = self._users.get(key, 0) + 1

    def for_row(self, agent: Mapping) -> dict:
        """The agent's profile, with ``speech.shared_with``: how many OTHER
        active agents speak with the same provider and voice (0 = its own)."""
        item = self._active.get(agent["agent_id"])
        mine = item is not None
        if item is None:
            item = profile(self.host, agent)
        key = _voice_key(item)
        users = self._users.get(key, 0) if key else 0
        speech_ = dict(item["speech"], shared_with=max(0, users - 1) if mine else users)
        return {"speech": speech_, "hearing": item["hearing"]}


def _voice_key(item: Mapping) -> tuple[str, str] | None:
    sp = item["speech"]
    return (sp["provider"], sp["voice_id"]) if sp.get("voice_id") else None


def for_agent(agent: Mapping, *, host: HostView | None = None,
              agent_rows: list[Mapping] | None = None) -> dict:
    """One agent's profile outside a snapshot (a creation answer, a roster
    event), sharing counted over ``agent_rows`` (all agents) when given."""
    host = host or HostView.build()
    if agent_rows is None:
        item = profile(host, agent)
        item["speech"]["shared_with"] = 0
        return item
    return Projection(host, agent_rows).for_row(agent)


def for_session(session: str) -> Any:
    """The profile of the agent behind ``session`` for a roster event or a
    creation answer; ``events.OMIT`` when it cannot be resolved, so an event
    is never lost to it (the app re-reads the snapshot either way)."""
    from . import agents as agents_db, events
    from .log import log_exception
    try:
        row = agents_db.get_by_session(session)
        if not row or row.get("archived_at"):
            # An archived agent speaks to nobody: no block, as on the snapshot.
            return events.OMIT
        return for_agent(row, agent_rows=agents_db.list_agents())
    except Exception as exc:  # noqa: BLE001 - a profile never fails the route
        log_exception("voiceProfileFail", exc, detail=session)
        return events.OMIT


def watch_config(stream, previous: str | None) -> str:
    """Tell open apps to re-read the snapshot when the configuration that
    voices depend on changes (a hand edit, clarp-admin). Pure on the cached
    config: no query. ``previous`` is the last fingerprint (None at start:
    nothing to tell); returns the current one for the caller to keep."""
    from . import config as config_module, events
    current = fingerprint(config_module.load())
    if previous is not None and previous != current:
        events.broadcast(stream, events.agent_roster("voice-profile"))
    return current


# ---------------------------------------------------------------- a clip that is not spoken
# (feature speech_outcome). The reasons are the open set of speech.silent_reason plus
# provider_failed and host_failed; the words are built from the reason and the provider's
# label, never from an error's text, and carry nothing provider-internal.
NOT_SPOKEN_WORDS = {
    NO_VOICE: "Not spoken: {provider} has no voice for {name}.",
    VOICE_UNAVAILABLE: "Not spoken: {provider} cannot use the voice set for {name}.",
    NOT_CONFIGURED: "Not spoken: {provider} is not set up on this Host.",
    PROVIDER_OFF: "Not spoken: voice output is off on this Host.",
    PROVIDER_FAILED: "Not spoken: {provider} did not make the clip.",
    HOST_FAILED: "Not spoken: the Host could not make the clip.",
}
# Google's structured error codes (google_tts.ERROR_CODES) to a reason.
GOOGLE_REASONS = {
    "google_credentials_missing": NOT_CONFIGURED, "google_project_missing": NOT_CONFIGURED,
    "google_auth_failed": NOT_CONFIGURED, "google_id_collision": NOT_CONFIGURED,
    "google_id_unverifiable": NOT_CONFIGURED, "voice_unavailable": VOICE_UNAVAILABLE,
}


PARTLY_SPOKEN_PREFIX = "Part of this reply was not spoken: "


def not_spoken_words(reason: str, *, provider_label: str = "", name: str = "",
                     partly: bool = False) -> str:
    """The words for a clip that was not spoken; ``partly`` when an earlier clip of the
    same reply was spoken, so the reply is not shown as wholly silent."""
    template = NOT_SPOKEN_WORDS.get(reason)
    words = template.format(provider=provider_label or "The voice provider",
                            name=name or "this agent") if template else "Not spoken."
    if partly:
        rest = words.removeprefix("Not spoken: ").removeprefix("Not spoken.").strip()
        words = PARTLY_SPOKEN_PREFIX + rest if rest else PARTLY_SPOKEN_PREFIX.rstrip(": ") + "."
    return words


def no_voice_reason(cfg, agent: Mapping | None, route: tts_mode.Route) -> str:
    """Why the worker found no voice for ``route.provider``: voice_unavailable when the agent
    has a voice of its own the provider refuses (a Google voice off its allowlist), else
    no_voice. The same rule as the profile's silent_reason."""
    if route.provider == GOOGLE:
        from . import google_tts
        choice = provider_voice(cfg, (agent or {}).get("voice_id"), agent, route, GOOGLE)
        if choice.voice and choice.voice not in google_tts.VOICES:
            return VOICE_UNAVAILABLE
    return NO_VOICE


def clip_failure(cfg, agent: Mapping | None, *, google_code: str | None = None) -> tuple[str, str]:
    """(reason, provider) for a clip that ended without audio, from the same resolver
    the profile shows: the profile's silent_reason when it has one, else an outage of
    the provider that was to speak. A Google structured code decides its own reason."""
    if agent is None:
        return HOST_FAILED, ""
    if google_code:
        return GOOGLE_REASONS.get(google_code, PROVIDER_FAILED), GOOGLE
    sp = speech(HostView.build(cfg), agent)
    return (sp["silent_reason"] or PROVIDER_FAILED), sp["provider"]
