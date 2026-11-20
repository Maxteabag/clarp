"""lib.transcription_pipeline.transcribe with a stub ctx: outcome mapping for
every STT error class, the response contract, and the single-engine read."""
import threading
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import health, stt_providers, transcription_results
from lib import transcription_pipeline as tp
from lib.stt import STTBusyError, STTModelLoadingError, STTUnknownModelError


class StubSTT:
    def __init__(self, text="hello there", ends_terminal=True, dur=1.5,
                 raise_=None, ready=True, available=True):
        self.text, self.ends_terminal, self.dur = text, ends_terminal, dur
        self.raise_ = raise_
        self.ready = threading.Event()
        if ready:
            self.ready.set()
        self.available = available
        self.calls = []

    def transcribe_bytes(self, audio, ctype, prompt, *, wait=0.0):
        self.calls.append(("bytes", audio, ctype, prompt, wait))
        if self.raise_:
            raise self.raise_
        return self.text, self.ends_terminal, self.dur


class ModelSTT(StubSTT):
    def transcribe_model_bytes(self, model, audio, ctype, prompt, *, wait=0.0):
        self.calls.append(("model", model, audio, ctype, prompt, wait))
        if self.raise_:
            raise self.raise_
        return self.text, self.ends_terminal, self.dur


class Recorder:
    def __init__(self):
        self.voice = []
        self.metrics = []

    def record_voice(self, event, **fields):
        self.voice.append((event, fields))

    def spawn_voice_metrics(self, audio, ctype, **fields):
        self.metrics.append((audio, ctype, fields))


def _ctx(stt, **extra):
    ns = SimpleNamespace(stt=stt, default_session="claude",
                         vocab_prompt=lambda *, delegated: f"vocab:{delegated}")
    for k, v in extra.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture(autouse=True)
def _quiet_engine_settings(monkeypatch, tmp_path):
    # No cloud engine selected, no long-form escalation, heard audio in tmp.
    monkeypatch.setattr(stt_providers, "selected_engine", lambda: "server-default")
    monkeypatch.setattr(stt_providers, "is_cloud_model", lambda engine: False)
    monkeypatch.setattr(stt_providers, "long_form_model_for", lambda seconds: "")
    monkeypatch.setattr(tp, "_cache_dir", lambda: tmp_path / "cache")


def _run(ctx, rec, *, requested_model="", focus="", job="job-1", **kw):
    fp = transcription_results.request_fingerprint(b"audio", "audio/webm",
                                                   requested_model, False)
    kw.setdefault("hands_free", False)
    kw.setdefault("utterance_id", "utt-1")
    kw.setdefault("client_ts", 123)
    return tp.transcribe(
        ctx, audio_bytes=b"audio", ctype="audio/webm",
        requested_model=requested_model, transcription_id=job, fingerprint=fp,
        record_voice=rec.record_voice, spawn_voice_metrics=rec.spawn_voice_metrics,
        focus_session=lambda: focus, **kw)


def test_success_payload_contract_and_side_effects():
    stt = StubSTT()
    rec = Recorder()
    out = _run(_ctx(stt), rec)
    assert out.status == 200
    assert isinstance(out.trace_id, str) and len(out.trace_id) == 16
    assert list(out.payload) == ["text", "ends_terminal", "trace_id",
                                 "herald_consumed", "hands_free",
                                 "orchestrator_skip_herald", "vocab_run_id",
                                 "cached"]
    assert out.payload == {
        "text": "hello there", "ends_terminal": True, "trace_id": out.trace_id,
        "herald_consumed": False, "hands_free": False,
        "orchestrator_skip_herald": False, "vocab_run_id": None, "cached": False,
    }
    # The stub got the plain vocab prompt and the authoritative wait.
    assert stt.calls[0][3] == "vocab:False" and stt.calls[0][4] == 10.0
    # Idempotency store holds the exact response.
    fp = transcription_results.request_fingerprint(b"audio", "audio/webm", "", False)
    assert transcription_results.load("job-1", fp) == out.payload
    # One transcript row, defaulting to the default session, plus metrics.
    assert [e for e, _ in rec.voice] == ["transcript"]
    fields = rec.voice[0][1]
    assert fields["session"] == "claude" and fields["trace_id"] == out.trace_id
    assert fields["utterance_id"] == "utt-1" and fields["client_ts"] == 123
    assert fields["duration_ms"] == 1500.0 and fields["text"] == "hello there"
    assert fields["detail"] == {"bytes": 5, "content_type": "audio/webm",
                                "hands_free": False, "ends_terminal": True,
                                "herald_consumed": False, "model": "server-default"}
    assert rec.metrics[0][2] == {"session": "claude", "trace_id": out.trace_id,
                                 "utterance_id": "utt-1", "transcript": "hello there"}


