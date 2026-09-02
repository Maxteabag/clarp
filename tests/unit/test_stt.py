"""Tests for stt._join_confident_segments and WhisperSTT readiness gating."""
import sys
import pathlib
import json
import io
import pytest
import threading
import time
import wave

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.stt import (DisabledSTT, UnavailableSTT, WhisperCppSTT, WhisperSTT,
                     _join_confident_segments,
                     installed_transcription_models)  # noqa: E402


class FakeSeg:
    def __init__(self, text: str, no_speech_prob: float = 0.0):
        self.text = text
        self.no_speech_prob = no_speech_prob


def test_join_drops_low_confidence_segments():
    out = _join_confident_segments([
        FakeSeg("hello "),
        FakeSeg("garbage ", no_speech_prob=0.8),
        FakeSeg("world"),
    ])
    assert out == "hello world"


def test_join_handles_segments_without_no_speech_attr():
    class Bare:
        text = "hi"
    assert _join_confident_segments([Bare()]) == "hi"


def test_transcribe_rejects_until_ready():
    s = WhisperSTT()
    with pytest.raises(RuntimeError):
        s.transcribe_bytes(b"", "audio/webm", "")


def test_transcribe_rejects_when_inference_lock_busy():
    from lib.stt import STTBusyError, WhisperSTT

    s = WhisperSTT()
    s.ready.set()
    s._model = object()
    assert s._lock.acquire(blocking=False)
    try:
        try:
            s.transcribe_bytes(b"audio", "audio/webm", "")
        except STTBusyError:
            pass
        else:
            raise AssertionError("expected STTBusyError")
    finally:
        s._lock.release()


def test_transcribe_lock_fairness_wait_param():
    """Regression: the authoritative /transcribe returned 429 ('whisper busy')
    when a best-effort live partial held the lock — so voice messages silently
    failed to send. transcribe_bytes(wait=) lets the real transcript wait for
    the lock; wait=0 (partials) still fails fast. The lock is checked before
    the model runs, so no real model is needed here."""
    import time
    from lib.stt import WhisperSTT, STTBusyError

    stt = WhisperSTT()
    stt._model = object()        # non-None so the readiness guard passes
    stt.ready.set()
    stt._lock.acquire()          # simulate a partial mid-flight
    try:
        # Non-blocking (partial-style): fails fast.
        t0 = time.time()
        try:
            stt.transcribe_bytes(b"\x00\x00", "wav", "", wait=0.0)
            assert False, "expected STTBusyError"
        except STTBusyError:
            pass
        assert time.time() - t0 < 0.2, "wait=0 must not block"

        # Authoritative (wait>0): blocks up to the timeout, then surfaces busy.
        t0 = time.time()
        try:
            stt.transcribe_bytes(b"\x00\x00", "wav", "", wait=0.3)
            assert False, "expected STTBusyError after timeout"
        except STTBusyError:
            pass
        assert time.time() - t0 >= 0.3, "wait>0 must block for the lock"
    finally:
        stt._lock.release()


def test_installed_models_come_from_managed_registry(tmp_path, monkeypatch):
    from lib import transcription_models
    monkeypatch.setattr(transcription_models, "REGISTRY", tmp_path / "models.json")
    snapshot = tmp_path / "faster-small"
    snapshot.mkdir(parents=True)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (snapshot / name).write_bytes(b"complete")
    checkpoint = tmp_path / "medium.pt"
    checkpoint.write_bytes(b"c" * 1_000_001)
    transcription_models.register("faster-whisper:small.en", str(snapshot))
    transcription_models.register("openai-whisper:medium", str(checkpoint))

    models = installed_transcription_models()
    assert [item["id"] for item in models] == [
        "faster-whisper:small.en", "openai-whisper:medium",
    ]


def test_unregistered_provider_cache_is_not_product_state(tmp_path, monkeypatch):
    from lib import transcription_models
    monkeypatch.setattr(transcription_models, "REGISTRY", tmp_path / "models.json")
    cache = tmp_path / "provider-cache"
    cache.mkdir()
    (cache / "medium.pt").write_bytes(b"x" * 1_000_001)
    assert not any(
        item["id"] == "openai-whisper:medium"
        for item in installed_transcription_models())


def test_selected_model_waits_for_lazy_load_in_same_request(monkeypatch):
    class Variant:
        def __init__(self):
            self.ready = threading.Event()
            self.load_done = self.ready
            self.load_error = None

        def transcribe_bytes(self, *_args, **_kwargs):
            return "kept utterance", True, 0.01

    stt = WhisperSTT("small.en")
    variant = Variant()
    selected = "openai-whisper:medium"
    stt._variants[selected] = variant
    monkeypatch.setattr(stt, "capabilities", lambda: {
        "models": [{"id": selected}], "default_model": stt.default_model_id,
    })
    threading.Thread(
        target=lambda: (time.sleep(0.03), variant.ready.set()), daemon=True
    ).start()

    assert stt.transcribe_model_bytes(
        selected, b"audio", "audio/wav", "", wait=1.0
    )[0] == "kept utterance"


