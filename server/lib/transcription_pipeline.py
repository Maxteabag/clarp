"""The uncached half of POST /transcribe.

`server.py` owns the HTTP plumbing (body read, size limits, idempotent result
lookup, response writing). Everything between "we have audio bytes that were
not transcribed before" and "here is the JSON body and status" lives here so
it can be exercised without a live handler: engine selection, long-form
escalation, vocabulary biasing, the STT call with its error mapping, heard
audio retention, herald grant/decline consumption, trace stamping, the event
log and voice timeline rows, and the durable idempotency store.
"""
from __future__ import annotations

import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import agents as agents_db
from . import eventlog, health
from . import trace as _trace
from .log import log, log_exception
from .paths import RuntimePaths
from .stt import STTBusyError, STTModelLoadingError, STTUnknownModelError
from .focus import current_focus_session

RecordVoice = Callable[..., None]
SpawnVoiceMetrics = Callable[..., None]


@dataclass(frozen=True)
class TranscriptionOutcome:
    """What the handler sends back: `_json(status, payload)`.

    `trace_id` is the id minted for this utterance, or None when the request
    was refused before one existed; the handler stamps it on the request so
    the access-log row links to the turn.
    """
    status: int
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None

    @classmethod
    def error(cls, status: int, message: str,
              trace_id: str | None = None) -> "TranscriptionOutcome":
        return cls(status=status, payload={"error": message}, trace_id=trace_id)


def _cache_dir() -> pathlib.Path:
    return RuntimePaths.from_home(pathlib.Path.home()).cache_dir


