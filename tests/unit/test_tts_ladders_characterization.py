"""S7 step 3, G0 (with tests/unit/test_tts_routing_goldens.py): the paths of
_synth_pwa_via_delivery and ElevenLabsEngine.synthesize the ladder table does
not reach, pinned before the TTSProvider/service refactor."""
from __future__ import annotations

import os
import pathlib

import pytest

from lib import config, tts_engine, tts_worker
from lib.clip_delivery import FinalizeResult

ROW = {"voice_id": "v", "session": "ada", "source": "pwa", "text": "hello", "trace_id": "t1",
       "agent_id": "a1", "queue_id": 1}
AGENT = {"agent_id": "a1", "persona": "Ada", "session": "ada"}


class _Session:
    def __init__(self, target, *, late, log):
        self.target_path = target
        self.clip_id = 7
        self.sse_fields = {"streamable": True}
        self.publish_after_finalize = late
        self.log = log

    def feed(self, index, chunk):
        self.log.append(("feed", index))

    def finalize(self, *, total_bytes):
        self.log.append(("finalize", total_bytes))
        return FinalizeResult(path=self.target_path, clip_id=self.clip_id, sse_url="/x", ok=True)

    def fail(self, error):
        self.log.append(("fail", error))


class _Delivery:
    name = "fake"

    def __init__(self, session=None, begin_error=None):
        self.session, self.begin_error = session, begin_error

    def begin(self, **_kw):
        if self.begin_error:
            raise self.begin_error
        return self.session


def test_a_delivery_that_cannot_begin_fails_the_clip(tmp_path):
    result = tts_worker._synth_pwa_via_delivery(
        ROW, AGENT, tmp_path, delivery=_Delivery(begin_error=RuntimeError("no disk")))
    assert (result.ok, result.error) == (False, "delivery begin failed: no disk")


@pytest.mark.parametrize("late", [False, True])
def test_the_clip_event_goes_out_before_synthesis_or_after_finalize(tmp_path, monkeypatch, late):
    log = []
    session = _Session(tmp_path / "clip.mp3", late=late, log=log)
    monkeypatch.setattr(tts_worker, "_publish_clip_event", lambda **kw: log.append(("publish",)))

    def synth(**kw):
        log.append(("synthesize",))
        kw["on_chunk"](0, b"a")
        return 5
    monkeypatch.setattr(tts_worker, "_synthesize", synth)
    result = tts_worker._synth_pwa_via_delivery(ROW, AGENT, tmp_path, delivery=_Delivery(session))
    assert (result.ok, result.target, result.clip_id) == (True, tmp_path / "clip.mp3", 7)
    expected = [("synthesize",), ("feed", 0), ("finalize", 5)]
    assert log == (expected + [("publish",)] if late else [("publish",)] + expected)


def test_a_failed_synthesis_fails_the_session_not_the_worker(tmp_path, monkeypatch):
    log = []
    session = _Session(tmp_path / "clip.mp3", late=True, log=log)
    monkeypatch.setattr(tts_worker, "_publish_clip_event", lambda **kw: log.append(("publish",)))
    monkeypatch.setattr(tts_worker, "_synthesize", lambda **kw: (_ for _ in ()).throw(RuntimeError("down")))
    result = tts_worker._synth_pwa_via_delivery(ROW, AGENT, tmp_path, delivery=_Delivery(session))
    assert (result.ok, result.error) == (False, "down")
    assert log == [("fail", "down")]


def _engine(tmp_path, monkeypatch, temporary):
    monkeypatch.setattr(config, "_CACHED", config.Config(tts_provider="elevenlabs"))
    monkeypatch.setattr(tts_engine.tempfile, "mkstemp",
                        lambda **_kw: (os.open(temporary, os.O_CREAT | os.O_RDWR, 0o600), str(temporary)))
    return tts_engine.ElevenLabsEngine(tmp_path / "audio", api_key="configured")


def test_a_broken_sidecar_or_event_log_never_fails_a_good_clip(tmp_path, monkeypatch):
    from lib import clips, eventlog
    engine = _engine(tmp_path, monkeypatch, tmp_path / "tmp.mp3")
    (tmp_path / "audio").mkdir()
    monkeypatch.setattr(tts_engine, "synthesize_to_file",
                        lambda _t, _v, out_path, **_kw: pathlib.Path(out_path).write_bytes(b"mp3"))
    monkeypatch.setattr(clips, "write_sidecar", lambda *a, **kw: (_ for _ in ()).throw(OSError("full")))
    monkeypatch.setattr(eventlog, "emit", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("log down")))
    out = engine.synthesize("hello", "voice", session="ada")
    assert out.read_bytes() == b"mp3" and out.parent == tmp_path / "audio"


def test_a_failed_clip_removes_its_temp_file_and_raises_its_own_error(tmp_path, monkeypatch):
    from lib import eventlog
    temporary = tmp_path / "tmp.mp3"
    engine = _engine(tmp_path, monkeypatch, temporary)

    def broken(_t, _v, out_path, **_kw):
        pathlib.Path(out_path).write_bytes(b"partial")
        raise RuntimeError("provider down")
    monkeypatch.setattr(tts_engine, "synthesize_to_file", broken)
    monkeypatch.setattr(eventlog, "emit_exception", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("log down")))
    with pytest.raises(RuntimeError, match="provider down"):
        engine.synthesize("hello", "voice", session="ada")
    assert not temporary.exists()