def test_failed_variant_is_evicted_for_future_retry(monkeypatch):
    class FailedVariant:
        ready = threading.Event()
        load_done = threading.Event()
        load_error = RuntimeError("bad cache")

    FailedVariant.load_done.set()
    stt = WhisperSTT("small.en")
    selected = "openai-whisper:medium"
    variant = FailedVariant()
    stt._variants[selected] = variant
    monkeypatch.setattr(stt, "capabilities", lambda: {
        "models": [{"id": selected}], "default_model": stt.default_model_id,
    })

    with pytest.raises(RuntimeError, match="bad cache"):
        stt.transcribe_model_bytes(selected, b"audio", "audio/wav", "")
    assert selected not in stt._variants


def test_variants_can_share_one_global_inference_lock():
    owner = WhisperSTT("small.en")
    variant = WhisperSTT("base.en", inference_lock=owner._lock)
    assert variant._lock is owner._lock


def test_multilingual_models_enable_language_detection():
    assert WhisperSTT("small.en").language == "en"
    assert WhisperSTT("small").language is None
    assert WhisperSTT("large-v3-turbo").language is None


def test_whisper_cpp_runs_managed_cli_and_reads_json(tmp_path, monkeypatch):
    model = tmp_path / "ggml-small.en.bin"
    runtime = tmp_path / "whisper-cli"
    model.write_bytes(b"model")
    runtime.write_text("binary")
    runtime.chmod(0o700)
    stt = WhisperCppSTT(
        "small.en", model_source=str(model), runtime_source=str(runtime))
    stt._load()
    seen = []

    def run(command, **_kwargs):
        seen.append(command)
        output = pathlib.Path(command[command.index("-of") + 1]).with_suffix(".json")
        output.write_text(json.dumps({
            "transcription": [{"text": " Hello"}, {"text": " world."}],
        }))
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("lib.stt.subprocess.run", run)
    encoded = io.BytesIO()
    with wave.open(encoded, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(bytes(3200))
    wav = encoded.getvalue()
    text, terminal, _duration = stt.transcribe_bytes(
        wav, "audio/wav", "Clarp", wait=1.0)
    assert text == "Hello world."
    assert terminal is True
    assert "--prompt" in seen[0]
    assert stt.default_model_id == "whisper.cpp:small.en"


def test_whisper_cpp_rejects_malformed_encoded_input(tmp_path):
    model = tmp_path / "model.bin"; model.write_bytes(b"model")
    runtime = tmp_path / "whisper-cli"; runtime.write_text("binary")
    runtime.chmod(0o700)
    stt = WhisperCppSTT(
        "small.en", model_source=str(model), runtime_source=str(runtime))
    stt._load()
    with pytest.raises(RuntimeError, match="could not decode"):
        stt.transcribe_bytes(b"not wav", "audio/m4a", "")


def test_whisper_cpp_decodes_webm_without_system_ffmpeg(tmp_path):
    import av
    from lib.stt import _write_whisper_cpp_wav

    encoded = io.BytesIO()
    with av.open(encoded, mode="w", format="webm") as container:
        stream = container.add_stream("libopus", rate=16_000)
        stream.layout = "mono"
        frame = av.AudioFrame(format="s16", layout="mono", samples=1600)
        frame.sample_rate = 16_000
        frame.planes[0].update(bytes(3200))
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)

    output = tmp_path / "normalized.wav"
    _write_whisper_cpp_wav(encoded.getvalue(), output)
    with wave.open(str(output), "rb") as normalized:
        assert normalized.getnchannels() == 1
        assert normalized.getsampwidth() == 2
        assert normalized.getframerate() == 16_000


def test_disabled_stt_never_advertises_server_transcription(monkeypatch):
    from lib import stt as stt_module, transcription_models
    monkeypatch.setattr(transcription_models, "catalog_status", lambda: [])
    monkeypatch.setattr(stt_module, "installed_transcription_models", lambda: [])
    stt = DisabledSTT()
    stt.start_loading()
    assert stt.ready.is_set() is True
    assert stt.capabilities()["available"] is False


def test_apple_default_accepts_explicit_custom_adapter_after_restart(monkeypatch):
    from types import SimpleNamespace
    from lib import custom_stt_adapters, stt as stt_module
    monkeypatch.setattr(stt_module, "installed_transcription_models", lambda: [])
    monkeypatch.setattr(
        custom_stt_adapters, "discover",
        lambda: [SimpleNamespace(id="custom.private")])

    manager = DisabledSTT()

    assert manager.available is True


def test_apple_default_mode_advertises_explicit_installed_models(monkeypatch):
    from lib import stt as stt_module, transcription_models
    model = {"id": "faster-whisper:small", "name": "Small",
             "provider": "faster-whisper", "model": "small", "weight": "medium"}
    monkeypatch.setattr(stt_module, "installed_transcription_models", lambda: [model])
    monkeypatch.setattr(transcription_models, "catalog_status", lambda: [model])
    stt = DisabledSTT()
    assert stt.capabilities()["models"] == [model]
    assert stt.available is True


