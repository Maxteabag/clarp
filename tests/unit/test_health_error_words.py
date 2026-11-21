"""GET /diagnostics/health is read by every paired device, limited ones too. A subsystem's
last error there is the exception's type name or a fixed word, never the exception's text,
which can name a local path or carry a provider's reply. A path is planted in each
subsystem's failure and must be nowhere in the answer a limited device gets."""
from __future__ import annotations

import email.message
import json
import threading
from types import SimpleNamespace

import server as host
from lib import health

PLANTED = "/home/someone/.config/clarp/secret/credentials.json"


def _health_as_a_limited_device() -> str:
    handler = host.Handler.__new__(host.Handler)
    handler.path = "/diagnostics/health"
    handler.headers = email.message.Message()
    handler._request_device_scope = "limited"
    handler._request_auth_validated = True
    handler._request_principal = "synthetic-device"
    handler._authorized = lambda: True
    ready = threading.Event()
    ready.set()
    handler.server = SimpleNamespace(
        ctx=SimpleNamespace(stt=SimpleNamespace(ready=ready), deployed_version=lambda: "test"),
        boot_config=SimpleNamespace(delivery="chunked"))
    sent = []
    handler._send = lambda status, body, *_a, **_k: sent.append((status, body))
    handler._json_error = lambda status, message: sent.append((status, message))
    handler.do_GET()
    (status, body), = sent
    assert status == 200
    return body.decode() if isinstance(body, bytes) else str(body)


def _fails_with_the_path(*_a, **_k):
    raise RuntimeError(f"could not open {PLANTED}")


def test_no_subsystems_error_text_reaches_a_limited_device(monkeypatch, tmp_path):
    from lib import agents as agents_db, audio_stream, db, tts_queue, tts_worker
    from lib import transcript_streamer
    from lib import transcription_pipeline as tp
    health.reset_for_tests()

    # sse: recording a broadcast event fails.
    monkeypatch.setattr(agents_db, "record_sse_event", _fails_with_the_path)
    audio_stream.AudioStream(tmp_path / "audio").broadcast({"type": "agent-roster"})
    monkeypatch.undo()

    # transcript_streamer: the runtime lookup fails.
    monkeypatch.setattr(db, "conn", _fails_with_the_path)
    assert transcript_streamer._live_backend_session("a1") is None
    monkeypatch.undo()

    # tts_worker, an exception while synthesizing.
    monkeypatch.setattr(agents_db, "get_by_agent_id", _fails_with_the_path)
    monkeypatch.setattr(tts_queue, "mark_failed", lambda *a, **k: None)
    row = {"queue_id": 1, "agent_id": "a1", "text": "hei", "session": "s1"}
    assert tts_worker.synth_one(audio_dir=tmp_path, claim=lambda: dict(row))
    first_tts = health.snapshot()["tts_worker"]["last_error"]
    monkeypatch.undo()

    # tts_worker, a provider's failure text in the result.
    monkeypatch.setattr(agents_db, "get_by_agent_id", lambda agent_id: None)
    monkeypatch.setattr(tts_worker, "_PwaStreamResult",
                        lambda ok, error="", **kw: SimpleNamespace(
                            ok=False, error=f"provider said {PLANTED}", cancelled=False))
    monkeypatch.setattr(tts_queue, "mark_failed", lambda *a, **k: None)
    assert tts_worker.synth_one(audio_dir=tmp_path, claim=lambda: dict(row))
    monkeypatch.undo()

    # stt: a transcription that fails (500).
    class Failing:
        ready = threading.Event()

        def transcribe_bytes(self, *a, **k):
            raise RuntimeError(f"could not load transcription model: {PLANTED}")
    Failing.ready.set()
    monkeypatch.setattr(tp, "_cache_dir", lambda: tmp_path / "cache")
    out = tp.transcribe(
        SimpleNamespace(stt=Failing(), default_session="s1", vocab_prompt=lambda *, delegated: ""),
        audio_bytes=b"audio", ctype="audio/webm", requested_model="", transcription_id="job-1",
        fingerprint="fp-1", record_voice=lambda *a, **k: None,
        spawn_voice_metrics=lambda *a, **k: None, focus_session=lambda: "", hands_free=False,
        utterance_id="u1", client_ts=1)
    assert out.status == 500

    shown = json.loads(_health_as_a_limited_device())["subsystems"]
    assert {name for name in ("sse", "transcript_streamer", "tts_worker", "stt")} <= set(shown)
    assert first_tts == "RuntimeError"
    assert shown["sse"]["last_error"] == "RuntimeError"
    assert shown["transcript_streamer"]["last_error"] == "RuntimeError"
    assert shown["tts_worker"]["last_error"] == "speech failed"
    assert shown["stt"]["last_error"] == "transcription failed"
    assert PLANTED not in json.dumps(shown) and "secret" not in _health_as_a_limited_device()