def test_focus_session_stamps_trace_and_rows():
    agent_id = agents_db.create_agent(persona="Rachel", voice_id="V", cwd="/tmp",
                                      session="rachel")
    rec = Recorder()
    out = _run(_ctx(StubSTT()), rec, focus="rachel")
    assert out.status == 200
    assert rec.voice[0][1]["session"] == "rachel"
    assert rec.metrics[0][2]["session"] == "rachel"
    row = agents_db.conn().execute(
        "SELECT trace_id FROM traces WHERE agent_id = ?",
        (agent_id,)).fetchone()
    assert row["trace_id"] == out.trace_id


def test_disabled_engine_is_503_without_a_trace():
    rec = Recorder()
    out = _run(_ctx(StubSTT(available=False)), rec)
    assert (out.status, out.payload, out.trace_id) == (
        503, {"error": "server transcription disabled"}, None)
    assert rec.voice == []


def test_loading_default_model_is_503():
    out = _run(_ctx(StubSTT(ready=False)), Recorder())
    assert (out.status, out.payload) == (503, {"error": "whisper model loading"})


def test_pinned_model_without_model_support_is_400():
    stt = StubSTT()
    out = _run(_ctx(stt), Recorder(), requested_model="deepgram:nova")
    assert out.status == 400
    assert out.payload == {"error": "transcription model not installed: deepgram:nova"}
    assert stt.calls == []
    assert out.trace_id


def test_pinned_model_uses_transcribe_model_bytes():
    stt = ModelSTT()
    out = _run(_ctx(stt), Recorder(), requested_model="faster-whisper:small.en")
    assert out.status == 200
    assert stt.calls[0][:2] == ("model", "faster-whisper:small.en")


def test_cloud_engine_setting_stands_in_for_server_default(monkeypatch):
    monkeypatch.setattr(stt_providers, "selected_engine", lambda: "deepgram:nova-3")
    monkeypatch.setattr(stt_providers, "is_cloud_model", lambda e: e.startswith("deepgram"))
    stt = ModelSTT(ready=False)   # local model not loaded must not matter
    rec = Recorder()
    out = _run(_ctx(stt), rec)
    assert out.status == 200
    assert stt.calls[0][:2] == ("model", "deepgram:nova-3")
    assert rec.voice[0][1]["detail"]["model"] == "deepgram:nova-3"


def test_long_form_escalation_overrides_pin(monkeypatch):
    monkeypatch.setattr(stt_providers, "long_form_model_for", lambda s: "eleven:scribe")
    monkeypatch.setattr(stt_providers, "long_form_threshold_sec", lambda: 60)
    monkeypatch.setattr("lib.audio_duration.seconds", lambda b: 90.0)
    stt = ModelSTT()
    out = _run(_ctx(stt), Recorder(), requested_model="faster-whisper:small.en")
    assert out.status == 200
    assert stt.calls[0][1] == "eleven:scribe"


@pytest.mark.parametrize("exc, status, message", [
    (STTUnknownModelError("model gone"), 400, "model gone"),
    (STTModelLoadingError("still loading"), 503, "still loading"),
])
def test_model_errors_map_without_voice_rows(exc, status, message):
    rec = Recorder()
    out = _run(_ctx(StubSTT(raise_=exc)), rec)
    assert (out.status, out.payload) == (status, {"error": message})
    assert out.trace_id
    assert rec.voice == []


def test_busy_is_429_with_error_row(monkeypatch):
    marks = []
    monkeypatch.setattr(health, "mark_error", lambda name, err: marks.append((name, err)))
    rec = Recorder()
    out = _run(_ctx(StubSTT(raise_=STTBusyError())), rec)
    assert (out.status, out.payload) == (429, {"error": "whisper busy"})
    assert marks == [("stt", "busy")]
    assert rec.voice == [("error", {"utterance_id": "utt-1", "client_ts": 123,
                                    "detail": {"message": "whisper busy"}})]
    assert rec.metrics == []


