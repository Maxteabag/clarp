"""Server-side TTS worker. Drains tts_queue and does the actual synthesis.

Lives inside the main server process as a daemon thread. The hooks put
work on the queue and exit in ~5ms; this thread holds the long-lived
ElevenLabs connection, retries on transient failures, and is the one
place rate limiting can live.

Why this exists: see the architecture-review thread. tl;dr — moving TTS
out of hook subprocesses removes the pane-resolution gymnastics, gives
us connection pooling, lets us enforce a cost ceiling, and decouples
audio latency from the user's critical path.
"""
from __future__ import annotations

import pathlib
import threading
from dataclasses import dataclass
from typing import Any

from . import agents as agents_db
from . import health
from . import tts_queue
from .clip_delivery import ClipDelivery, ClipDeliverySession
from .clip_delivery.chunked_file import ChunkedFileDelivery
from .cartesia_tts import CartesiaError
from .cartesia_tts import synthesize as cartesia_synthesize
from .config import load as load_config
from .eleven_ws import ElevenWSError, synthesize_streaming
from .log import log_exception
from .paths import RuntimePaths
from .protocol import SSEType
from .voice import (
    CARTESIA, DEEPGRAM, ELEVENLABS, resolve_voice,
)


def cartesia_synthesize_raw_pcm(**kwargs) -> int:
    """Lazy wrapper so importing tts_worker doesn't require websocket-client.

    Tests monkeypatch this symbol directly, so keep the seam at module scope
    while deferring the optional dependency until raw-pcm delivery is actually
    selected.
    """
    from .cartesia_ws import synthesize_raw_pcm
    return synthesize_raw_pcm(**kwargs)


def _event_context(row: dict, agent: dict | None = None):
    from .eventlog import EventContext
    return EventContext(
        trace_id=row.get("trace_id") or None,
        agent_id=(agent or {}).get("agent_id") or row.get("agent_id") or None,
        session=row.get("session") or None,
        backend_session_id=(agent or {}).get("backend_session_id") or None,
    )


def _emit(*a, **kw):
    try:
        from . import eventlog
        eventlog.emit(*a, **kw)
    except Exception:
        pass


def synth_one(*,
              audio_dir: pathlib.Path,
              stream: Any | None = None,
              herald: Any | None = None,
              broker: Any | None = None,
              delivery: ClipDelivery | None = None) -> bool:
    """Claim and process one queued row. Returns True if work was done,
    False if the queue was empty.

    Idempotent at the row level: synthesis happens under SYNTHESIZING
    status, success advances to DONE, failure to FAILED. Either way the
    row is no longer reclaimable.
    """
    row = tts_queue.claim_next()
    if row is None:
        return False

    queue_id = row["queue_id"]
    try:
        agent = agents_db.get_by_agent_id(row["agent_id"])
        # The queue row's trace_id is authoritative — set the agent's
        # current trace marker to match before synthesizing, so the
        # sidecar + clips row pick up the same trace_id we accepted into
        # the queue (instead of whatever the agent's marker happens to
        # hold at synthesis time).
        if agent and row.get("trace_id"):
            try:
                agents_db.set_trace(agent["agent_id"], row["trace_id"])
            except Exception as e:  # noqa: BLE001
                log_exception("ttsWorkerTraceSetFail", e,
                              detail=row["agent_id"])
        if agent:
            voice_config = load_config()
            if voice_config.tts_provider == "none":
                result = _PwaStreamResult(True)
            else:
                # WebSocket/local synthesis writes through one delivery
                # contract; provider selection stays inside _synthesize.
                active_delivery = delivery or ChunkedFileDelivery(broker=broker)
                result = _synth_pwa_via_delivery(
                    row, agent, audio_dir,
                    delivery=active_delivery,
                    stream=stream, herald=herald,
                )
        else:
            result = _PwaStreamResult(
                False, error="no registered agent for queued utterance")
    except Exception as e:  # noqa: BLE001 — never let the worker die
        log_exception("ttsWorkerSynthCrash", e, detail=row.get("text", "")[:60])
        health.mark_error("tts_worker", e)
        try:
            tts_queue.mark_failed(queue_id, str(e))
        except Exception:
            pass
        _publish_tts_error(stream, row, None, str(e))
        return True

    if not result.ok:
        tts_queue.mark_failed(queue_id, result.error)
        health.mark_error("tts_worker", result.error)
        _emit("tts_worker", "synthFail",
              context=_event_context(row, agent),
              detail={"queue_id": queue_id, "error": result.error,
                      "agent_id": row["agent_id"]})
        _publish_tts_error(stream, row, agent, result.error)
        return True

    # Look up the clip_id we just wrote so the queue row points at it.
    clip_id: int | None = None
    if getattr(result, "clip_id", None):
        clip_id = result.clip_id
    elif result.target:
        try:
            clip_id = _clip_id_for_path(str(result.target))
        except Exception as e:
            log_exception("ttsWorkerClipIdLookupFail", e,
                          detail=str(result.target))

    tts_queue.mark_done(queue_id, clip_id=clip_id)
    _emit("tts_worker", "synthOk",
          context=_event_context(row, agent),
          clip_url=(f"/audio/{result.target.name}" if result.target else None),
          detail={"queue_id": queue_id, "agent_id": row["agent_id"],
                  "clip_id": clip_id})
    health.mark_success("tts_worker")
    return True


