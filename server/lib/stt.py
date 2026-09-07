"""Speech-to-text adapters for Clarp-managed local Whisper runtimes.

The Whisper model is heavy (~hundreds of MB) and slow to load (~5–10 s), so
the server kicks off loading in a background thread at import time and waits
on `ready` only when a `/transcribe` request arrives.
"""
from __future__ import annotations

import json
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
import wave
from typing import Iterable

from .hallucinations import is_pure_hallucination
from .log import log, log_exception


class STTBusyError(RuntimeError):
    """Raised when another Whisper inference is already running."""


class STTModelLoadingError(RuntimeError):
    """Raised while a lazily selected installed model is loading."""


class STTUnknownModelError(ValueError):
    """Raised when the client requests a model not installed on this server."""


def huggingface_cache_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get(
        "HF_HUB_CACHE",
        pathlib.Path(os.environ.get(
            "HF_HOME", pathlib.Path.home() / ".cache/huggingface")) / "hub",
    ))


def _model_weight(name: str) -> str:
    base = name.removesuffix(".en")
    if base in {"tiny", "base"}: return "light"
    if base == "small": return "medium"
    if base == "medium": return "heavy"
    return "very-heavy"


def _model_label(name: str) -> str:
    return name.replace("large-v3-turbo", "Large v3 Turbo").replace(".", " ").title()


def _installed_model_records() -> list[dict]:
    """Return models explicitly registered by Clarp's model manager."""
    from .transcription_models import installed_records
    return installed_records()


def installed_transcription_models() -> list[dict]:
    return [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in _installed_model_records()
    ]