def test_unexpected_failure_is_500_with_stage(monkeypatch):
    marks = []
    monkeypatch.setattr(health, "mark_error", lambda name, err: marks.append(name))
    rec = Recorder()
    boom = RuntimeError("decoder exploded " + "x" * 400)
    out = _run(_ctx(StubSTT(raise_=boom)), rec)
    assert out.status == 500 and out.payload == {"error": str(boom)}
    assert marks == ["stt"]
    event, fields = rec.voice[0]
    assert event == "error"
    assert fields["detail"] == {"message": str(boom)[:300], "stage": "stt"}


def test_herald_grant_blanks_text_but_keeps_transcript_row():
    herald = SimpleNamespace(
        on_user_text=lambda text: SimpleNamespace(granted=True, declined=False))
    rec = Recorder()
    out = _run(_ctx(StubSTT(text="yes bella"), herald=herald), rec)
    assert out.payload["text"] == "" and out.payload["herald_consumed"] is True
    assert rec.voice[0][1]["text"] == "yes bella"
    assert rec.voice[0][1]["detail"]["herald_consumed"] is True


def test_vocab_biasing_uses_run_payload_and_records_result(monkeypatch):
    seen = {}
    monkeypatch.setattr("lib.vocab_store.update_run_result",
                        lambda run_id, **kw: seen.update(run_id=run_id, **kw))

    def vocab_for_transcription(*, delegated, session, trace_id, requested_model):
        seen["args"] = (delegated, session, requested_model)
        return SimpleNamespace(payload="biased prompt", run_id=42)

    stt = StubSTT()
    out = _run(_ctx(stt, vocab_for_transcription=vocab_for_transcription),
               Recorder(), hands_free=True, focus="claude")
    assert stt.calls[0][3] == "biased prompt"
    assert out.payload["vocab_run_id"] == 42 and out.payload["hands_free"] is True
    assert seen["args"] == (True, "claude", "")
    assert seen["run_id"] == 42 and seen["transcript"] == "hello there"


def test_vocab_failure_falls_back_to_plain_prompt():
    def broken(**kw):
        raise RuntimeError("compile failed")
    stt = StubSTT()
    out = _run(_ctx(stt, vocab_for_transcription=broken), Recorder())
    assert out.status == 200 and stt.calls[0][3] == "vocab:False"


def test_job_id_collision_on_store_is_409():
    fp_a = transcription_results.request_fingerprint(b"other", "audio/webm", "", False)
    transcription_results.store("job-1", fp_a, {"text": "old"})
    out = _run(_ctx(StubSTT()), Recorder())
    assert out.status == 409 and "error" in out.payload


def test_engine_is_read_once_per_request():
    """A swap of ctx.stt while the request runs must not hand it a second engine."""
    ctx = _ctx(StubSTT(text="first"))
    original = ctx.stt
    replacement = StubSTT(text="second", ready=False)

    def swapping_transcribe(audio, ctype, prompt, *, wait=0.0):
        ctx.stt = replacement
        return "first", True, 0.5

    original.transcribe_bytes = swapping_transcribe
    out = _run(ctx, Recorder())
    assert out.status == 200 and out.payload["text"] == "first"
    assert replacement.calls == []


class PerModelSTT(StubSTT):
    """Each model raises its own error, or answers with its own name."""

    def __init__(self, errors):
        super().__init__()
        self.errors = errors

    def transcribe_model_bytes(self, model, audio, ctype, prompt, *, wait=0.0):
        self.calls.append(("model", model))
        if self.errors.get(model):
            raise self.errors[model]
        return f"from {model}", True, 1.0


@pytest.mark.parametrize("first_error", [
    STTBusyError(), STTModelLoadingError("whisper model loading"),
    STTUnknownModelError("transcription model not installed: faster-whisper:medium")])
def test_an_agent_fallback_engine_runs_when_the_first_is_busy_loading_or_missing(
        monkeypatch, first_error):
    """VP-09: a busy, loading or missing first engine must not lose the
    recording when the agent has a fallback engine configured."""
    from lib import stt_language
    monkeypatch.setattr(stt_language, "engines_for",
                        lambda _focus: ["faster-whisper:medium", "deepgram:nova-3"])
    stt = PerModelSTT({"faster-whisper:medium": first_error})
    out = _run(_ctx(stt), Recorder(), focus="rachel")
    assert out.status == 200 and out.payload["text"] == "from deepgram:nova-3"
    assert [c[1] for c in stt.calls] == ["faster-whisper:medium", "deepgram:nova-3"]


