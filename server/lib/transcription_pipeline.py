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
from .stt import (STTBusyError, STTEngineReplacedError, STTModelLoadingError,
                  STTUnknownModelError, bounded_selection_wait)
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


class _SwapRetry:
    """The engine a request started with. A transcription that finds it
    closed by a model swap runs once more on the engine published now, so a
    swap never fails the user's dictation (VP-07)."""

    _RETRIED = ("transcribe_bytes", "transcribe_model_bytes")

    def __init__(self, ctx):
        self._ctx = ctx
        self._engine = ctx.stt

    def __getattr__(self, name):
        value = getattr(self._engine, name)
        if name not in self._RETRIED or not callable(value):
            return value

        def call(*args, **kwargs):
            try:
                return value(*args, **kwargs)
            except STTEngineReplacedError:
                current = self._ctx.stt
                if current is self._engine:
                    raise
                log("sttEngineReplacedRetry", name)
                return getattr(current, name)(*args, **kwargs)
        return call


def transcribe(ctx, *, audio_bytes: bytes, ctype: str, hands_free: bool,
               requested_model: str, transcription_id: str, fingerprint: str,
               utterance_id: str = "", client_ts: int | None = None,
               record_voice: RecordVoice,
               spawn_voice_metrics: SpawnVoiceMetrics,
               focus_session: Callable[[], str] = current_focus_session,
               call_scope: dict | None = None,
               ) -> TranscriptionOutcome:
    """Run one utterance through STT and file every side effect.

    `record_voice(event, **fields)` and `spawn_voice_metrics(...)` are the
    handler's helpers; they are passed in so this module never imports
    `server`. The caller must hold `transcription_results.serialize(id)`.

    `call_scope` is a validated call identity (call_transcription.validate_scope):
    the clip's session then comes from it, never from UI focus, and language
    and engines follow the call policy with their sources reported as `stt`.
    Without it nothing here changes.
    """
    # One request sees one engine: `_activate_transcription_if_default` may
    # swap ctx.stt on another thread while this request runs.
    stt = _SwapRetry(ctx)
    if getattr(stt, "available", True) is False:
        return TranscriptionOutcome.error(503, "server transcription disabled")
    call_stt = None
    call_steps: list[dict] = []
    if call_scope is not None:
        planned = _plan_call(call_scope, requested_model, audio_bytes)
        if isinstance(planned, TranscriptionOutcome):
            return planned
        focus, call_stt, call_steps, requested_model = planned
    else:
        focus = focus_session()
    requested_model, agent_fallbacks = _plan_engines(
        focus=focus, requested_model=requested_model, in_call=call_stt is not None,
        audio_bytes=audio_bytes)
    if not requested_model and not stt.ready.is_set():
        return TranscriptionOutcome.error(503, "whisper model loading")
    # The trace is minted before compiling so the vocab run, the
    # transcribe event and everything downstream share one id.
    trace_id = _trace.new_trace_id()
    prompt, vocab_run_id = _vocab_prompt(
        ctx, hands_free=hands_free, focus=focus, trace_id=trace_id,
        requested_model=requested_model)
    started = time.monotonic()
    ran = _run_engines(
        stt, call_stt=call_stt, call_steps=call_steps, requested_model=requested_model,
        agent_fallbacks=agent_fallbacks, focus=focus, audio_bytes=audio_bytes, ctype=ctype,
        prompt=prompt, trace_id=trace_id, vocab_run_id=vocab_run_id,
        utterance_id=utterance_id, client_ts=client_ts, record_voice=record_voice)
    if isinstance(ran, TranscriptionOutcome):
        return ran
    text, ends_terminal, _dur, requested_model = ran
    latency_ms = int((time.monotonic() - started) * 1000)
    return _after_transcript(
        ctx, text=text, ends_terminal=ends_terminal, dur=_dur, requested_model=requested_model,
        latency_ms=latency_ms, call_stt=call_stt, focus=focus, audio_bytes=audio_bytes,
        ctype=ctype, hands_free=hands_free, trace_id=trace_id, vocab_run_id=vocab_run_id,
        transcription_id=transcription_id, fingerprint=fingerprint,
        utterance_id=utterance_id, client_ts=client_ts, record_voice=record_voice,
        spawn_voice_metrics=spawn_voice_metrics)