def _clip_id_for_path(path: str) -> int | None:
    from .db import conn
    row = conn().execute(
        "SELECT clip_id FROM clips WHERE path = ? ORDER BY clip_id DESC LIMIT 1",
        (path,),
    ).fetchone()
    return int(row["clip_id"]) if row else None


@dataclass
class _PwaStreamResult:
    """One synthesis outcome: synth_one's downstream code (mark_done,
    broadcast) reads only these fields."""
    ok: bool
    target: pathlib.Path | None = None
    error: str = ""
    clip_id: int | None = None


def _synthesize(*, cfg, row: dict, agent: dict,
                out_path, on_chunk, trace_id,
                delivery_fields: dict | None = None) -> int:
    """Provider dispatch for one clip.

    Cartesia (Sonic) is primary; ElevenLabs is the backup. The agent's
    stored voice_id may carry a per-provider map (see lib.voice) — we
    resolve the right id for each provider, falling back to the persona
    keyed Cartesia map for agents whose voice_id predates it.

    On a Cartesia failure (or a persona with no Cartesia voice / no key)
    we transparently fall back to ElevenLabs. The exception only escapes
    when the fallback also fails, so the caller surfaces a real outage.
    """
    raw_voice = row["voice_id"]
    persona = (agent or {}).get("persona", "")
    text = row["text"]

    cartesia_voice = (resolve_voice(raw_voice, CARTESIA)
                      or cfg.cartesia_voice_for(persona))
    eleven_voice = resolve_voice(raw_voice, ELEVENLABS) or raw_voice

    provider = cfg.tts_provider
    want_cartesia = (provider == CARTESIA
                     and cartesia_voice and cfg.cartesia_key())
    wants_raw_pcm = (delivery_fields or {}).get("delivery") == "raw-pcm"
    if wants_raw_pcm:
        if not want_cartesia:
            raise CartesiaError(
                "raw-pcm delivery requires Cartesia provider, key, and voice"
            )
        return cartesia_synthesize_raw_pcm(
            text=text,
            voice_id=cartesia_voice,
            out_path=out_path,
            api_key=cfg.cartesia_key(),
            model=cfg.cartesia_model,
            on_chunk=on_chunk,
            trace_id=trace_id,
            encoding=getattr(cfg, "raw_pcm_encoding", "pcm_f32le"),
            sample_rate=getattr(cfg, "raw_pcm_sample_rate", 44100),
        )

    def run(selected: str) -> int:
        if selected == CARTESIA:
            if not (cartesia_voice and cfg.cartesia_key()):
                raise CartesiaError("Cartesia key or voice is not configured")
            return cartesia_synthesize(
                text=text, voice_id=cartesia_voice, out_path=out_path,
                api_key=cfg.cartesia_key(), model=cfg.cartesia_model,
                on_chunk=on_chunk, trace_id=trace_id)
        if selected == ELEVENLABS:
            if not (eleven_voice and cfg.eleven_key()):
                raise ElevenWSError("ElevenLabs key or voice is not configured")
            return synthesize_streaming(
                text=text, voice_id=eleven_voice, out_path=out_path,
                api_key=cfg.eleven_key(), model=cfg.eleven_model,
                speed=cfg.eleven_speed, on_chunk=on_chunk,
                trace_id=trace_id)
        if selected == DEEPGRAM:
            from .deepgram_tts import synthesize as deepgram_synthesize
            deepgram_voice = (
                resolve_voice(raw_voice, DEEPGRAM) or cfg.deepgram_model)
            return deepgram_synthesize(
                text=text, voice_id=deepgram_voice, out_path=out_path,
                api_key=cfg.deepgram_key(), on_chunk=on_chunk,
                trace_id=trace_id)
        from .custom_tts_adapters import get as custom_adapter
        from .tts_providers import VALID_IDS, synthesize as provider_synthesize
        manifest = custom_adapter(selected, reserved_ids=VALID_IDS)
        if manifest is not None:
            adapter_voice = (
                resolve_voice(raw_voice, selected) or manifest.default_voice)
            return provider_synthesize(
                selected, text=text, voice=adapter_voice,
                out_path=out_path, on_chunk=on_chunk)
        raise RuntimeError(f"unsupported server TTS provider: {selected}")

    try:
        return run(provider)
    except Exception as primary_error:
        fallback = cfg.tts_fallback
        if fallback in {"", "none", provider}:
            raise
        _emit("tts_worker", "providerFallback",
              context=_event_context(row, agent), level="warn",
              detail={"provider": provider, "error": str(primary_error)[:200],
                      "fallback": fallback})
        return run(fallback)