def test_the_last_agent_engines_own_error_still_decides_the_status(monkeypatch):
    from lib import stt_language
    monkeypatch.setattr(stt_language, "engines_for",
                        lambda _focus: ["faster-whisper:medium", "deepgram:nova-3"])
    stt = PerModelSTT({"faster-whisper:medium": STTBusyError(),
                       "deepgram:nova-3": STTModelLoadingError("loading")})
    assert _run(_ctx(stt), Recorder(), focus="rachel").status == 503


@pytest.mark.parametrize("first_error", [
    STTBusyError(), STTModelLoadingError("whisper model loading"),
    STTUnknownModelError("transcription model not installed: a")])
def test_a_call_fallback_engine_runs_when_the_first_is_busy_loading_or_missing(first_error):
    stt = PerModelSTT({"a": first_error})
    steps = [{"engine": "a", "model": "a", "language": None, "adapted": False},
             {"engine": "b", "model": "b", "language": None, "adapted": False}]
    call_stt = {"language": {"value": None}, "engines": ["a", "b"], "requested_model": ""}
    text, _terminal, _dur, engine = tp._run_call_steps(
        stt, steps, call_stt, focus="", audio_bytes=b"audio", ctype="audio/webm", prompt="")
    assert (text, engine) == ("from b", "b")
    assert [a["outcome"] for a in call_stt["report"]["attempts"]] == ["error", "ok"]


class SelectionWaitSTT(PerModelSTT):
    """Records the selection-lock bound each engine attempt ran under."""

    def __init__(self, errors):
        super().__init__(errors)
        self.bounds = []

    def transcribe_model_bytes(self, model, audio, ctype, prompt, *, wait=0.0):
        from lib import stt as stt_module
        self.bounds.append((model, stt_module.current_selection_wait()))
        return super().transcribe_model_bytes(model, audio, ctype, prompt, wait=wait)


def test_only_an_engine_with_a_fallback_after_it_bounds_its_selection_wait(monkeypatch):
    # VP-08: a fallback exists, so waiting minutes for another variant is
    # worse than trying it; the last engine keeps today's unbounded wait.
    from lib import stt_language
    monkeypatch.setattr(stt_language, "engines_for",
                        lambda _focus: ["faster-whisper:medium", "faster-whisper:large-v3"])
    stt = SelectionWaitSTT({"faster-whisper:medium": STTBusyError()})
    out = _run(_ctx(stt), Recorder(), focus="rachel")
    assert out.status == 200
    assert stt.bounds == [("faster-whisper:medium", 10.0), ("faster-whisper:large-v3", None)]

    calls = SelectionWaitSTT({"a": STTBusyError()})
    steps = [{"engine": "a", "model": "a", "language": None, "adapted": False},
             {"engine": "b", "model": "b", "language": None, "adapted": False}]
    call_stt = {"language": {"value": None}, "engines": ["a", "b"], "requested_model": ""}
    tp._run_call_steps(calls, steps, call_stt, focus="", audio_bytes=b"audio",
                       ctype="audio/webm", prompt="")
    assert calls.bounds == [("a", 10.0), ("b", None)]


# R152: a recording must never fail because of a choice made in another screen, and a recording of
# the owner's voice is never sent to a provider he did not choose FOR TRANSCRIPTION (the agent's own
# engines, the Host's selected engine, the device's model), whatever keys the Host holds for speech.
def test_an_engine_that_cannot_take_the_language_set_falls_back_to_the_hosts_choice(monkeypatch):
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "elevenlabs:scribe_v2")
    stt = PerModelSTT({"deepgram:nova-3": stt_providers.LanguageSetUnsupported("set")})
    rec = Recorder()
    out = _run(_ctx(stt), rec, requested_model="deepgram:nova-3", focus="rachel")
    assert out.status == 200 and out.payload["text"] == "from elevenlabs:scribe_v2"
    assert out.payload["engine_fallback"] == {
        "from": "deepgram:nova-3", "to": "elevenlabs:scribe_v2", "reason": "language_set",
        "languages": "detected"}
    assert [c[1] for c in stt.calls] == ["deepgram:nova-3", "elevenlabs:scribe_v2"]
    assert rec.voice[-1][1]["detail"]["model"] == "elevenlabs:scribe_v2"