def _plan_call(call_scope: dict, requested_model: str, audio_bytes: bytes):
    """The engines and languages of a call-scoped clip (call_transcription),
    or the 409 that refuses it. Returns (focus, call_stt, call_steps,
    requested_model) or a TranscriptionOutcome."""
    from . import call_transcription
    focus = call_scope["session"]
    call_stt = call_transcription.resolve(focus, in_call=True, requested_model=requested_model)
    # The one policy snapshot this clip resolved, planned and reports with.
    policy = call_stt["_policy"]
    if policy["error"]:
        # A stored policy that can never work (an impossible or unreadable
        # pair, or one the global engine has since broken) is refused before
        # any provider, never treated as unset or global.
        return TranscriptionOutcome(status=409, payload={
            "ok": False, "error": "call policy invalid", "call_policy_error": policy["error"],
            "language": policy["languages"],
            "engines": [call_transcription.concrete(e) for e in policy["engines"]],
            "attempts": []})
    engines = list(call_stt["_plan"])
    if call_stt["engines"]["source"] in ("device", "global"):
        # The long-form escalation applies as it does outside a call.
        try:
            from . import audio_duration, stt_providers
            long_model = stt_providers.long_form_model_for(audio_duration.seconds(audio_bytes))
            if long_model:
                engines = [long_model]
        except Exception as e:  # noqa: BLE001
            log_exception("sttLongFormRouteFail", e)
    if policy["configured"]:
        call_steps = call_transcription.plan(engines, call_stt["language"]["value"])
    else:
        # No call policy: exactly today's engines and languages (§12.2).
        call_steps = call_transcription.unchanged_plan(engines)
    if call_stt["engines"]["source"] == "device":
        # An explicit header names the model asked for, exactly as outside
        # a call: `server-default` or `local` goes to transcribe_model_bytes
        # as sent, never swapped for the local default's own call.
        for step, named in zip(call_steps, engines):
            if step.pop("local_default", None):
                step["model"] = named
    if not any("skip" not in step for step in call_steps):
        # Fail closed and visibly: never transcribe under another language
        # while the record says the call language applied.
        return TranscriptionOutcome(status=409, payload={
            "ok": False, "error": "no compatible engine",
            "language": call_stt["language"]["value"],
            "engines": [step["engine"] for step in call_steps],
            "attempts": [{"engine": step["engine"], "outcome": "skipped", "reason": step["skip"]}
                         for step in call_steps]})
    requested_model = next(step["engine"] for step in call_steps if "skip" not in step)
    return focus, call_stt, call_steps, requested_model


def _plan_engines(*, focus: str, requested_model: str, in_call: bool,
                  audio_bytes: bytes) -> tuple[str, list[str]]:
    """Gather what decides the engine (each read logged and never fatal) and
    let stt_router decide. Returns (model, the agent's fallback engines)."""
    from . import stt_router
    # An agent's own engine is more specific than the phone's device-wide
    # model choice (sent as X-Transcription-Model), so it wins over both.
    agent_engines: list[str] = []
    try:
        from .stt_language import engines_for
        agent_engines = list(engines_for(focus) if not in_call else [])
    except Exception as e:  # noqa: BLE001
        log_exception("sttAgentEngineFail", e)
    cloud_engine = ""
    if stt_router.wants_cloud_engine(
            requested_model=requested_model, agent_engines=agent_engines, in_call=in_call):
        # A cloud engine chosen in settings stands in for the server
        # default; an explicit header from the client still wins.
        try:
            from . import stt_providers
            engine = stt_providers.selected_engine()
            if stt_providers.is_cloud_model(engine):
                cloud_engine = engine
        except Exception as e:  # noqa: BLE001
            log_exception("sttEngineSettingFail", e)
    # Long recordings escalate to the stronger model configured for them.
    # This deliberately overrides a client-pinned model too: the pin says
    # which model handles an ordinary clip, and escalation is the whole
    # point of the setting. An unreadable duration escalates nothing.
    long_model, clip_seconds = "", 0.0
    try:
        from . import audio_duration, stt_providers
        clip_seconds = audio_duration.seconds(audio_bytes)
        long_model = stt_providers.long_form_model_for(clip_seconds)
    except Exception as e:  # noqa: BLE001 - escalation never blocks STT
        log_exception("sttLongFormRouteFail", e)
    plan = stt_router.plan_engines(
        requested_model=requested_model, agent_engines=agent_engines,
        cloud_engine=cloud_engine, long_model=long_model or "", in_call=in_call)
    if plan.escalated_from is not None:
        try:
            from . import stt_providers
            log("sttLongFormRoute",
                f"{clip_seconds:.1f}s >= "
                f"{stt_providers.long_form_threshold_sec()}s → {plan.model}"
                f" (was {plan.escalated_from or 'server-default'})")
        except Exception as e:  # noqa: BLE001 - escalation never blocks STT
            # As before: an escalation that cannot be logged does not happen.
            log_exception("sttLongFormRouteFail", e)
            return plan.escalated_from, list(plan.agent_fallbacks)
    return plan.model, list(plan.agent_fallbacks)


