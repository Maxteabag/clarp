"""Whisper inference in a worker process (P6.1) — the real fix for HTTP
handler stalls during transcription. Uses the worker's fake-model mode so no
model download or faster_whisper import is needed."""
import pathlib
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.stt import SubprocessWhisperSTT, WhisperSTT  # noqa: E402


@pytest.fixture
def fake_worker_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_PWA_STT_FAKE", "1")


def test_worker_transcribes_out_of_process(fake_worker_env):
    stt = SubprocessWhisperSTT("small.en", "int8", model_source="fake-model")
    stt.start_loading()
    assert stt.load_done.wait(timeout=30), "worker never reported ready"
    assert stt.load_error is None
    assert stt.ready.is_set()
    assert stt._proc is not None and stt._proc.poll() is None
    text, ends_terminal, dur = stt.transcribe_bytes(b"RIFF....", "audio/wav", "", wait=5.0)
    assert text == "fake transcript."
    assert ends_terminal is True
    assert dur >= 0
    # Still the worker, never fell back.
    assert stt._fallback_in_process is False
    stt._kill(stt._proc)


def test_worker_respawns_after_crash(fake_worker_env):
    stt = SubprocessWhisperSTT("small.en", "int8", model_source="fake-model")
    stt.start_loading()
    assert stt.load_done.wait(timeout=30)
    first = stt._proc
    stt._kill(first)
    assert first.poll() is not None
    text, _, _ = stt.transcribe_bytes(b"RIFF....", "audio/wav", "", wait=5.0)
    assert text == "fake transcript."
    assert stt._proc is not first, "a dead worker must be respawned, not reused"
    stt._kill(stt._proc)


def test_worker_failure_falls_back_to_in_process(monkeypatch):
    """A worker that can't start must never leave voice broken: the parent
    loads the model in-process exactly as before."""
    loaded = threading.Event()

    def fake_in_process_load(self):
        self._model = object()      # stands in for a loaded model
        self.ready.set()
        self.load_done.set()
        loaded.set()

    monkeypatch.setattr(WhisperSTT, "_load", fake_in_process_load)
    monkeypatch.setattr(
        SubprocessWhisperSTT, "_worker_cmd",
        lambda self: [sys.executable, "-c", "import sys; sys.exit(3)"])
    stt = SubprocessWhisperSTT("small.en", "int8", model_source="fake-model")
    stt.start_loading()
    assert stt.load_done.wait(timeout=30)
    assert loaded.is_set(), "in-process fallback did not run"
    assert stt._fallback_in_process is True
    assert stt.ready.is_set()
    assert stt._inference_available() is True