def _synth_pwa_via_delivery(row: dict, agent: dict,
                            audio_dir: pathlib.Path,
                            *,
                            delivery: ClipDelivery,
                            stream: Any | None = None,
                            herald: Any | None = None) -> _PwaStreamResult:
    """Synthesize a PWA-mode clip via the ElevenLabs WebSocket endpoint,
    routing bytes through the configured `ClipDelivery`.

    The worker holds the universal sequence — open session, publish SSE,
    synthesize, finalize-or-fail — and is delivery-agnostic. ChunkedFile
    teaches the session to fan bytes to a broker and a file; HLS teaches
    it to pipe bytes to ffmpeg. Either way this function looks the same.
    """
    cfg = load_config()
    trace_id = row.get("trace_id")

    try:
        session = delivery.begin(
            audio_dir=audio_dir,
            agent=agent,
            voice_id=row["voice_id"],
            session=row["session"],
            source=row["source"],
            text_len=len(row["text"]),
            trace_id=trace_id,
        )
    except Exception as e:  # noqa: BLE001 — translate to a queue failure
        log_exception("clipDeliveryBeginFail", e, detail=delivery.name)
        return _PwaStreamResult(False, error=f"delivery begin failed: {e}")

    # Some deliveries broadcast at begin (ChunkedFile — client subscribes
    # to a live stream that grows). Others must wait until finalize (HLS —
    # the playlist must have #EXT-X-ENDLIST before iOS Safari can play it
    # reliably). Sessions advertise their preference via publish_after_finalize.
    publish_late = getattr(session, "publish_after_finalize", False)
    if not publish_late:
        _publish_clip_event(
            stream=stream, herald=herald,
            session=session, row=row, agent=agent, trace_id=trace_id,
        )

    try:
        bytes_written = _synthesize(
            cfg=cfg,
            row=row,
            agent=agent,
            out_path=session.target_path,
            on_chunk=session.feed,
            trace_id=trace_id,
            delivery_fields=session.sse_fields,
        )
    except Exception as e:  # provider adapters normalize failures to queue state
        session.fail(str(e))
        return _PwaStreamResult(False, error=str(e))

    final = session.finalize(total_bytes=bytes_written)

    if publish_late:
        _publish_clip_event(
            stream=stream, herald=herald,
            session=session, row=row, agent=agent, trace_id=trace_id,
        )

    return _PwaStreamResult(
        True, target=final.path, clip_id=final.clip_id,
    )


def _publish_tts_error(stream: Any | None, row: dict,
                       agent: dict | None, error: str | None) -> None:
    """Broadcast a tts-error SSE so a failed synthesis isn't just silent audio.
    Maps the raw error to a short human message; keeps the detail for logs."""
    if stream is None:
        return
    low = (error or "").lower()
    if "quota" in low or "401" in low:
        message = "Voice paused: ElevenLabs quota exceeded."
    elif "api_key" in low or "not configured" in low:
        message = "Voice unavailable: TTS not configured."
    else:
        message = "Voice synthesis failed."
    try:
        stream.broadcast({
            "type": SSEType.TTS_ERROR,
            "session": row.get("session"),
            "agent_id": row.get("agent_id"),
            "persona": (agent or {}).get("persona"),
            "message": message,
            "error": (error or "")[:300],
        })
    except Exception as e:  # noqa: BLE001
        log_exception("ttsErrorBroadcastFail", e, detail=message)


