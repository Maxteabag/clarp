"""One clip's TTS ladder, including the no-fallback-after-audio rule."""
from __future__ import annotations

import pathlib
import subprocess
import tempfile

from dataclasses import replace

from . import tts_mode
from .cartesia_tts import CartesiaError
from .tts_provider import AudioSink
from .tts_registry import ProviderRegistry, cartesia_synthesize_raw_pcm
from .voice import CARTESIA, GEMINI
from .voice_markup import strip_emotion_tags


def event_context(row: dict, agent: dict | None = None):
    from .eventlog import EventContext
    return EventContext(
        trace_id=row.get("trace_id") or None,
        agent_id=(agent or {}).get("agent_id") or row.get("agent_id") or None,
        session=row.get("session") or None,
        backend_session_id=(agent or {}).get("backend_session_id") or None,
    )


def emit_event(*a, **kw):
    try:
        from . import eventlog
        eventlog.emit(*a, **kw)
    except Exception:
        pass


def _decode_raw_fallback(source, target, audio_format, feed, check_active) -> int:
    """Convert one encoded fallback to the already advertised raw format.

    Decode completely before feeding: malformed or timed-out decodes never
    leak an encoded header or partial decoder output into a live PCM session.
    """
    encoding = audio_format["encoding"]
    formats = {"pcm_f32le": "f32le", "pcm_s16le": "s16le"}
    if encoding not in formats or audio_format["container"] != "raw":
        raise RuntimeError("raw fallback decode: unsupported format")
    decoded = source.with_suffix(".pcm")
    check_active()
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
             "-vn", "-ac", str(audio_format["channels"]),
             "-ar", str(audio_format["sample_rate"]), "-c:a", encoding,
             "-f", formats[encoding], str(decoded)],
            check=True, timeout=15.0, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("raw fallback decode failed") from error
    check_active()
    frame_bytes = audio_format["channels"] * (4 if encoding == "pcm_f32le" else 2)
    size = decoded.stat().st_size
    if not size or size % frame_bytes:
        raise RuntimeError("raw fallback decode: empty or unaligned PCM")
    with decoded.open("rb") as src, pathlib.Path(target).open("wb") as dst:
        index = 0
        while chunk := src.read(16384 * frame_bytes):
            check_active()
            feed(index, chunk)
            check_active()
            dst.write(chunk)
            index += 1
    return size