def _own_model(monkeypatch, model):
    from lib import config
    monkeypatch.setattr(config, "load", lambda path=None: config.Config(whisper_model=model))


def test_a_multilingual_own_engine_hears_it_when_the_host_chose_no_set_engine(monkeypatch):
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "")
    _own_model(monkeypatch, "small")
    stt = PerModelSTT({"cartesia:ink-whisper": stt_providers.LanguageSetUnsupported("set")})
    out = _run(_ctx(stt), Recorder(), requested_model="cartesia:ink-whisper", focus="rachel")
    assert out.status == 200 and out.payload["text"] == "hello there"       # transcribe_bytes
    assert out.payload["engine_fallback"]["to"] == "server-default"
    assert out.payload["engine_fallback"]["languages"] == "detected"


def test_an_english_only_own_engine_is_never_used_the_recording_is_refused_with_words(monkeypatch):
    from lib import stt_language
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "")
    monkeypatch.setattr(stt_language, "language_for", lambda session: "no,en")
    _own_model(monkeypatch, "small.en")
    stt = PerModelSTT({"deepgram:nova-3": stt_providers.LanguageSetUnsupported("set")})
    out = _run(_ctx(stt), Recorder(), requested_model="deepgram:nova-3", focus="rachel")
    assert out.status == 422
    assert out.payload["error"] == "language_set_unsupported"
    assert (out.payload["engine"], out.payload["languages"]) == ("deepgram:nova-3", ["no", "en"])
    assert out.payload["engines"] == ["elevenlabs:scribe_v2", "google:chirp_3"]
    assert "deepgram:nova-3" in out.payload["message"] and "elevenlabs:scribe_v2" in out.payload["message"]
    assert [c[0] for c in stt.calls if c[0] == "bytes"] == []               # no English nonsense


def test_an_answer_without_a_fallback_has_no_new_field():
    out = _run(_ctx(ModelSTT()), Recorder(), requested_model="deepgram:nova-3")
    assert out.status == 200 and "engine_fallback" not in out.payload


def test_only_the_language_set_refusal_falls_back(monkeypatch):
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "elevenlabs:scribe_v2")
    stt = PerModelSTT({"deepgram:nova-3": ValueError("something else")})
    assert _run(_ctx(stt), Recorder(), requested_model="deepgram:nova-3").status == 500
    assert [c[1] for c in stt.calls] == ["deepgram:nova-3"]


def test_the_set_engine_is_only_the_hosts_selected_one_never_another_keyed_provider(monkeypatch):
    from lib import config
    monkeypatch.setattr(config, "load", lambda path=None: config.Config())
    monkeypatch.setattr(stt_providers, "_key_for", lambda cfg, provider: "k")     # every key held
    monkeypatch.setattr(stt_providers, "selected_engine", lambda: "google:chirp_3")
    assert stt_providers.language_set_engine() == "google:chirp_3"
    for selected in ("deepgram:nova-3", "cartesia:ink-whisper", "local", "server-default"):
        monkeypatch.setattr(stt_providers, "selected_engine", lambda selected=selected: selected)
        assert stt_providers.language_set_engine() == ""


def test_the_refusal_is_the_typed_error(monkeypatch):
    from lib import stt_language
    monkeypatch.setattr(stt_providers, "_key_for", lambda cfg, provider: "k")
    monkeypatch.setattr(stt_language, "current_language", lambda: "no,en")
    with pytest.raises(stt_providers.LanguageSetUnsupported):
        stt_providers.transcribe("deepgram:nova-3", b"", "audio/wav", "")


class _RealCloudSTT(StubSTT):
    """Cloud models go through the real stt_providers.transcribe (its language-set check
    and its adapters, stubbed at the network edge); the own engine is recorded as local."""

    def __init__(self, received):
        super().__init__()
        self.received = received

    def transcribe_model_bytes(self, model, audio, ctype, prompt, *, wait=0.0):
        if stt_providers.provider_of(model) in stt_providers.VALID_IDS:
            text, ends, dur = stt_providers.transcribe(model, audio, ctype, prompt)
            return text or f"from {model}", ends, dur
        self.received.append("local")
        return f"from {model}", True, 1.0

    def transcribe_bytes(self, audio, ctype, prompt, *, wait=0.0):
        self.received.append("local")
        return "local text", True, 1.0


