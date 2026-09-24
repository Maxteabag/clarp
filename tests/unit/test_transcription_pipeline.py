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