def inference_cpu_threads(cpu_count: int | None = None) -> int:
    """Threads granted to in-process inference: half the cores, capped at 4.

    Inference shares this process with every HTTP handler thread. Left at
    ctranslate2's default it takes all cores, and a single long transcribe
    starves /teams, /log and /send for tens of seconds (measured 2026-08-24:
    243s transcribe stalled /teams for 56s), which the app reads as the
    server being offline.
    """
    cores = cpu_count if cpu_count is not None else (os.cpu_count() or 4)
    return max(1, min(4, cores // 2))


# Greedy decoding (beam_size=1) commits to the first plausible token and cannot
# back out, which is precisely how a coined name becomes a common word. Beam
# search keeps alternatives alive long enough for the biasing prompt to pull
# the right one through. 5 is the usual default; 1 restores the old behaviour.
DEFAULT_BEAM_SIZE = 5
_BEAM_SIZE_KEY = "transcription.decode.beam_size"


def inference_beam_size() -> int:
    """Beam width for local Whisper decoding.

    Bounded rather than free-form: beams above ~8 cost latency out of
    proportion to any accuracy gain, and a mistyped setting should degrade
    gracefully instead of stalling a turn.
    """
    try:
        from . import settings_store
        return settings_store.get_int(
            _BEAM_SIZE_KEY, default=DEFAULT_BEAM_SIZE, minimum=1, maximum=8)
    except Exception as e:  # noqa: BLE001 - decoding must never fail on config
        log_exception("beamSizeSettingFail", e)
        return DEFAULT_BEAM_SIZE


class WhisperSTT:
    """Loads a faster-whisper model in the background; transcribes on demand."""

    def __init__(self, model_name: str = "small.en", compute_type: str = "int8",
                 inference_lock: threading.Lock | None = None,
                 model_source: str | None = None):
        self.model_name = model_name
        self.language = "en" if model_name.endswith(".en") else None
        self.available = True
        self.compute_type = compute_type
        self.model_source = model_source or model_name
        self._model = None
        self.ready = threading.Event()
        self.load_done = threading.Event()
        self.load_error: Exception | None = None
        self._lock = inference_lock or threading.Lock()
        self._variants: dict[str, WhisperSTT | OpenAIWhisperSTT] = {}
        self._variants_lock = threading.Lock()
        self._selection_lock = threading.Lock()

    provider = "faster-whisper"

    @property
    def default_model_id(self) -> str:
        return f"{self.provider}:{self.model_name}"

    def capabilities(self) -> dict:
        models = installed_transcription_models()
        from .custom_stt_adapters import catalog_models, inventory
        known = {item["id"] for item in models}
        models.extend(
            item for item in catalog_models() if item["id"] not in known)
        default_id = self.default_model_id
        if not any(item["id"] == default_id for item in models):
            models.insert(0, {
                "id": default_id, "name": _model_label(self.model_name),
                "provider": self.provider, "model": self.model_name,
                "weight": _model_weight(self.model_name),
            })
        from . import stt_providers
        known = {item["id"] for item in models}
        models.extend(
            item for item in stt_providers.cloud_models(available_only=True)
            if item["id"] not in known)
        from .transcription_models import catalog_status
        return {
            "available": True,
            "default_model": default_id,
            "engine": stt_providers.selected_engine(),
            "turn_taking": stt_providers.selected_turn_taking(),
            "models": models,
            "catalog": catalog_status(),
            "adapters": inventory(),
        }

    def transcribe_model_bytes(self, model_id: str, audio_bytes: bytes,
                               content_type: str, vocab_prompt: str, *,
                               wait: float = 0.0) -> tuple[str, bool, float]:
        selected = model_id.strip() or self.default_model_id
        if selected == self.default_model_id:
            if not self.load_done.wait(timeout=150.0):
                raise STTModelLoadingError(f"transcription model loading: {selected}")
            if self.load_error is not None:
                raise RuntimeError(
                    f"could not load transcription model {selected}: {self.load_error}")
            return self.transcribe_bytes(
                audio_bytes, content_type, vocab_prompt, wait=wait)
        provider = selected.split(":", 1)[0] if ":" in selected else ""
        from . import stt_providers
        if stt_providers.is_cloud_model(selected):
            # Cloud engines need no warm-up and no inference lock: the
            # provider serialises nothing on our side.
            return stt_providers.transcribe(
                selected, audio_bytes, content_type, vocab_prompt)
        from .custom_stt_adapters import get as custom_adapter, models as custom_models
        manifest = custom_adapter(provider)
        with self._variants_lock:
            already_loaded = selected in self._variants
        if already_loaded:
            pass
        elif manifest is not None:
            if selected not in {item["id"] for item in custom_models(manifest)}:
                raise STTUnknownModelError(
                    f"transcription model not installed: {selected}")
        elif selected not in {
                item["id"] for item in _installed_model_records()}:
            raise STTUnknownModelError(
                f"transcription model not installed: {selected}")
        if manifest is not None:
            from .custom_stt_adapters import inference_lock, transcribe
            adapter_lock = inference_lock(manifest.id)
            acquired = (adapter_lock.acquire(timeout=wait) if wait > 0
                        else adapter_lock.acquire(blocking=False))
            if not acquired:
                raise STTBusyError("transcription adapter busy")
            try:
                return transcribe(
                    manifest, model_id=selected,
                    audio_bytes=audio_bytes, content_type=content_type,
                    vocab_prompt=vocab_prompt)
            finally:
                adapter_lock.release()
        # Serialize selection + loading + inference so two requests cannot warm
        # different multi-gigabyte variants at once. The inner inference lock is
        # still shared with the configured default model.
        with self._selection_lock:
            with self._variants_lock:
                variant = self._variants.get(selected)
                if variant is None:
                    provider, name = selected.split(":", 1)
                    record = next(
                        item for item in _installed_model_records()
                        if item["id"] == selected)
                    source = record["_local_path"]
                    if provider == "faster-whisper":
                        variant = WhisperSTT(
                            name, self.compute_type, self._lock,
                            model_source=source)
                    elif provider == "openai-whisper":
                        variant = OpenAIWhisperSTT(
                            name, self._lock, model_source=source)
                    elif provider == "whisper.cpp":
                        variant = WhisperCppSTT(
                            name, self._lock, model_source=source,
                            runtime_source=record["_runtime_path"])
                    else:
                        raise STTUnknownModelError(
                            f"unsupported transcription provider: {provider}")
                    self._variants.clear()
                    self._variants[selected] = variant
                    variant.start_loading()
            if not variant.load_done.wait(timeout=150.0):
                raise STTModelLoadingError(
                    f"transcription model loading: {selected}")
            if variant.load_error is not None:
                with self._variants_lock:
                    if self._variants.get(selected) is variant:
                        self._variants.pop(selected, None)
                raise RuntimeError(
                    f"could not load transcription model {selected}: {variant.load_error}")
            return variant.transcribe_bytes(
                audio_bytes, content_type, vocab_prompt, wait=wait)

    def start_loading(self) -> None:
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        log("whisperLoadStart", f"{self.model_name} {self.compute_type}")
        t0 = time.time()
        try:
            from faster_whisper import WhisperModel  # type: ignore
            self._model = WhisperModel(self.model_source, device="cpu",
                                       compute_type=self.compute_type,
                                       cpu_threads=inference_cpu_threads())
        except Exception as e:
            self.load_error = e
            log_exception("whisperLoadFail", e)
            self.load_done.set()
            return
        log("whisperLoadOk", f"{time.time() - t0:.1f}s")
        self.ready.set()
        self.load_done.set()

    def _inference_available(self) -> bool:
        return self._model is not None

    def _run_inference(self, path: str, vocab_prompt: str) -> list:
        """Run the model on `path`; returns segment-like objects with
        `.text` and `.no_speech_prob`. Called with the inference lock held."""
        segments_iter, _info = self._model.transcribe(
            path,
            language=self.language,
            # Beam search rather than greedy. Costs a little latency and buys
            # accuracy on exactly the material that was failing: proper nouns
            # and coined product names, where the greedy path commits to a
            # common word early and cannot recover. Tunable because the right
            # trade differs between a phone in a car and a desktop.
            beam_size=inference_beam_size(),
            condition_on_previous_text=False,
            initial_prompt=vocab_prompt,
            no_speech_threshold=0.7,
            log_prob_threshold=-0.9,
            compression_ratio_threshold=2.4,
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 400,
                "speech_pad_ms": 150,
            },
        )
        return list(segments_iter)

    def transcribe_bytes(self, audio_bytes: bytes, content_type: str,
                         vocab_prompt: str, *, wait: float = 0.0
                         ) -> tuple[str, bool, float]:
        """Write `audio_bytes` to a temp file, run faster-whisper, return
        (final_text, ends_terminal, duration_seconds).

        `wait` controls lock contention with concurrent inferences (e.g. the
        best-effort live-transcription partials): 0.0 (default) acquires
        non-blocking and raises STTBusyError immediately if busy — right for
        skippable partials. A positive value blocks up to `wait` seconds for
        the lock — right for the authoritative one-shot transcript, which must
        not be dropped just because a partial happened to be running.

        Raises RuntimeError if the model isn't loaded yet.
        """
        if not self.ready.is_set() or not self._inference_available():
            raise RuntimeError("whisper model not ready")

        suffix = (".m4a" if "mp4" in content_type or "m4a" in content_type
                  else ".wav" if "wav" in content_type
                  else ".ogg" if "ogg" in content_type
                  else ".webm")
        fd, path = tempfile.mkstemp(prefix="claude-stt-", suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(audio_bytes)
            t0 = time.time()
            acquired = (self._lock.acquire(timeout=wait) if wait > 0
                        else self._lock.acquire(blocking=False))
            if not acquired:
                raise STTBusyError("whisper busy")
            try:
                segments = self._run_inference(path, vocab_prompt)
            finally:
                self._lock.release()
            raw = _join_confident_segments(segments)
            text = "" if is_pure_hallucination(raw) else raw
            dur = time.time() - t0
            log("whisperDone", f"{dur:.2f}s raw={raw!r} kept={text!r}")
        finally:
            try:
                os.unlink(path)
            except OSError as e:
                log_exception("whisperUnlinkFail", e, detail=path)
        ends_terminal = bool(text and text.rstrip()[-1:] in ".!?")
        return text, ends_terminal, dur


class WhisperCppSTT(WhisperSTT):
    """macOS-native Whisper adapter backed by a managed whisper-cli binary."""

    provider = "whisper.cpp"
    PROCESS_TIMEOUT_SEC = 300.0

    def __init__(
        self, model_name: str, inference_lock: threading.Lock | None = None, *,
        model_source: str, runtime_source: str,
    ):
        super().__init__(
            model_name, "native", inference_lock, model_source=model_source)
        self.runtime_source = runtime_source

    def _load(self) -> None:
        model = pathlib.Path(self.model_source)
        runtime = pathlib.Path(self.runtime_source)
        if (not model.is_file() or not runtime.is_file()
                or not os.access(runtime, os.X_OK)):
            self.load_error = RuntimeError(
                "managed whisper.cpp runtime or model is missing")
            self.load_done.set()
            return
        self._model = str(model)
        self.ready.set()
        self.load_done.set()
        log("whisperLoadOk", f"whisper.cpp {self.model_name}")

    def transcribe_bytes(self, audio_bytes: bytes, content_type: str,
                         vocab_prompt: str, *, wait: float = 0.0
                         ) -> tuple[str, bool, float]:
        if not self.ready.is_set() or self._model is None:
            raise STTModelLoadingError("whisper.cpp model not ready")
        acquired = (self._lock.acquire(timeout=wait) if wait > 0
                    else self._lock.acquire(blocking=False))
        if not acquired:
            raise STTBusyError("whisper busy")
        started = time.time()
        try:
            with tempfile.TemporaryDirectory(prefix="clarp-stt-") as directory:
                audio = pathlib.Path(directory) / "input.wav"
                output = pathlib.Path(directory) / "transcript"
                _write_whisper_cpp_wav(audio_bytes, audio)
                command = [
                    self.runtime_source, "-m", self.model_source,
                    "-f", str(audio), "-oj", "-of", str(output),
                    "-nt", "-np", "-t", str(inference_cpu_threads()),
                    "-l", self.language or "auto",
                ]
                if vocab_prompt:
                    command += ["--prompt", vocab_prompt]
                completed = subprocess.run(
                    command, capture_output=True, text=True,
                    timeout=self.PROCESS_TIMEOUT_SEC)
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout).strip()[-500:]
                    raise RuntimeError(
                        f"whisper.cpp transcription failed: {detail or completed.returncode}")
                payload = json.loads(output.with_suffix(".json").read_text())
                raw = "".join(
                    str(segment.get("text") or "")
                    for segment in payload.get("transcription", [])).strip()
        finally:
            self._lock.release()
        text = "" if is_pure_hallucination(raw) else raw
        duration = time.time() - started
        log("whisperDone", f"{duration:.2f}s raw={raw!r} kept={text!r}")
        return text, bool(text and text.rstrip()[-1:] in ".!?"), duration


def _write_whisper_cpp_wav(audio_bytes: bytes, destination: pathlib.Path) -> None:
    """Normalize WAV/MP4/WebM input to the mono 16 kHz PCM CLI contract."""
    if audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (
                    1, 2, 16_000,
                ):
                    destination.write_bytes(audio_bytes)
                    return
        except (EOFError, wave.Error):
            pass
    try:
        import av  # type: ignore
        from av.audio.resampler import AudioResampler  # type: ignore

        with av.open(io.BytesIO(audio_bytes), mode="r") as container:
            stream = next(
                (candidate for candidate in container.streams
                 if candidate.type == "audio"), None)
            if stream is None:
                raise RuntimeError("uploaded media has no audio stream")
            resampler = AudioResampler(format="s16", layout="mono", rate=16_000)
            with wave.open(str(destination), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                for frame in container.decode(stream):
                    for converted in resampler.resample(frame):
                        output.writeframes(
                            bytes(converted.planes[0])[:converted.samples * 2])
                for converted in resampler.resample(None):
                    output.writeframes(
                        bytes(converted.planes[0])[:converted.samples * 2])
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"could not decode transcription audio: {exc}") from exc


def _join_confident_segments(segments: Iterable) -> str:
    kept = []
    for seg in segments:
        if getattr(seg, "no_speech_prob", 0.0) >= 0.6:
            continue
        kept.append(seg.text)
    return "".join(kept).strip()


class SubprocessWhisperSTT(WhisperSTT):
    """WhisperSTT whose inference runs in a worker process (lib.stt_worker).

    Same interface and lock semantics as WhisperSTT; only `_load` and
    `_run_inference` differ. If the worker fails to start or load, falls back
    to the in-process model so transcription never silently disappears.
    The worker is respawned lazily if it dies between requests.
    """

    WORKER_TIMEOUT_SEC = 300.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, dict]] = {}
        self._pending_lock = threading.Lock()
        self._fallback_in_process = False
        self._load_event_text: str | None = None

    # -- worker lifecycle -------------------------------------------------
    def _worker_cmd(self) -> list[str]:
        return [sys.executable, "-m", "lib.stt_worker"]

    def _worker_cwd(self) -> str:
        return str(pathlib.Path(__file__).resolve().parents[1])

    def _spawn_worker(self) -> bool:
        """Start the worker and wait for its ready event. Returns True when
        the worker loaded the model, False when it failed (caller falls back)."""
        try:
            proc = subprocess.Popen(
                self._worker_cmd(), cwd=self._worker_cwd(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except OSError as e:
            log_exception("sttWorkerSpawnFail", e)
            return False
        ready = threading.Event()
        outcome: dict = {}
        threading.Thread(target=self._reader, args=(proc, ready, outcome),
                         daemon=True, name="stt-worker-reader").start()
        try:
            proc.stdin.write(json.dumps({
                "op": "load", "model_source": self.model_source,
                "compute_type": self.compute_type,
                "cpu_threads": inference_cpu_threads(),
                "language": self.language,
            }) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as e:
            log_exception("sttWorkerLoadWriteFail", e)
            self._kill(proc)
            return False
        if not ready.wait(timeout=self.WORKER_TIMEOUT_SEC) or not outcome.get("ok"):
            log("sttWorkerLoadFail", str(outcome.get("error") or "timeout")[:300])
            self._kill(proc)
            return False
        self._proc = proc
        return True

    def _reader(self, proc: subprocess.Popen, ready: threading.Event,
                outcome: dict) -> None:
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("event") == "ready":
                    outcome["ok"] = True
                    ready.set()
                    continue
                if msg.get("event") == "load_error":
                    outcome["ok"] = False
                    outcome["error"] = msg.get("error")
                    ready.set()
                    continue
                rid = str(msg.get("id") or "")
                with self._pending_lock:
                    slot = self._pending.get(rid)
                if slot is not None:
                    slot[1].update(msg)
                    slot[0].set()
        except (OSError, ValueError):
            pass
        finally:
            if not ready.is_set():
                outcome.setdefault("error", "worker exited before ready")
                ready.set()
            # Fail every request still waiting on this (now dead) worker.
            with self._pending_lock:
                waiting = list(self._pending.values())
            for ev, box in waiting:
                box.setdefault("error", "stt worker exited")
                ev.set()

    @staticmethod
    def _kill(proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    # -- WhisperSTT overrides ---------------------------------------------
    def _load(self) -> None:
        log("whisperLoadStart", f"{self.model_name} {self.compute_type} (worker)")
        t0 = time.time()
        if self._spawn_worker():
            log("whisperLoadOk", f"{time.time() - t0:.1f}s worker pid={self._proc.pid}")
            self.ready.set()
            self.load_done.set()
            return
        log("sttWorkerFallback", "loading whisper in-process instead")
        self._fallback_in_process = True
        super()._load()

    def _inference_available(self) -> bool:
        if self._fallback_in_process:
            return super()._inference_available()
        return True

    def _run_inference(self, path: str, vocab_prompt: str) -> list:
        if self._fallback_in_process:
            return super()._run_inference(path, vocab_prompt)
        with self._proc_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                log("sttWorkerRespawn", "worker not running; respawning")
                if not self._spawn_worker():
                    raise RuntimeError("stt worker unavailable")
                proc = self._proc
        rid = uuid.uuid4().hex
        ev = threading.Event()
        box: dict = {}
        with self._pending_lock:
            self._pending[rid] = (ev, box)
        try:
            try:
                proc.stdin.write(json.dumps({
                    "op": "transcribe", "id": rid, "path": path,
                    "prompt": vocab_prompt or "",
                }) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as e:
                raise RuntimeError(f"stt worker write failed: {e}") from e
            if not ev.wait(timeout=self.WORKER_TIMEOUT_SEC):
                self._kill(proc)
                raise RuntimeError("stt worker timed out")
        finally:
            with self._pending_lock:
                self._pending.pop(rid, None)
        if box.get("error"):
            raise RuntimeError(f"stt worker: {box['error']}")
        return [types.SimpleNamespace(text=str(seg.get("text") or ""),
                                      no_speech_prob=float(seg.get("no_speech_prob") or 0.0))
                for seg in (box.get("segments") or [])]


class OpenAIWhisperSTT:
    """Lazy wrapper for models already cached by the OpenAI Whisper package."""

    def __init__(self, model_name: str,
                 inference_lock: threading.Lock | None = None,
                 model_source: str | None = None):
        self.model_name = model_name
        self.language = "en" if model_name.endswith(".en") else None
        self.model_source = model_source or model_name
        self._model = None
        self.ready = threading.Event()
        self.load_done = threading.Event()
        self.load_error: Exception | None = None
        self._lock = inference_lock or threading.Lock()

    def start_loading(self) -> None:
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        log("whisperLoadStart", f"openai {self.model_name}")
        try:
            import whisper  # type: ignore
            try:
                import torch  # type: ignore
                torch.set_num_threads(inference_cpu_threads())
            except Exception:
                pass
            self._model = whisper.load_model(self.model_source)
        except Exception as e:
            self.load_error = e
            log_exception("whisperLoadFail", e)
            self.load_done.set()
            return
        self.ready.set()
        self.load_done.set()
        log("whisperLoadOk", f"openai {self.model_name}")

    def transcribe_bytes(self, audio_bytes: bytes, content_type: str,
                         vocab_prompt: str, *, wait: float = 0.0
                         ) -> tuple[str, bool, float]:
        if not self.ready.is_set() or self._model is None:
            raise STTModelLoadingError("whisper model not ready")
        suffix = ".m4a" if "mp4" in content_type or "m4a" in content_type else ".wav"
        fd, path = tempfile.mkstemp(prefix="claude-stt-", suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(audio_bytes)
            acquired = (self._lock.acquire(timeout=wait) if wait > 0
                        else self._lock.acquire(blocking=False))
            if not acquired:
                raise STTBusyError("whisper busy")
            t0 = time.time()
            try:
                result = self._model.transcribe(
                    path, language=self.language, fp16=False,
                    initial_prompt=vocab_prompt or None,
                    condition_on_previous_text=False,
                )
            finally:
                self._lock.release()
            raw = str(result.get("text") or "").strip()
            text = "" if is_pure_hallucination(raw) else raw
            duration = time.time() - t0
        finally:
            try: os.unlink(path)
            except OSError as e: log_exception("whisperUnlinkFail", e, detail=path)
        return text, bool(text and text.rstrip()[-1:] in ".!?"), duration


class CustomAdapterSTT:
    """Configured-default STT provider backed by a custom executable adapter."""

    def __init__(self, manifest, model_name: str):
        self.manifest = manifest
        self.model_name = model_name or manifest.default_model
        self.provider = manifest.id
        from .custom_stt_adapters import inference_lock
        self._lock = inference_lock(manifest.id)
        self._router = DisabledSTT()
        self.ready = threading.Event()
        self.load_done = threading.Event()
        self.load_error = None
        try:
            from .custom_stt_adapters import models
            rows = models(self.manifest)
            if self.default_model_id not in {row["id"] for row in rows}:
                raise RuntimeError(
                    f"configured transcription model is unavailable: {self.default_model_id}")
            self.ready.set()
            self.available = True
        except Exception as exc:
            self.load_error = exc
            alternatives = self._router.capabilities()["models"]
            self.available = bool(alternatives)
        self.load_done.set()

    @property
    def default_model_id(self) -> str:
        return f"{self.provider}:{self.model_name}"

    def start_loading(self) -> None:
        return None

    def capabilities(self) -> dict:
        from .custom_stt_adapters import catalog_models, inventory, models
        from .transcription_models import catalog_status
        installed = installed_transcription_models()
        known = {row["id"] for row in installed}
        installed.extend(
            row for row in catalog_models() if row["id"] not in known)
        payload = {
            "available": bool(installed),
            "default_model": self.default_model_id,
            "models": installed,
            "catalog": catalog_status() + [
                row for row in installed if row.get("custom")],
            "adapters": inventory(),
        }
        try:
            rows = models(self.manifest)
            if self.default_model_id not in {row["id"] for row in rows}:
                raise RuntimeError(
                    f"configured transcription model is unavailable: {self.default_model_id}")
            self.load_error = None
            self.available = True
            self.ready.set()
        except Exception as exc:
            self.load_error = exc
            self.available = bool(installed)
            self.ready.clear()
            payload["error"] = str(exc)
        return payload

    def transcribe_model_bytes(
        self, model_id: str, audio_bytes: bytes, content_type: str,
        vocab_prompt: str, *, wait: float = 0.0,
    ) -> tuple[str, bool, float]:
        provider = model_id.split(":", 1)[0] if ":" in model_id else self.provider
        if provider != self.provider:
            return self._router.transcribe_model_bytes(
                model_id, audio_bytes, content_type,
                vocab_prompt, wait=wait)
        acquired = (self._lock.acquire(timeout=wait) if wait > 0
                    else self._lock.acquire(blocking=False))
        if not acquired:
            raise STTBusyError("transcription adapter busy")
        from .custom_stt_adapters import transcribe
        try:
            return transcribe(
                self.manifest, model_id=model_id, audio_bytes=audio_bytes,
                content_type=content_type, vocab_prompt=vocab_prompt)
        finally:
            self._lock.release()

    def transcribe_bytes(
        self, audio_bytes: bytes, content_type: str, vocab_prompt: str,
        *, wait: float = 0.0,
    ) -> tuple[str, bool, float]:
        return self.transcribe_model_bytes(
            self.default_model_id, audio_bytes, content_type,
            vocab_prompt, wait=wait)


class DisabledSTT(WhisperSTT):
    """Apple-default mode with opt-in access to explicitly installed models."""

    def __init__(self):
        super().__init__("__disabled__", "int8")
        self.ready.set()
        from .custom_stt_adapters import discover
        self.available = bool(installed_transcription_models() or discover())

    def start_loading(self) -> None:
        return

    def capabilities(self) -> dict:
        from .transcription_models import catalog_status
        from .custom_stt_adapters import catalog_models, inventory
        installed = installed_transcription_models()
        installed.extend(catalog_models())
        self.available = bool(installed)
        return {
            "available": bool(installed), "default_model": "", "models": installed,
            "catalog": catalog_status(),
            "adapters": inventory(),
        }

    def transcribe_bytes(self, *_args, **_kwargs):
        raise RuntimeError("server transcription is disabled")


class UnavailableSTT(WhisperSTT):
    """Configured server transcription whose local model files are missing."""

    def __init__(self, model_name: str, compute_type: str, message: str, *,
                 provider: str = "faster-whisper"):
        super().__init__(model_name, compute_type)
        self.provider = provider
        self.message = message

    def capabilities(self) -> dict:
        from .transcription_models import catalog_status
        from .custom_stt_adapters import catalog_models, inventory
        installed = installed_transcription_models()
        installed.extend(catalog_models())
        payload = {
            "available": bool(installed),
            "default_model": self.default_model_id,
            "models": installed,
            "catalog": catalog_status(),
            "adapters": inventory(),
        }
        payload["error"] = self.message
        return payload

    def start_loading(self) -> None:
        # Missing defaults are repaired only through the managed installer.
        # Explicitly selected installed variants still load on demand below.
        return

    def transcribe_model_bytes(self, model_id: str, *args, **kwargs):
        if model_id.strip() == self.default_model_id:
            raise STTUnknownModelError(self.message)
        return super().transcribe_model_bytes(model_id, *args, **kwargs)

    def transcribe_bytes(self, *_args, **_kwargs):
        raise RuntimeError(self.message)