@pytest.mark.parametrize("own_model", ["small.en", "small"])
@pytest.mark.parametrize("selected", ["local", "deepgram:nova-3", "cartesia:ink-whisper",
                                      "elevenlabs:scribe_v2", "google:chirp_3"])
@pytest.mark.parametrize("device", ["deepgram:nova-3", "cartesia:ink-whisper", "cartesia:ink-2",
                                    "elevenlabs:scribe_v2", "google:chirp_3"])
def test_no_provider_outside_the_owners_choice_ever_receives_audio(monkeypatch, device, selected, own_model):
    """Every key is held; the agent is heard in no,en and has no engines of its own. Audio may
    reach only the device's model, the Host's selected engine or the Host's own engine."""
    import lib.cartesia_stt, lib.cartesia_stt_ws, lib.deepgram_stt, lib.eleven_stt, lib.google_stt
    from lib import config, stt_language
    received = []
    for provider, module in (("deepgram", lib.deepgram_stt), ("elevenlabs", lib.eleven_stt),
                             ("google", lib.google_stt), ("cartesia", lib.cartesia_stt),
                             ("cartesia", lib.cartesia_stt_ws)):
        monkeypatch.setattr(module, "transcribe",
                            lambda provider=provider, **kw: (received.append(provider), ("heard", 1.0))[1])
    monkeypatch.setattr(config, "load", lambda path=None: config.Config(
        whisper_model=own_model, google_stt_project="p"))
    monkeypatch.setattr(stt_providers, "_key_for", lambda cfg, provider: "k")
    monkeypatch.setattr(stt_providers, "selected_engine", lambda: selected)
    monkeypatch.setattr(stt_language, "language_for", lambda session: "no,en")
    monkeypatch.setattr(stt_language, "engines_for", lambda session: [])
    out = _run(_ctx(_RealCloudSTT(received)), Recorder(), requested_model=device, focus="rachel")
    chosen = {stt_providers.provider_of(device), stt_providers.provider_of(selected), "local"}
    assert set(received) <= chosen, (received, chosen)
    assert out.status in (200, 422)
    if out.status == 422:
        assert own_model == "small.en" and received == []      # refused, not English nonsense


# F1: whether the Host's own engine can hear the recording is read from its model's declared
# languages (the adapter's model list, the curated catalogue), never from its name.
def _own_adapter(monkeypatch, languages, *, model="fast"):
    from lib import config, custom_stt_adapters
    manifest = SimpleNamespace(id="myasr", default_model=model)
    monkeypatch.setattr(custom_stt_adapters, "get",
                        lambda provider: manifest if provider == "myasr" else None)
    monkeypatch.setattr(custom_stt_adapters, "models", lambda m, force=False: [
        {"id": f"myasr:{model}", "languages": list(languages)}])
    monkeypatch.setattr(config, "load", lambda path=None: config.Config(
        whisper_provider="myasr", whisper_model=model))


def test_an_own_adapter_model_that_declares_only_english_is_never_used(monkeypatch):
    from lib import stt_language
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "")
    monkeypatch.setattr(stt_language, "language_for", lambda session: "no,en")
    _own_adapter(monkeypatch, ["en"])                    # its name has no ".en"
    stt = PerModelSTT({"deepgram:nova-3": stt_providers.LanguageSetUnsupported("set")})
    out = _run(_ctx(stt), Recorder(), requested_model="deepgram:nova-3", focus="rachel")
    assert out.status == 422 and out.payload["error"] == "language_set_unsupported"
    assert out.payload["reasons"] == ["the Host's own model myasr:fast hears only en"]
    assert [c for c in stt.calls if c[0] == "bytes"] == []


def test_an_own_adapter_model_that_declares_the_agents_languages_hears_it(monkeypatch):
    from lib import stt_language
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "")
    monkeypatch.setattr(stt_language, "language_for", lambda session: "no,en")
    _own_adapter(monkeypatch, ["nb-NO", "en"], model="tiny.en")    # the name says nothing
    stt = PerModelSTT({"deepgram:nova-3": stt_providers.LanguageSetUnsupported("set")})
    out = _run(_ctx(stt), Recorder(), requested_model="deepgram:nova-3", focus="rachel")
    assert out.status == 200 and out.payload["engine_fallback"]["to"] == "server-default"


