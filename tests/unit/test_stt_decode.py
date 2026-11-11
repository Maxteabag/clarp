"""Local Whisper decoding parameters.

Production Hosts run greedy decoding (`beam_size=1`) by default for predictable
CPU decode latency; `transcription.decode.beam_size` opts into wider beam search
on both the subprocess worker and in-process fallback paths.
"""
from __future__ import annotations

import io
import json
import threading

import pytest

from lib import settings_store, stt, stt_worker


def test_greedy_decode_is_the_default_matching_production_worker():
    assert stt.DEFAULT_BEAM_SIZE == 1
    assert stt.inference_beam_size() == 1


def test_beam_size_is_configurable():
    settings_store.set_text("transcription.decode.beam_size", "3")
    assert stt.inference_beam_size() == 3


def test_worker_and_in_process_paths_receive_the_same_beam_size():
    in_proc_beams: list[int] = []
    worker_beams: list[int] = []

    class FakeInProcModel:
        def transcribe(self, _path, **kw):
            in_proc_beams.append(kw["beam_size"])
            return iter([]), None

    class FakeWorkerModel:
        def transcribe(self, _path, **kw):
            worker_beams.append(kw["beam_size"])
            return iter([]), None

    in_proc = stt.WhisperSTT("small.en")
    in_proc._model = FakeInProcModel()

    subproc = stt.SubprocessWhisperSTT("small.en")

    class FakeProc:
        def __init__(self):
            self.stdin = io.StringIO()

        def poll(self):
            return None

        def write_and_dispatch(self):
            lines = [ln for ln in self.stdin.getvalue().splitlines() if ln.strip()]
            self.stdin = io.StringIO()
            for line in lines:
                req = json.loads(line)
                segs = stt_worker._transcribe(FakeWorkerModel(), req, subproc.language)
                with subproc._pending_lock:
                    ev, box = subproc._pending[req["id"]]
                box["segments"] = segs
                ev.set()

    fake_proc = FakeProc()

    class ImmediateFlushIO(io.StringIO):
        def flush(self):
            super().flush()
            threading.Thread(target=fake_proc.write_and_dispatch, daemon=True).start()

    fake_proc.stdin = ImmediateFlushIO()
    subproc._proc = fake_proc

    in_proc._run_inference("/tmp/a.wav", "Clarp")
    fake_proc.stdin = ImmediateFlushIO()
    subproc._run_inference("/tmp/a.wav", "Clarp")

    settings_store.set_text("transcription.decode.beam_size", "4")
    in_proc._run_inference("/tmp/b.wav", "Clarp")
    fake_proc.stdin = ImmediateFlushIO()
    subproc._run_inference("/tmp/b.wav", "Clarp")

    assert in_proc_beams == [1, 4]
    assert worker_beams == [1, 4]


@pytest.mark.parametrize("stored,expected", [
    ("0", 1),        # below the floor
    ("-4", 1),
    ("99", 8),       # above the ceiling: latency cost with no accuracy gain
])
def test_beam_size_is_clamped_to_a_usable_range(stored, expected):
    settings_store.set_text("transcription.decode.beam_size", stored)
    assert stt.inference_beam_size() == expected


def test_a_nonsense_setting_falls_back_to_the_default(monkeypatch):
    """A mistyped value must not stall a turn."""
    settings_store.set_text("transcription.decode.beam_size", "fast please")
    assert stt.inference_beam_size() == stt.DEFAULT_BEAM_SIZE


def test_a_broken_settings_store_falls_back_to_the_default(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(settings_store, "get_int", boom)
    assert stt.inference_beam_size() == stt.DEFAULT_BEAM_SIZE