def _vocab_prompt(ctx, *, hands_free: bool, focus: str, trace_id: str,
                  requested_model: str) -> tuple[Any, int]:
    """The vocabulary prompt for this clip and its vocab run id (0: none)."""
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
    return prompt, vocab_run_id


def _run_engines(stt, *, call_stt, call_steps, requested_model: str, agent_fallbacks: list[str],
                 focus: str, audio_bytes: bytes, ctype: str, prompt, trace_id: str,
                 vocab_run_id: int, utterance_id: str, client_ts, record_voice: RecordVoice):
    """Transcribe on the planned engines, falling back in order. Returns
    (text, ends_terminal, duration, model that ran) or the error outcome."""
    try:
        # Authoritative transcript: wait for the whisper lock rather than
        # 429 — best-effort live-transcription partials must yield to it.
        model_transcribe = getattr(stt, "transcribe_model_bytes", None)
        if call_stt is not None:
            text, ends_terminal, _dur, effective = _run_call_steps(
                stt, call_steps, call_stt, focus=focus, audio_bytes=audio_bytes, ctype=ctype,
                prompt=prompt)
            requested_model = effective
        elif requested_model and callable(model_transcribe):
            from .stt_language import bound_session
            with bound_session(focus):
                # The agent's fallback engines are tried in order when its
                # first engine fails, so one provider outage does not lose
                # the user's recording.
                for index, engine in enumerate([requested_model, *agent_fallbacks]):
                    # Only an engine with a fallback after it gives up on a
                    # variant another request is warming (VP-08).
                    selection_wait = 10.0 if index < len(agent_fallbacks) else None
                    try:
                        with bounded_selection_wait(selection_wait):
                            text, ends_terminal, _dur = model_transcribe(
                                engine, audio_bytes, ctype, prompt, wait=10.0)
                        break
                    except Exception as e:  # noqa: BLE001 - busy, loading or missing too (VP-09)
                        if index == len(agent_fallbacks):
                            raise
                        log_exception("sttAgentEngineFallback", e, detail=engine)
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
        log_exception("transcribeFail", e,
                      detail=f"model={requested_model} bytes={len(audio_bytes)}")
        # Keep the failed clip too (when retention is on), so the failure can
        # be reproduced; a successful clip is retained further down.
        try:
            from . import heard_audio
            heard_audio.retain(
                _cache_dir(), trace_id=trace_id, audio_bytes=audio_bytes,
                content_type=ctype, session=focus, run_id=vocab_run_id,
                model=f"{requested_model or ''} (failed)")
        except Exception as retain_error:  # noqa: BLE001
            log_exception("heardAudioRetainFail", retain_error)
        record_voice("error", utterance_id=utterance_id or None,
                     client_ts=client_ts,
                     detail={"message": str(e)[:300], "stage": "stt"})
        failed = TranscriptionOutcome.error(500, str(e), trace_id)
        if call_stt is not None and "report" in call_stt:
            failed.payload["stt"] = call_stt["report"]          # every attempt, not hidden
        return failed
    return text, ends_terminal, _dur, requested_model