def test_the_own_engine_is_judged_by_its_declared_languages(monkeypatch):
    from lib import config, custom_stt_adapters
    cfg = lambda **kw: config.Config(**kw)
    refusal = stt_providers.own_engine_refusal
    # The curated catalogue decides a built-in model.
    assert refusal(cfg(whisper_model="small"), ["no", "en"]) == ""
    assert "hears only en" in refusal(cfg(whisper_model="small.en"), ["no", "en"])
    # A model declaring nothing (registered by hand) is judged as the engine runs it.
    assert refusal(cfg(whisper_model="distil-large-v3"), ["no", "en"]) == ""
    assert "hears only en" in refusal(cfg(whisper_model="tiny.en"), ["no", "en"])
    assert refusal(cfg(whisper_enabled=False, whisper_model="small"), ["no", "en"])
    # An adapter that cannot answer is not used.
    _own_adapter(monkeypatch, ["multilingual"])
    def down(manifest, force=False):
        raise custom_stt_adapters.AdapterError("adapter gone")
    monkeypatch.setattr(custom_stt_adapters, "models", down)
    assert "unavailable: adapter gone" in refusal(config.load(), ["no", "en"])


# F2: the fallback never sends the recording again to an engine that already failed for it.
def test_the_fallback_never_resends_to_an_engine_that_already_failed(monkeypatch):
    from lib import stt_language
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "elevenlabs:scribe_v2")
    monkeypatch.setattr(stt_language, "engines_for",
                        lambda session: ["elevenlabs:scribe_v2", "deepgram:nova-3"])
    monkeypatch.setattr(stt_language, "language_for", lambda session: "no,en")
    _own_model(monkeypatch, "small.en")
    stt = PerModelSTT({"elevenlabs:scribe_v2": RuntimeError("elevenlabs 503"),
                       "deepgram:nova-3": stt_providers.LanguageSetUnsupported("set")})
    out = _run(_ctx(stt), Recorder(), focus="rachel")
    assert [c[1] for c in stt.calls] == ["elevenlabs:scribe_v2", "deepgram:nova-3"]
    assert out.status == 422 and out.payload["error"] == "language_set_unsupported"
    assert out.payload["reasons"][0] == "elevenlabs:scribe_v2 already failed for this recording"


def test_after_a_failed_set_engine_the_own_engine_still_takes_over(monkeypatch):
    from lib import stt_language
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "elevenlabs:scribe_v2")
    monkeypatch.setattr(stt_language, "engines_for",
                        lambda session: ["elevenlabs:scribe_v2", "deepgram:nova-3"])
    _own_model(monkeypatch, "small")
    stt = PerModelSTT({"elevenlabs:scribe_v2": RuntimeError("elevenlabs 503"),
                       "deepgram:nova-3": stt_providers.LanguageSetUnsupported("set")})
    out = _run(_ctx(stt), Recorder(), focus="rachel")
    assert out.status == 200 and out.payload["engine_fallback"]["to"] == "server-default"
    assert [c[0] for c in stt.calls] == ["model", "model", "bytes"]


def test_the_own_engine_that_already_failed_is_not_asked_again(monkeypatch):
    from lib import stt_language
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "")
    monkeypatch.setattr(stt_language, "engines_for",
                        lambda session: ["server-default", "deepgram:nova-3"])
    _own_model(monkeypatch, "small")
    stt = PerModelSTT({"server-default": RuntimeError("whisper worker crashed"),
                       "deepgram:nova-3": stt_providers.LanguageSetUnsupported("set")})
    out = _run(_ctx(stt), Recorder(), focus="rachel")
    assert out.status == 422
    assert out.payload["reasons"] == ["the Host's own engine already failed for this recording"]
    assert [c[0] for c in stt.calls if c[0] == "bytes"] == []


def test_the_engine_that_just_refused_the_set_is_not_asked_again(monkeypatch):
    # Even were it the Host's selection, the engine that refused is never sent the recording again.
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "deepgram:nova-3")
    _own_model(monkeypatch, "small")
    stt = PerModelSTT({"deepgram:nova-3": stt_providers.LanguageSetUnsupported("set")})
    out = _run(_ctx(stt), Recorder(), requested_model="deepgram:nova-3", focus="rachel")
    assert out.status == 200 and out.payload["engine_fallback"]["to"] == "server-default"
    assert [c[0] for c in stt.calls] == ["model", "bytes"]


