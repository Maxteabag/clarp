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


# STT-N1: retain the harness forward EOF barrier and reverse-order control.
class HeldEOF:
    def __init__(self, stream, seen, release):
        self.stream = stream
        self.seen = seen
        self.release = release

    def __iter__(self):
        for line in self.stream:
            yield line
        self.seen.set()
        if not self.release.wait(10):
            raise AssertionError("STT-N1 control: old EOF release timed out")


@pytest.mark.parametrize("order", ["old-reader-first", "replacement-first"])
def test_retired_reader_cannot_fail_replacement_request(monkeypatch, order):
    monkeypatch.setenv("CLAUDE_PWA_STT_FAKE", "1")
    eof_seen = threading.Event()
    release_eof = threading.Event()
    reader_finished = threading.Event()

    class ControlledSTT(SubprocessWhisperSTT):
        def __init__(self):
            super().__init__("small.en", "int8", model_source="fake-model")
            self.reader_count = 0
            self.spawn_count = 0

        def _reader(self, proc, ready, outcome):
            self.reader_count += 1
            first = self.reader_count == 1
            if first:
                proc.stdout = HeldEOF(proc.stdout, eof_seen, release_eof)
            try:
                super()._reader(proc, ready, outcome)
            finally:
                if first:
                    reader_finished.set()

        def _spawn_worker(self):
            self.spawn_count += 1
            spawned = super()._spawn_worker()
            if self.spawn_count == 2 and order == "replacement-first":
                assert spawned, "STT-N1 control: replacement never became ready"
                release_eof.set()
                assert reader_finished.wait(10), "STT-N1 control: old reader stuck"
            return spawned

    stt = ControlledSTT()
    try:
        stt.start_loading()
        assert stt.load_done.wait(10), "STT-N1 control: initial worker not ready"
        assert stt.load_error is None
        first = stt._proc
        assert first is not None
        stt._kill(first)
        assert first.poll() is not None
        assert eof_seen.wait(10), "STT-N1 control: old reader never reached EOF"
        if order == "old-reader-first":
            release_eof.set()
            assert reader_finished.wait(10), "STT-N1 control: old reader stuck"
        text, _, _ = stt.transcribe_bytes(b"RIFF....", "audio/wav", "", wait=5.0)
        assert text == "fake transcript."
        assert stt._proc is not first
        assert stt._proc.poll() is None
        assert stt._fallback_in_process is False
    finally:
        release_eof.set()
        stt._kill(stt._proc)
