"""VP-07: swapping the STT engine stops the old Whisper worker and publishes
the replacement only once it has loaded."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

import server as server_module
from lib import config
from lib import stt as stt_module
from lib.stt import SubprocessWhisperSTT
from tests.unit.test_transcription_pipeline import (  # noqa: F401 - autouse fixture
    Recorder, StubSTT, _ctx, _quiet_engine_settings, _run)

# The error a closed engine raises (RuntimeError before VP-07 added it).
STTEngineReplacedError = getattr(stt_module, "STTEngineReplacedError", RuntimeError)


class _Ctx:
    def __init__(self, stt):
        self.stt = stt

    def replace_stt(self, stt):
        previous, self.stt = self.stt, stt
        return previous


class _Previous:
    default_model_id = "faster-whisper:base.en"

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _handler(monkeypatch, previous, *, load_error=None):
    published_while_loading = []

    class FakeWhisper:
        def __init__(self, model_name, compute, model_source=None):
            self.default_model_id = f"faster-whisper:{model_name}"
            self.load_done = threading.Event()
            self.load_error = None
            self.closed = False

        def start_loading(self):
            published_while_loading.append(handler.ctx.stt is self)
            self.load_error = load_error
            self.load_done.set()

        def close(self):
            self.closed = True

    class FakeUnavailable(FakeWhisper):
        pass

    monkeypatch.setattr(stt_module, "WhisperSTT", FakeWhisper)
    monkeypatch.setattr(stt_module, "UnavailableSTT", FakeUnavailable)
    monkeypatch.setattr(stt_module, "DisabledSTT", FakeUnavailable)
    monkeypatch.setattr(stt_module, "_installed_model_records", lambda: [{
        "id": "faster-whisper:small.en", "_local_path": "/model"}])
    monkeypatch.setattr(config, "load", lambda: SimpleNamespace(
        whisper_enabled=True, whisper_model="small.en", whisper_compute="int8",
        whisper_isolate=False))
    handler = object.__new__(server_module.Handler)
    handler.server = SimpleNamespace(ctx=_Ctx(previous))
    return handler, published_while_loading


def test_activation_publishes_the_loaded_replacement_and_closes_the_previous(monkeypatch):
    previous = _Previous()
    handler, published_while_loading = _handler(monkeypatch, previous)
    handler._activate_transcription_if_default("faster-whisper:small.en")
    assert published_while_loading == [False]   # the old engine served during the load
    assert handler.ctx.stt is not previous and previous.closed


def test_a_replacement_that_fails_to_load_is_closed_and_never_published(monkeypatch):
    previous = _Previous()
    handler, _ = _handler(monkeypatch, previous, load_error=RuntimeError("bad model"))
    with pytest.raises(RuntimeError, match="bad model"):
        handler._activate_transcription_if_default("faster-whisper:small.en")
    assert handler.ctx.stt is previous and not previous.closed


class _Stdin:
    def __init__(self):
        self.lines, self.closed = [], False

    def write(self, text):
        self.lines.append(text)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _Proc:
    def __init__(self):
        self.stdin = _Stdin()
        self.waited = threading.Event()

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.waited.set()
        return 0

    def kill(self):
        pass


def test_close_quits_the_worker_and_a_closed_engine_never_respawns(monkeypatch):
    stt = SubprocessWhisperSTT("small.en", "int8", model_source="fake-model")
    proc = _Proc()
    stt._proc = proc
    stt.close()
    assert [json.loads(line) for line in proc.stdin.lines] == [{"op": "quit"}]
    assert proc.stdin.closed and proc.waited.wait(5)
    monkeypatch.setattr(stt, "_spawn_worker", lambda: pytest.fail("a closed engine respawned"))
    with pytest.raises(STTEngineReplacedError):
        stt._run_inference("/tmp/clip.wav", "")


def test_a_dictation_that_meets_a_replaced_engine_runs_on_the_new_one():
    """The swap closed the engine this request started with: the request runs
    once on the engine published now instead of failing (lead's condition)."""
    fresh = StubSTT(text="from the new engine")

    class Replaced(StubSTT):
        def transcribe_bytes(self, *args, **kwargs):
            ctx.stt = fresh        # the swap published the replacement meanwhile
            raise STTEngineReplacedError("stt engine was replaced")
    ctx = _ctx(Replaced())
    out = _run(ctx, Recorder())
    assert out.status == 200 and out.payload["text"] == "from the new engine"


def test_swap_after_process_selection_retries_the_dictation():
    """A swap that closes the worker after a request chose it but before it
    sent: the request must see the engine replaced and retry (reviewer C"s
    control on 55e3b106)."""
    selected, proceed = threading.Event(), threading.Event()
    engine = SubprocessWhisperSTT("small.en", "int8", model_source="fake-model")
    proc = _Proc()

    class Pipe:
        closed = False
        def write(self, text):
            if self.closed:
                raise ValueError("I/O operation on closed file")
        def flush(self):
            if self.closed:
                raise ValueError("I/O operation on closed file")
        def close(self):
            self.closed = True
    proc.stdin = Pipe()
    engine._proc = proc
    engine.ready.set()
    engine.load_done.set()
    # Pause at the real boundary after selecting proc and before sending.
    class Gate:
        first = True
        def __enter__(self):
            if self.first:
                self.first = False
                selected.set()
                assert proceed.wait(5)
        def __exit__(self, *args):
            pass
    engine._pending_lock = Gate()
    fresh = StubSTT(text="new engine result")
    ctx = _ctx(engine)
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(_run(ctx, Recorder())), daemon=True)
    thread.start()
    try:
        assert selected.wait(5)
        ctx.stt = fresh
        engine.close()
    finally:
        proceed.set()
        thread.join(5)
    assert not thread.is_alive()
    assert outcomes[0].status == 200, outcomes[0].payload
    assert len(fresh.calls) == 1


def test_any_failure_of_a_replaced_engines_worker_counts_as_replaced():
    """Whatever error the closed engine's worker produces (here it exits after
    the request was sent), our own swap is the cause: the request gets the
    replaced error, which the pipeline retries (lead's condition)."""
    engine = SubprocessWhisperSTT("small.en", "int8", model_source="fake-model")
    proc = _Proc()

    class DyingStdin(_Stdin):
        def write(self, text):
            super().write(text)
            request = json.loads(text)
            if request.get("op") == "transcribe":
                ev, box = engine._pending[request["id"]]

                def swap_then_die():
                    engine.close()                      # the swap lands right after the send
                    box["error"] = "stt worker exited"  # ...and the worker is gone
                    ev.set()
                threading.Thread(target=swap_then_die, daemon=True).start()
    proc.stdin = DyingStdin()
    engine._proc = proc
    with pytest.raises(STTEngineReplacedError):
        engine._run_inference("/tmp/clip.wav", "")