# F4: an own engine that is off or unavailable is never a 500: the typed refusal with words.
@pytest.mark.parametrize("own_error", [RuntimeError("server transcription is disabled"),
                                       RuntimeError("configured transcription model is not "
                                                    "installed: faster-whisper:small")])
def test_an_own_engine_that_is_off_or_missing_is_the_typed_refusal(monkeypatch, own_error):
    from lib import stt_language
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "")
    monkeypatch.setattr(stt_language, "language_for", lambda session: "no,en")
    _own_model(monkeypatch, "small")
    stt = PerModelSTT({"deepgram:nova-3": stt_providers.LanguageSetUnsupported("set")})
    stt.raise_ = own_error                                     # transcribe_bytes
    rec = Recorder()
    out = _run(_ctx(stt), rec, requested_model="deepgram:nova-3", focus="rachel")
    assert out.status == 422 and out.payload["error"] == "language_set_unsupported"
    assert out.payload["reasons"] == [f"the Host's own engine could not hear it: {own_error}"]
    assert str(own_error) in out.payload["message"]
    assert rec.voice[-1][1]["detail"]["message"] == "language_set_unsupported"


def test_a_disabled_own_engine_is_never_the_fallback(monkeypatch):
    from lib import config
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "")
    monkeypatch.setattr(config, "load", lambda path=None: config.Config(
        whisper_model="small", whisper_enabled=False))
    stt = PerModelSTT({"deepgram:nova-3": stt_providers.LanguageSetUnsupported("set")})
    out = _run(_ctx(stt), Recorder(), requested_model="deepgram:nova-3", focus="rachel")
    assert out.status == 422 and out.payload["reasons"] == ["the Host's own engine is off"]
    assert [c[0] for c in stt.calls if c[0] == "bytes"] == []


def test_an_own_engine_still_loading_is_the_loading_answer(monkeypatch):
    monkeypatch.setattr(stt_providers, "language_set_engine", lambda: "")
    _own_model(monkeypatch, "small")
    stt = PerModelSTT({"deepgram:nova-3": stt_providers.LanguageSetUnsupported("set")})
    stt.load_done = threading.Event()                          # not loaded yet
    out = _run(_ctx(stt), Recorder(), requested_model="deepgram:nova-3", focus="rachel")
    assert out.status == 503
    assert [c[0] for c in stt.calls if c[0] == "bytes"] == []


def test_a_selected_set_engine_without_its_key_is_never_picked(monkeypatch):
    from lib import config
    monkeypatch.setattr(config, "load", lambda path=None: config.Config())
    monkeypatch.setattr(stt_providers, "selected_engine", lambda: "google:chirp_3")
    monkeypatch.setattr(stt_providers, "_key_for", lambda cfg, provider: "")
    assert stt_providers.language_set_engine() == ""
    monkeypatch.setattr(stt_providers, "_key_for", lambda cfg, provider: "k")
    assert stt_providers.language_set_engine() == "google:chirp_3"


def test_a_fallback_to_chirp_records_the_set_it_was_handed(monkeypatch):
    import lib.google_stt
    from lib import config, stt_language
    handed = []
    monkeypatch.setattr(lib.google_stt, "transcribe",
                        lambda **kw: (handed.append(kw.get("language")), ("heard", 1.0))[1])
    monkeypatch.setattr(config, "load", lambda path=None: config.Config(
        whisper_model="small", google_stt_project="p"))
    monkeypatch.setattr(stt_providers, "_key_for", lambda cfg, provider: "k")
    monkeypatch.setattr(stt_providers, "selected_engine", lambda: "google:chirp_3")
    monkeypatch.setattr(stt_language, "language_for", lambda session: "no,en")
    out = _run(_ctx(_RealCloudSTT([])), Recorder(), requested_model="deepgram:nova-3",
               focus="rachel")
    assert out.status == 200 and out.payload["engine_fallback"] == {
        "from": "deepgram:nova-3", "to": "google:chirp_3", "reason": "language_set",
        "languages": ["no", "en"]}
    assert handed and "," in str(handed[0])
