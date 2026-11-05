"""Map a clip's configuration to built-in or installed custom providers.

Provider-specific credentials, voices and optional imports live here. Retry
policy and stream safety belong to tts_service.
"""
from __future__ import annotations

from .cartesia_tts import CartesiaError, synthesize as cartesia_synthesize
from .eleven_ws import ElevenWSError, synthesize_streaming
from .tts_provider import AudioSink, TTSProvider
from .voice import CARTESIA, DEEPGRAM, ELEVENLABS, GEMINI, GOOGLE, resolve_voice


def cartesia_synthesize_raw_pcm(**kwargs) -> int:
    """Lazy wrapper so importing tts_worker doesn't require websocket-client.

    Defer the optional dependency until raw-pcm delivery is selected.
    """
    from .cartesia_ws import synthesize_raw_pcm
    return synthesize_raw_pcm(**kwargs)


def _gemini_voice(cfg, raw_voice, persona: str, agent: dict | None,
                  route_voice: str | None, cartesia_voice: str | None) -> str | None:
    """Gemini voice for this clip, or None when the agent has none.

    A helper speaking with a contact's Cartesia voice keeps that contact's
    Gemini voice. The default voice is only for agents moved to Gemini on
    purpose; a contact with no voice stays silent, as it would under Cartesia.
    """
    return (route_voice or resolve_voice(raw_voice, GEMINI)
            or cfg.gemini_voice_for(persona)
            or cfg.gemini_voice_for_cartesia(cartesia_voice)
            or (cfg.gemini_voice
                if cfg.tts_override_for(agent).get("provider") == GEMINI else None))


def _google_voice(raw_voice: str | None, route_voice: str | None) -> str:
    """The agent's Google voice: its [tts.agents] google_voice, else its stored google key, else a
    bare stored id that is a Google voice (a preview passes the voice it auditions). Only an agent
    with no Google voice at all gets the documented default for a new choice. A saved voice off
    the allowlist is returned as it is, so synthesis refuses it (voice_unavailable) and it is
    never replaced by the default."""
    from . import google_tts
    saved = route_voice or resolve_voice(raw_voice, GOOGLE)
    if saved:
        return saved
    if raw_voice and raw_voice.strip() in google_tts.VOICES:
        return raw_voice.strip()
    return google_tts.DEFAULT_VOICE


class ProviderRegistry:
    """Resolve voices once for one clip, keeping fallback voices provider-specific."""

    def __init__(self, cfg, raw_voice, agent, route, *, providers=None):
        self.cfg, self.raw_voice, self.agent, self.route = cfg, raw_voice, agent, route
        self.providers = providers or {}
        self.persona = (agent or {}).get("persona", "")
        self.cartesia_voice = self.voice_for(CARTESIA) or cfg.cartesia_voice_for(self.persona)
        self.eleven_voice = self.voice_for(ELEVENLABS) or raw_voice

    def voice_for(self, selected):
        if selected == self.route.provider and self.route.voice:
            return self.route.voice
        return resolve_voice(self.raw_voice, selected)

    def get(self, selected: str) -> TTSProvider:
        if selected in self.providers:
            return self.providers[selected]
        return ConfiguredProvider(selected, self)


class ConfiguredProvider:
    def __init__(self, selected, registry):
        self.id, self.registry = selected, registry
        self.voice = None

    @property
    def available(self) -> bool:
        """False only for a provider whose missing key/voice is known locally.

        Other providers validate their credentials at synthesis time; this does
        not contact providers or replace their authoritative error handling.
        """
        if self.id == CARTESIA:
            return bool(self.registry.cartesia_voice and self.registry.cfg.cartesia_key())
        if self.id == ELEVENLABS:
            return bool(self.registry.eleven_voice and self.registry.cfg.eleven_key())
        return True

    def synthesize(self, text: str, sink: AudioSink) -> int:
        selected, cfg = self.id, self.registry.cfg
        raw_voice, agent = self.registry.raw_voice, self.registry.agent
        persona, clip_route = self.registry.persona, self.registry.route
        provider = clip_route.provider
        out_path, on_chunk, trace_id = sink.out_path, sink.on_chunk, sink.trace_id
        if selected == CARTESIA:
            if not (self.registry.cartesia_voice and cfg.cartesia_key()):
                raise CartesiaError("Cartesia key or voice is not configured")
            self.voice = self.registry.cartesia_voice
            return cartesia_synthesize(
                text=text, voice_id=self.registry.cartesia_voice, out_path=out_path,
                api_key=cfg.cartesia_key(), model=cfg.cartesia_model,
                on_chunk=on_chunk, trace_id=trace_id)
        if selected == ELEVENLABS:
            if not (self.registry.eleven_voice and cfg.eleven_key()):
                raise ElevenWSError("ElevenLabs key or voice is not configured")
            self.voice = self.registry.eleven_voice
            return synthesize_streaming(
                text=text, voice_id=self.registry.eleven_voice, out_path=out_path,
                api_key=cfg.eleven_key(), model=cfg.eleven_model,
                speed=cfg.eleven_speed, on_chunk=on_chunk,
                trace_id=trace_id)
        if selected == GEMINI:
            from .gemini_tts import GeminiTTSError, synthesize as gemini_synthesize
            gemini_voice = _gemini_voice(
                cfg, raw_voice, persona, agent,
                clip_route.voice if provider == GEMINI else None, self.registry.cartesia_voice)
            if not gemini_voice:
                raise GeminiTTSError(f"no Gemini voice configured for {persona!r}")
            self.voice = gemini_voice
            return gemini_synthesize(
                text=text,
                voice=cfg.gemini_tts_voice(gemini_voice),
                out_path=out_path, api_key=cfg.gemini_tts_key(),
                backend=cfg.gemini_backend,
                model=cfg.gemini_model, on_chunk=on_chunk,
                trace_id=trace_id)
        if selected == DEEPGRAM:
            from .deepgram_tts import synthesize as deepgram_synthesize
            deepgram_voice = self.registry.voice_for(DEEPGRAM) or cfg.deepgram_model
            self.voice = deepgram_voice
            return deepgram_synthesize(
                text=text, voice_id=deepgram_voice, out_path=out_path,
                api_key=cfg.deepgram_key(), on_chunk=on_chunk,
                trace_id=trace_id)
        if selected == GOOGLE:
            from . import google_tts
            # First: a legacy custom "google" adapter refuses built-in Google before any voice is
            # interpreted or credentials are built (raises GoogleIdCollision, str = the bare code).
            google_tts.refuse_if_collision(cfg)
            google_voice = _google_voice(raw_voice, clip_route.voice if provider == GOOGLE else None)
            self.voice = google_voice
            return google_tts.synthesize(
                text=text, voice=google_voice, out_path=out_path,
                creds=google_tts.credentials(cfg), on_chunk=on_chunk)
        from .custom_tts_adapters import get as custom_adapter
        from .tts_providers import VALID_IDS, synthesize as provider_synthesize
        manifest = custom_adapter(selected, reserved_ids=VALID_IDS)
        if manifest is not None:
            adapter_voice = self.registry.voice_for(selected) or manifest.default_voice
            self.voice = adapter_voice
            return provider_synthesize(
                selected, text=text, voice=adapter_voice,
                out_path=out_path, on_chunk=on_chunk)
        raise RuntimeError(f"unsupported server TTS provider: {selected}")