def transcribe(ctx, *, audio_bytes: bytes, ctype: str, hands_free: bool,
               requested_model: str, transcription_id: str, fingerprint: str,
               utterance_id: str = "", client_ts: int | None = None,
               record_voice: RecordVoice,
               spawn_voice_metrics: SpawnVoiceMetrics,
               focus_session: Callable[[], str] = current_focus_session,
               ) -> TranscriptionOutcome:
    """Run one utterance through STT and file every side effect.

    `record_voice(event, **fields)` and `spawn_voice_metrics(...)` are the
    handler's helpers; they are passed in so this module never imports
    `server`. The caller must hold `transcription_results.serialize(id)`.
    """
    from . import transcription_results

    # One request sees one engine: `_activate_transcription_if_default` may
    # swap ctx.stt on another thread while this request runs.
    stt = ctx.stt
    if getattr(stt, "available", True) is False:
        return TranscriptionOutcome.error(503, "server transcription disabled")
    if not requested_model:
        # A cloud engine chosen in settings stands in for the server
        # default; an explicit header from the client still wins.
        try:
            from . import stt_providers
            engine = stt_providers.selected_engine()
            if stt_providers.is_cloud_model(engine):
                requested_model = engine
        except Exception as e:  # noqa: BLE001
            log_exception("sttEngineSettingFail", e)
    # Long recordings escalate to the stronger model configured for them.
    # This deliberately overrides a client-pinned model too: the pin says
    # which model handles an ordinary clip, and escalation is the whole
    # point of the setting. An unreadable duration escalates nothing.
    try:
        from . import audio_duration, stt_providers
        clip_seconds = audio_duration.seconds(audio_bytes)
        long_model = stt_providers.long_form_model_for(clip_seconds)
        if long_model and long_model != requested_model:
            log("sttLongFormRoute",
                f"{clip_seconds:.1f}s >= "
                f"{stt_providers.long_form_threshold_sec()}s → {long_model}"
                f" (was {requested_model or 'server-default'})")
            requested_model = long_model
    except Exception as e:  # noqa: BLE001 - escalation never blocks STT
        log_exception("sttLongFormRouteFail", e)
    if not requested_model and not stt.ready.is_set():
        return TranscriptionOutcome.error(503, "whisper model loading")
    # The trace is minted before compiling so the vocab run, the
    # transcribe event and everything downstream share one id.
    trace_id = _trace.new_trace_id()
    focus = focus_session()
    vocab_run_id = 0
    vocab_fn = getattr(ctx, "vocab_for_transcription", None)
    if callable(vocab_fn):
        try:
            vocab = vocab_fn(delegated=hands_free, session=focus,
                             trace_id=trace_id,
                             requested_model=requested_model)
            prompt, vocab_run_id = vocab.payload, vocab.run_id
        except Exception as e:  # noqa: BLE001 - biasing never blocks STT
            log_exception("vocabForTranscribeFail", e)
            prompt = ctx.vocab_prompt(delegated=hands_free)
    else:
        prompt = ctx.vocab_prompt(delegated=hands_free)
    started = time.monotonic()
    try:
        # Authoritative transcript: wait for the whisper lock rather than
        # 429 — best-effort live-transcription partials must yield to it.
        model_transcribe = getattr(stt, "transcribe_model_bytes", None)
        if requested_model and callable(model_transcribe):
            text, ends_terminal, _dur = model_transcribe(
                requested_model, audio_bytes, ctype, prompt, wait=10.0)
        elif requested_model and requested_model != "server-default":
            raise STTUnknownModelError(
                f"transcription model not installed: {requested_model}")
        else:
            text, ends_terminal, _dur = stt.transcribe_bytes(
                audio_bytes, ctype, prompt, wait=10.0)
        health.mark_success("stt")
    except STTUnknownModelError as e:
        return TranscriptionOutcome.error(400, str(e), trace_id)
    except STTModelLoadingError as e:
        return TranscriptionOutcome.error(503, str(e), trace_id)
    except STTBusyError:
        health.mark_error("stt", "busy")
        record_voice("error", utterance_id=utterance_id or None,
                     client_ts=client_ts, detail={"message": "whisper busy"})
        return TranscriptionOutcome.error(429, "whisper busy", trace_id)
    except Exception as e:  # noqa: BLE001
        health.mark_error("stt", e)
        log_exception("transcribeFail", e)
        record_voice("error", utterance_id=utterance_id or None,
                     client_ts=client_ts,
                     detail={"message": str(e)[:300], "stage": "stt"})
        return TranscriptionOutcome.error(500, str(e), trace_id)
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        from . import heard_audio
        heard_audio.retain(
            _cache_dir(),
            trace_id=trace_id, audio_bytes=audio_bytes, content_type=ctype,
            session=focus, run_id=vocab_run_id, model=requested_model or "")
    except Exception as e:  # noqa: BLE001 - diagnostics never block a turn
        log_exception("heardAudioFail", e)
    if vocab_run_id:
        try:
            from . import vocab_store
            vocab_store.update_run_result(
                vocab_run_id, transcript=text, latency_ms=latency_ms)
        except Exception as e:  # noqa: BLE001
            log_exception("vocabRunUpdateFail", e)

    # Run the user's utterance against any pending heralds. Affirmatives
    # with a name release that agent's held buffer; mentions / declines
    # leave the buffer intact.
    herald = getattr(ctx, "herald", None)
    herald_consumed = False
    # Deterministic herald grants/declines must run before LLM routing.
    # If the orchestrator is unavailable, this preserves the old
    # regex/fuzzy "Yes, Bella" fallback instead of dispatching the grant
    # phrase as an agent message.
    skip_herald = False
    if herald is not None and text:
        try:
            decision = herald.on_user_text(text)
            # A grant/decline ("Yes Domi?") is a COMMAND, not a prompt —
            # it releases (or holds) the agent's buffer. Mark it consumed so
            # we don't also dispatch it to the agent (which made Domi reply
            # to "Yes Domi?" itself).
            if decision and (decision.granted or decision.declined):
                herald_consumed = True
        except Exception as e:  # noqa: BLE001
            log_exception("heraldIntentFail", e)

    # The client echoes the trace id back in subsequent /send + /clog
    # calls so the whole turn — STT, send, hook, Claude reply, TTS,
    # broadcast, play — carries one queryable id.
    if focus:
        agents_db.set_trace_for_session(focus, trace_id)
    eventlog.emit("server", "transcribe", trace_id=trace_id, session=focus or None,
                  duration_ms=int(_dur * 1000) if _dur else None,
                  detail={"text": text, "ends_terminal": ends_terminal,
                          "herald_consumed": herald_consumed,
                          "hands_free": hands_free,
                          "orchestrator_skip_herald": skip_herald,
                          "vocab_run_id": vocab_run_id or None,
                          "stt_latency_ms": latency_ms})
    record_voice(
        "transcript", session=focus or ctx.default_session or None,
        trace_id=trace_id,
        utterance_id=utterance_id or None, client_ts=client_ts,
        duration_ms=round(_dur * 1000, 1) if _dur else None, text=text,
        detail={"bytes": len(audio_bytes), "content_type": ctype,
                "hands_free": hands_free, "ends_terminal": ends_terminal,
                "herald_consumed": herald_consumed,
                "model": requested_model or "server-default"})
    spawn_voice_metrics(audio_bytes, ctype,
                        session=focus or ctx.default_session or None,
                        trace_id=trace_id, utterance_id=utterance_id or None,
                        transcript=text)

    # Blank the text when the utterance was a herald grant/decline so the
    # client's empty-text guard skips dispatch — it released the buffer,
    # it isn't a message for the agent.
    reply_text = "" if herald_consumed else text
    response = {"text": reply_text, "ends_terminal": ends_terminal,
                "trace_id": trace_id,
                "herald_consumed": herald_consumed,
                "hands_free": hands_free,
                "orchestrator_skip_herald": skip_herald,
                "vocab_run_id": vocab_run_id or None,
                "cached": False}
    try:
        transcription_results.store(
            transcription_id, fingerprint, response)
    except transcription_results.JobIDCollisionError as e:
        return TranscriptionOutcome.error(409, str(e), trace_id)
    except Exception as e:  # noqa: BLE001
        log_exception("transcriptionResultStoreFail", e)
        return TranscriptionOutcome.error(
            500, "transcription result persistence failed", trace_id)
    return TranscriptionOutcome(status=200, payload=response, trace_id=trace_id)
