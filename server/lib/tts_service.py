"""One clip's TTS ladder, including the no-fallback-after-audio rule."""
from __future__ import annotations

from dataclasses import replace

from . import tts_mode
from .cartesia_tts import CartesiaError
from .tts_provider import AudioSink
from .tts_registry import ProviderRegistry, cartesia_synthesize_raw_pcm
from .voice import CARTESIA, GEMINI
from .voice_markup import strip_emotion_tags


def synth(text, route, sink: AudioSink, purpose, *, cfg, raw_voice, agent, emit, context):
    """Speak into a sink; purpose identifies the caller without changing its policy.

    Raw PCM remains Cartesia-only. File/stream clips retry only before a
    chunk has reached their sink. Events keep their existing worker source.
    """
    spoken, plain = text, strip_emotion_tags(text)
    if route is None:
        route = tts_mode.Route(cfg.tts_provider, None, tts_mode.fallback_for(cfg, cfg.tts_provider))
    registry = ProviderRegistry(cfg, raw_voice, agent, route)
    provider = route.provider
    cartesia_voice = registry.cartesia_voice
    want_cartesia = provider == CARTESIA and cartesia_voice and cfg.cartesia_key()
    if (sink.delivery_fields or {}).get("delivery") == "raw-pcm":
        if not want_cartesia:
            raise CartesiaError("raw-pcm delivery requires Cartesia provider, key, and voice")
        return cartesia_synthesize_raw_pcm(
            text=plain, voice_id=cartesia_voice, out_path=sink.out_path,
            api_key=cfg.cartesia_key(), model=cfg.cartesia_model,
            on_chunk=sink.on_chunk, trace_id=sink.trace_id,
            encoding=getattr(cfg, "raw_pcm_encoding", "pcm_f32le"),
            sample_rate=getattr(cfg, "raw_pcm_sample_rate", 44100))

    fed = []
    if sink.on_chunk is not None:
        live_feed = sink.on_chunk

        def on_chunk(index, chunk):
            fed[:] = [True]
            live_feed(index, chunk)

        sink = replace(sink, on_chunk=on_chunk)

    selected = registry.get(provider)
    try:
        written = selected.synthesize(spoken if provider == GEMINI else plain, sink)
    except Exception as primary_error:
        fallback = route.fallback
        if fallback in {"", "none", provider}:
            raise
        if fed:
            emit("tts_worker", "providerFallbackSkipped", context=context(), level="warn",
                 detail={"provider": provider, "error": str(primary_error)[:200],
                         "fallback": fallback, "reason": "audio_streamed"})
            raise
        emit("tts_worker", "providerFallback", context=context(), level="warn",
             detail={"provider": provider, "error": str(primary_error)[:200], "fallback": fallback})
        selected = registry.get(fallback)
        written = selected.synthesize(spoken if fallback == GEMINI else plain, sink)
        emit("tts_worker", "spoke", context=context(),
             detail={"provider": fallback, "voice": selected.voice, "fallback_for": provider})
        return written
    emit("tts_worker", "spoke", context=context(),
         detail={"provider": provider, "voice": selected.voice})
    return written