def test_missing_default_still_runs_explicit_installed_variant(monkeypatch):
    from lib import stt as stt_module, transcription_models

    class Variant:
        load_done = threading.Event()
        load_error = None
        def transcribe_bytes(self, *_args, **_kwargs):
            return "alternate", True, 0.01

    Variant.load_done.set()
    alternate = "openai-whisper:medium"
    monkeypatch.setattr(stt_module, "installed_transcription_models", lambda: [{
        "id": alternate, "name": "Medium", "provider": "openai-whisper",
        "model": "medium", "weight": "heavy",
    }])
    monkeypatch.setattr(transcription_models, "catalog_status", lambda: [])
    manager = UnavailableSTT("missing", "int8", "default missing")
    manager.start_loading()
    assert manager.load_done.is_set() is False
    manager._variants[alternate] = Variant()
    assert manager.transcribe_model_bytes(
        alternate, b"audio", "audio/wav", "")[0] == "alternate"


def test_inference_cpu_threads_leaves_cores_for_handlers():
    from lib.stt import inference_cpu_threads
    assert inference_cpu_threads(8) == 4
    assert inference_cpu_threads(16) == 4   # capped: inference gains little past 4
    assert inference_cpu_threads(4) == 2
    assert inference_cpu_threads(2) == 1
    assert inference_cpu_threads(1) == 1    # never zero


def test_custom_adapter_stt_exposes_models_and_transcribes(monkeypatch):
    from types import SimpleNamespace
    from lib import custom_stt_adapters
    from lib.stt import CustomAdapterSTT
    manifest = SimpleNamespace(
        id="custom.private", name="Private STT", default_model="general")
    row = {"id": "custom.private:general", "name": "General",
           "provider": "custom.private", "model": "general",
           "weight": "adapter", "installed": True, "custom": True}
    builtin = {"id": "faster-whisper:small.en", "name": "Small",
               "provider": "faster-whisper", "model": "small.en",
               "weight": "medium", "installed": True}
    monkeypatch.setattr(
        "lib.stt.installed_transcription_models", lambda: [builtin])
    monkeypatch.setattr(custom_stt_adapters, "models", lambda _manifest: [row])
    monkeypatch.setattr(custom_stt_adapters, "catalog_models", lambda: [row])
    monkeypatch.setattr(custom_stt_adapters, "inventory", lambda: [{
        "id": "custom.private", "name": "Private STT",
        "description": "", "installed": True, "available": True}])
    monkeypatch.setattr(
        custom_stt_adapters, "transcribe",
        lambda *_args, **_kwargs: ("private transcript", True, 0.2))

    manager = CustomAdapterSTT(manifest, "general")

    assert manager.ready.is_set()
    assert manager.capabilities()["models"] == [builtin, row]
    assert manager.transcribe_bytes(
        b"audio", "audio/wav", "Clarp")[0] == "private transcript"
    manager._router.transcribe_model_bytes = (
        lambda *_args, **_kwargs: ("whisper fallback", True, 0.3))
    assert manager.transcribe_model_bytes(
        "faster-whisper:small.en", b"audio", "audio/wav", "", wait=1
    )[0] == "whisper fallback"


def test_custom_adapter_stt_serializes_inference(monkeypatch):
    from types import SimpleNamespace
    from lib import custom_stt_adapters
    from lib.stt import CustomAdapterSTT, STTBusyError
    manifest = SimpleNamespace(
        id="custom.private", name="Private STT", default_model="general")
    row = {"id": "custom.private:general", "name": "General",
           "provider": "custom.private", "model": "general",
           "weight": "adapter", "installed": True, "custom": True}
    monkeypatch.setattr(custom_stt_adapters, "models", lambda _manifest: [row])
    monkeypatch.setattr(custom_stt_adapters, "catalog_models", lambda: [row])
    monkeypatch.setattr(custom_stt_adapters, "inventory", lambda: [])
    monkeypatch.setattr(
        "lib.stt.installed_transcription_models", lambda: [])
    manager = CustomAdapterSTT(manifest, "general")
    manager._lock.acquire()
    try:
        with pytest.raises(STTBusyError):
            manager.transcribe_bytes(
                b"audio", "audio/wav", "", wait=0)
    finally:
        manager._lock.release()


def test_explicit_custom_model_does_not_hold_whisper_selection_lock(monkeypatch):
    from types import SimpleNamespace
    from lib import custom_stt_adapters
    from lib.stt import WhisperSTT
    manifest = SimpleNamespace(id="custom.private", default_model="general")
    row = {"id": "custom.private:general", "model": "general"}
    monkeypatch.setattr(
        custom_stt_adapters, "get",
        lambda provider: manifest if provider == "custom.private" else None)
    monkeypatch.setattr(custom_stt_adapters, "models", lambda _manifest: [row])
    monkeypatch.setattr(
        custom_stt_adapters, "transcribe",
        lambda *_args, **_kwargs: ("custom", True, 0.1))
    manager = WhisperSTT("small.en")
    manager._selection_lock.acquire()
    try:
        assert manager.transcribe_model_bytes(
            "custom.private:general", b"audio", "audio/wav", "", wait=0.1
        )[0] == "custom"
    finally:
        manager._selection_lock.release()