def _after_transcript(ctx, *, text: str, ends_terminal: bool, dur, requested_model: str,
                      latency_ms: int, call_stt, focus: str, audio_bytes: bytes, ctype: str,
                      hands_free: bool, trace_id: str, vocab_run_id: int, transcription_id: str,
                      fingerprint: str, utterance_id: str, client_ts,
                      record_voice: RecordVoice,
                      spawn_voice_metrics: SpawnVoiceMetrics) -> TranscriptionOutcome:
    """Everything a transcript sets off: the heard-audio copy, the vocab run's
    result, herald grants, the trace, events and voice metrics, and the stored
    result the client polls."""
    from . import transcription_results
    _dur = dur
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
                          "stt_latency_ms": latency_ms,
                          **({"stt": call_stt["report"]} if call_stt is not None else {})})
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
    if call_stt is not None:
        # Call-scoped only: what was asked for and what ran, field by field.
        response["stt"] = call_stt["report"]
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


def _run_call_steps(stt, steps: list[dict], call_stt: dict, *, focus: str, audio_bytes: bytes,
                    ctype: str, prompt) -> tuple[str, bool, float, str]:
    """Run a call-scoped clip through its planned engines in order: the next
    only on failure, an incompatible one skipped, every attempt recorded in
    ``call_stt["report"]``. Returns (text, ends_terminal, duration, engine)."""
    from .stt_language import bound_language, bound_session, capturing_handed
    attempts: list[dict] = []
    report = {"language": call_stt["language"], "engines": call_stt["engines"],
              "effective_engine": None, "attempts": attempts, "language_adapted": None,
              "requested_model": call_stt["requested_model"]}
    call_stt["report"] = report
    runnable = [step for step in steps if "skip" not in step]
    model_transcribe = getattr(stt, "transcribe_model_bytes", None)
    for step in steps:
        if "skip" in step:
            attempts.append({"engine": step["engine"], "outcome": "skipped", "reason": step["skip"]})
            continue
        started = time.monotonic()
        # Each attempt records only what its own engine boundary was handed
        # (null when it was unobserved); nothing carries over to a fallback.
        handed: dict = {}
        try:
            selection_wait = None if step is runnable[-1] else 10.0  # VP-08, as above
            with bound_session(focus), bound_language(step["language"]), capturing_handed() as handed, \
                    bounded_selection_wait(selection_wait):
                if step.get("local_default"):
                    if not stt.ready.is_set():
                        raise STTModelLoadingError("whisper model loading")
                    text, ends_terminal, dur = stt.transcribe_bytes(audio_bytes, ctype, prompt, wait=10.0)
                elif callable(model_transcribe):
                    text, ends_terminal, dur = model_transcribe(
                        step.get("model", step["engine"]), audio_bytes, ctype, prompt, wait=10.0)
                elif step.get("model") == "server-default":
                    text, ends_terminal, dur = stt.transcribe_bytes(audio_bytes, ctype, prompt, wait=10.0)
                else:
                    raise STTUnknownModelError(
                        f"transcription model not installed: {step.get('model', step['engine'])}")
        except Exception as e:  # noqa: BLE001 - the next engine, or the caller's error (VP-09)
            attempts.append({"engine": step["engine"], "outcome": "error", "language": handed.get("language"),
                             "ms": int((time.monotonic() - started) * 1000), "reason": str(e)[:200]})
            if step is runnable[-1]:
                raise
            log_exception("sttCallEngineFallback", e, detail=step["engine"])
            continue
        # The language handed at the engine boundary (§12.1: never claim more).
        attempts.append({"engine": step["engine"], "outcome": "ok", "language": handed.get("language"),
                         "ms": int((time.monotonic() - started) * 1000)})
        report["effective_engine"] = step["engine"]
        report["language_adapted"] = "first_code" if step["adapted"] else None
        return text, ends_terminal, dur, step["engine"]
    raise RuntimeError("no transcription engine ran")