def _publish_clip_event(*, stream: Any | None, herald: Any | None,
                        session: ClipDeliverySession,
                        row: dict, agent: dict,
                        trace_id: str | None) -> None:
    """Publish the live clip event directly from the producer.

    The old path waited for AudioStream's directory watcher to rediscover
    the mp3. This keeps SQLite/worker state authoritative AND respects the
    herald arbitration policy without round-tripping through the file
    watcher. The URL fields come from the delivery's `sse_fields` so HLS
    and chunked-file can advertise different player paths."""
    if stream is None and herald is None:
        return
    target = session.target_path
    # `url` is the plain <audio src> fallback every client can play. Chunked
    # file delivery uses /audio/<mp3>; HLS has no mp3/broker stream, so the
    # canonical playlist URL becomes both `url` and `playlist_url`.
    fields = dict(session.sse_fields)
    name = target.name if target is not None else f"clip-{session.clip_id}"
    url = (
        fields.get("url")
        or (f"/audio/{name}" if target is not None else "")
        or fields.get("playlist_url")
        or fields.get("stream_url")
        or ""
    )
    meta = {
        "clip_id": session.clip_id,
        "agent_id": agent["agent_id"],
        "persona": agent.get("persona"),
        "voice_id": row["voice_id"],
        "session": row["session"],
        "source": row["source"],
        "trace_id": trace_id,
        "text_len": len(row["text"] or ""),
        **fields,
    }
    try:
        ts_str = name.split("__", 1)[0].split(".", 1)[0]
        ts = int(ts_str) if ts_str.isdigit() else 0
    except (ValueError, AttributeError):
        ts = 0
    if herald is not None:
        try:
            herald.ingest_clip(row["session"], url=url, ts=ts, meta=meta)
            return
        except Exception as e:  # noqa: BLE001
            log_exception("ttsWorkerHeraldPublishFail", e, detail=url)
    if stream is not None:
        event = {
            "type": SSEType.AUDIO,
            "url": url,
            "name": name,
            "session": row["session"],
            "clip_id": session.clip_id,
            "agent_id": agent["agent_id"],
            "persona": agent.get("persona"),
            "trace_id": trace_id,
            **session.sse_fields,
        }
        stream.broadcast(event)


class TTSWorker:
    """Daemon thread that drains tts_queue at a fixed interval.

    Polling cadence is deliberately short (default 100ms) since the
    enqueue→synthesize handoff IS the user-perceptible audio latency.
    The worker spends most of its time sleeping; when work arrives it
    runs flat-out (back-to-back synth_one calls) until the queue drains.
    """
    DEFAULT_INTERVAL_SEC = 0.1

    def __init__(self, *,
                 audio_dir: pathlib.Path,
                 interval_sec: float = DEFAULT_INTERVAL_SEC,
                 stream: Any | None = None,
                 herald: Any | None = None,
                 broker: Any | None = None,
                 delivery: ClipDelivery | None = None):
        self.audio_dir = audio_dir
        self.interval_sec = interval_sec
        self.stream = stream
        self.herald = herald
        self.broker = broker
        # If no explicit delivery was passed, default to ChunkedFileDelivery
        # wired to the broker — this preserves today's behavior exactly.
        # `build_server` overrides with `build_from_config(cfg)` so the
        # config knob actually controls anything.
        self.delivery = delivery or ChunkedFileDelivery(broker=broker)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Reset any rows stuck in 'synthesizing' from a previous run that
        # died mid-claim. Idempotent + cheap.
        try:
            stranded = tts_queue.reset_in_flight()
            if stranded:
                _emit("tts_worker", "resetInFlight",
                      detail={"count": stranded})
        except Exception as e:  # noqa: BLE001
            log_exception("ttsWorkerResetFail", e)
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                         name="tts-worker")
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        # Tight inner loop while there's work, sleep when empty. Beats a
        # fixed-rate poll when there's a burst of clips (a long Claude
        # turn that emits 4 chunks).
        while not self._stop.is_set():
            try:
                worked = synth_one(audio_dir=self.audio_dir,
                                   stream=self.stream,
                                   herald=self.herald,
                                   broker=self.broker,
                                   delivery=self.delivery)
                health.mark_success("tts_worker")
            except Exception as e:  # noqa: BLE001
                health.mark_error("tts_worker", e)
                log_exception("ttsWorkerLoopFail", e)
                worked = False
            if worked:
                continue       # drain back-to-back
            # Sleep with stop-event short-circuit.
            if self._stop.wait(self.interval_sec):
                break


def from_paths(paths: RuntimePaths) -> TTSWorker:
    """Convenience: build a TTSWorker from the standard RuntimePaths."""
    return TTSWorker(audio_dir=paths.audio_dir)