def synth(text, route, sink: AudioSink, purpose, *, cfg, raw_voice, agent, emit, context, registry=None, primary_text=None, check_active=None):
    """Speak into a sink; purpose identifies the caller without changing its policy.

    Raw sessions try their configured fallback only before PCM is fed. File/stream clips retry only before a
    chunk has reached their sink. Events keep their existing worker source.
    primary_text preserves direct-route input without changing fallback markup policy.
    """
    spoken, plain = text, strip_emotion_tags(text)
    if route is None:
        route = tts_mode.Route(cfg.tts_provider, None, tts_mode.fallback_for(cfg, cfg.tts_provider))
    if registry is None:
        registry = ProviderRegistry(cfg, raw_voice, agent, route)
    provider = route.provider
    cartesia_voice = registry.cartesia_voice
    want_cartesia = provider == CARTESIA and cartesia_voice and cfg.cartesia_key()
    wants_raw_pcm = (sink.delivery_fields or {}).get("delivery") == "raw-pcm"

    fed = []
    if sink.on_chunk is not None:
        live_feed = sink.on_chunk

        def on_chunk(index, chunk):
            fed[:] = [True]
            live_feed(index, chunk)

        sink = replace(sink, on_chunk=on_chunk)

    def ensure_active():
        if check_active is not None:
            check_active()

    selected = registry.get(provider)
    try:
        if wants_raw_pcm:
            ensure_active()
            if not want_cartesia:
                raise CartesiaError("raw-pcm delivery requires Cartesia provider, key, and voice")
            written = cartesia_synthesize_raw_pcm(
                text=plain, voice_id=cartesia_voice, out_path=sink.out_path,
                api_key=cfg.cartesia_key(), model=cfg.cartesia_model,
                on_chunk=sink.on_chunk, trace_id=sink.trace_id,
                encoding=sink.delivery_fields["audio_format"]["encoding"],
                sample_rate=sink.delivery_fields["audio_format"]["sample_rate"])
            ensure_active()
            return written
        text_for_primary = primary_text if primary_text is not None else (spoken if provider == GEMINI else plain)
        written = selected.synthesize(text_for_primary, sink)
    except Exception as primary_error:
        if wants_raw_pcm:
            ensure_active()
        preserve_primary = purpose in {"preview", "narration"}
        if preserve_primary:
            from .google_tts import GoogleIdCollision, GoogleTTSError
            if (isinstance(primary_error, GoogleIdCollision)
                    or (isinstance(primary_error, GoogleTTSError)
                        and primary_error.code == "voice_unavailable")):
                raise  # a voice-id guard refusal, never a provider outage
        fallback = route.fallback
        if fallback in {"", "none", provider}:
            raise
        if fed:
            emit("tts_worker", "providerFallbackSkipped", context=context(), level="warn",
                 detail={"provider": provider,
                         **({"error_code": "cartesia_raw_after_audio"} if wants_raw_pcm else
                            {"error": str(primary_error)[:200]}),
                         "fallback": fallback, "reason": "audio_streamed"})
            raise
        selected = registry.get(fallback)
        if preserve_primary and not selected.available:
            emit("tts_worker", "providerFallbackFailed", context=context(), level="warn",
                 detail={"provider": provider, "fallback": fallback, "reason": "not_configured"})
            raise
        emit("tts_worker", "providerFallback", context=context(), level="warn",
             detail=({"provider": provider, "fallback": fallback,
                      "error_code": "cartesia_raw_before_audio"} if wants_raw_pcm else
                     {"provider": provider, "error": str(primary_error)[:200], "fallback": fallback}))
        try:
            if wants_raw_pcm:
                ensure_active()
                with tempfile.TemporaryDirectory(prefix="clarp-raw-fallback-") as directory:
                    encoded = pathlib.Path(directory) / "fallback.audio"
                    selected.synthesize(spoken if fallback == GEMINI else plain,
                                        replace(sink, out_path=encoded, on_chunk=None))
                    ensure_active()
                    written = _decode_raw_fallback(
                        encoded, sink.out_path, sink.delivery_fields["audio_format"],
                        sink.on_chunk, ensure_active)
            else:
                written = selected.synthesize(spoken if fallback == GEMINI else plain, sink)
        except Exception as fallback_error:
            if not preserve_primary:
                raise
            emit("tts_worker", "providerFallbackFailed", context=context(), level="warn",
                 detail={"provider": provider, "fallback": fallback, "error": str(fallback_error)[:200]})
            raise primary_error from fallback_error
        emit("tts_worker", "spoke", context=context(),
             detail={"provider": fallback, "voice": selected.voice, "fallback_for": provider})
        return written
    emit("tts_worker", "spoke", context=context(),
         detail={"provider": provider, "voice": selected.voice})
    return written


def synth_preview(text, provider, voice, out_path, *, cfg, purpose="preview", agent=None, manifest=None):
    """Write an audition/narration clip; return whether the requested primary spoke.

    A primary success may enter its existing voice cache. A fallback success
    must be served temporarily, so a different voice never poisons that cache.
    Caller validation and HTTP error mapping remain in the route.
    """
    from .tts_registry import preview_registry
    agent = agent or {}
    route = tts_mode.Route(provider, voice, tts_mode.fallback_for(cfg, provider))
    registry, primary = preview_registry(cfg, provider, voice, agent, route, manifest=manifest)

    def emit(source, event, **kwargs):
        # Preserve ordinary route-success telemetry; fallback attempts/failures
        # retain the shared service's diagnostics.
        if event != "spoke":
            emit_event(source, event, **kwargs)

    synth(text, route, AudioSink(out_path), purpose,
          cfg=cfg, raw_voice=voice, agent=agent, registry=registry, primary_text=text,
          emit=emit, context=lambda: event_context({}, agent))
    return primary.succeeded
