"""Custom speech-to-text adapters using Clarp's bounded executable protocol."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import math
import re
import shutil
import stat
import tempfile
import threading
import time
from typing import Any
import wave

from .custom_tts_adapters import (
    AdapterError, ID_RE, MAX_RESPONSE_BYTES, _request, _safe_source_tree)
from .deployment import LAYOUT


SCHEMA_VERSION = 1
ROOT = Path(os.environ.get(
    "CLARP_STT_ADAPTERS_DIR", LAYOUT.config_dir / "stt-adapters.d"))
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REQUIRED_OPERATIONS = frozenset({"models", "transcribe"})
MAX_MODELS = 100
_CACHE_SECONDS = 30
_MODEL_CACHE: dict[str, tuple[tuple[str, int, int], float, list[dict]]] = {}
_ERROR_CACHE: dict[str, tuple[tuple[str, int, int], float, str]] = {}
_INFERENCE_LOCK_GUARD = threading.Lock()
_INFERENCE_LOCKS: dict[str, threading.Lock] = {}


def inference_lock(adapter_id: str) -> threading.Lock:
    with _INFERENCE_LOCK_GUARD:
        return _INFERENCE_LOCKS.setdefault(adapter_id, threading.Lock())


@dataclass(frozen=True)
class STTAdapterManifest:
    id: str
    name: str
    description: str
    path: Path
    executable: Path
    default_model: str
    timeout_seconds: int
    protocol: str = "stt"

    def provider_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "custom": True,
            "installed": True,
            "available": True,
            "default_model": f"{self.id}:{self.default_model}",
        }


def _manifest_path(source: Path) -> Path:
    source = source.expanduser()
    return source / "manifest.json" if source.is_dir() else source


def load_manifest(source: str | Path, *, portable: bool = False) -> STTAdapterManifest:
    path = _manifest_path(Path(source))
    if path.is_symlink() or not path.is_file():
        raise AdapterError(f"STT adapter manifest is not a regular file: {path}")
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AdapterError("STT adapter manifest is too large")
        data = json.loads(raw)
    except (OSError, ValueError, TypeError) as exc:
        raise AdapterError(f"invalid STT adapter manifest: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise AdapterError(f"STT adapter schema_version must be {SCHEMA_VERSION}")
    adapter_id = str(data.get("id") or "").strip().lower()
    if not ID_RE.fullmatch(adapter_id) or not adapter_id.startswith("custom."):
        raise AdapterError("STT adapter id must be a valid custom.* identifier")
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 80:
        raise AdapterError("STT adapter name must contain 1-80 characters")
    operations = data.get("operations")
    if not isinstance(operations, list) or not REQUIRED_OPERATIONS.issubset(
            {str(item).strip().lower() for item in operations}):
        raise AdapterError("STT adapter must declare models and transcribe operations")
    executable_value = str(data.get("executable") or "").strip()
    executable_input = Path(executable_value).expanduser()
    if not executable_value:
        raise AdapterError("STT adapter executable is required")
    if portable and executable_input.is_absolute():
        raise AdapterError("installed STT adapter executables must use a relative path")
    executable = (executable_input if executable_input.is_absolute()
                  else path.parent / executable_input).resolve()
    if portable and path.parent.resolve() not in executable.parents:
        raise AdapterError("STT adapter executable must stay inside its package")
    try:
        mode = executable.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise AdapterError(f"STT adapter executable is unavailable: {executable}") from exc
    if executable.is_symlink() or not stat.S_ISREG(mode) or not os.access(executable, os.X_OK):
        raise AdapterError("STT adapter executable must be a regular executable file")
    default_model = str(data.get("default_model") or "").strip()
    if not MODEL_ID_RE.fullmatch(default_model):
        raise AdapterError("STT adapter default_model is required and must be a local model id")
    try:
        timeout = int(data.get("timeout_seconds", 120))
    except (TypeError, ValueError) as exc:
        raise AdapterError("STT adapter timeout_seconds must be an integer") from exc
    if timeout < 5 or timeout > 600:
        raise AdapterError("STT adapter timeout_seconds must be between 5 and 600")
    return STTAdapterManifest(
        id=adapter_id, name=name,
        description=str(data.get("description") or "").strip()[:300],
        path=path.resolve(), executable=executable,
        default_model=default_model, timeout_seconds=timeout)


def _candidates() -> list[Path]:
    if not ROOT.is_dir() or ROOT.is_symlink():
        return []
    return sorted(ROOT.glob("*/manifest.json")) + sorted(ROOT.glob("*.json"))


def discover() -> list[STTAdapterManifest]:
    result = []
    seen = set()
    for path in _candidates():
        try:
            manifest = load_manifest(path, portable=True)
        except AdapterError:
            continue
        if manifest.id not in seen:
            seen.add(manifest.id)
            result.append(manifest)
    return result


def get(adapter_id: str) -> STTAdapterManifest | None:
    normalized = adapter_id.strip().lower()
    return next((item for item in discover() if item.id == normalized), None)


def _signature(manifest: STTAdapterManifest) -> tuple[str, int, int]:
    try:
        return (str(manifest.path), manifest.path.stat().st_mtime_ns,
                manifest.executable.stat().st_mtime_ns)
    except OSError as exc:
        raise AdapterError(f"{manifest.name} STT adapter files are unavailable") from exc


def models(manifest: STTAdapterManifest, *, force: bool = False) -> list[dict]:
    signature = _signature(manifest)
    cached = _MODEL_CACHE.get(manifest.id)
    if not force and cached and cached[0] == signature and cached[1] > time.monotonic():
        return [dict(item) for item in cached[2]]
    cached_error = _ERROR_CACHE.get(manifest.id)
    if (not force and cached_error and cached_error[0] == signature
            and cached_error[1] > time.monotonic()):
        raise AdapterError(cached_error[2])
    try:
        response = _request(
            manifest, {"schema_version": 1, "operation": "models"}, timeout=30)
        raw = response.get("models")
        if not isinstance(raw, list) or not raw or len(raw) > MAX_MODELS:
            raise AdapterError("STT adapter models must be a non-empty bounded list")
        result = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict):
                raise AdapterError("STT adapter model entries must be objects")
            local_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            if not MODEL_ID_RE.fullmatch(local_id) or not name or len(name) > 100:
                raise AdapterError("STT adapter model id or name is invalid")
            if local_id in seen:
                raise AdapterError(f"duplicate STT adapter model id: {local_id}")
            seen.add(local_id)
            result.append({
                "id": f"{manifest.id}:{local_id}",
                "name": name,
                "provider": manifest.id,
                "model": local_id,
                "weight": str(item.get("weight") or "adapter")[:40],
                "languages": [str(value)[:30] for value in (
                    item.get("languages") or []) if str(value).strip()][:50],
                "description": str(item.get("description") or "").strip()[:300],
                "installed": True,
                "status": "installed",
                "custom": True,
                "adapter_name": manifest.name,
            })
        if manifest.default_model not in seen:
            raise AdapterError("STT adapter default_model is absent from models response")
    except AdapterError as exc:
        _ERROR_CACHE[manifest.id] = (
            signature, time.monotonic() + _CACHE_SECONDS, str(exc))
        raise
    _ERROR_CACHE.pop(manifest.id, None)
    _MODEL_CACHE[manifest.id] = (
        signature, time.monotonic() + _CACHE_SECONDS,
        [dict(item) for item in result])
    return result


def catalog_models() -> list[dict]:
    result = []
    for manifest in discover():
        try:
            result.extend(models(manifest))
        except AdapterError as exc:
            # Preserve configured per-Computer selections through a transient
            # adapter outage. Runtime validation still calls models() and fails
            # closed; this row is capability/UI continuity only.
            result.append({
                "id": f"{manifest.id}:{manifest.default_model}",
                "name": manifest.name,
                "provider": manifest.id,
                "model": manifest.default_model,
                "weight": "adapter",
                "languages": [],
                "description": manifest.description,
                "installed": True,
                "status": "unavailable",
                "error": str(exc)[:300],
                "custom": True,
                "adapter_name": manifest.name,
            })
    return result


def transcribe(
    manifest: STTAdapterManifest, *, model_id: str, audio_bytes: bytes,
    content_type: str, vocab_prompt: str,
) -> tuple[str, bool, float]:
    local_id = model_id.split(":", 1)[1] if ":" in model_id else model_id
    allowed = {item["model"] for item in models(manifest)}
    if local_id not in allowed:
        raise AdapterError(f"STT adapter model is unavailable: {local_id}")
    suffix = ".m4a" if "mp4" in content_type or "m4a" in content_type \
        else ".wav" if "wav" in content_type else ".ogg" if "ogg" in content_type \
        else ".webm"
    with tempfile.TemporaryDirectory(prefix=f"clarp-stt-{manifest.id}-") as temp:
        audio_path = Path(temp) / f"audio{suffix}"
        audio_path.write_bytes(audio_bytes)
        response = _request(manifest, {
            "schema_version": 1,
            "operation": "transcribe",
            "model_id": local_id,
            "audio_path": str(audio_path),
            "content_type": content_type,
            "vocabulary_prompt": vocab_prompt,
        })
    raw_text = response.get("text", "")
    if not isinstance(raw_text, str):
        raise AdapterError("STT adapter text must be a string")
    text = raw_text.strip()
    from .hallucinations import is_pure_hallucination
    if is_pure_hallucination(text):
        text = ""
    if len(text) > 200_000:
        raise AdapterError("STT adapter transcript is too large")
    try:
        duration = float(response.get("duration_seconds") or 0.0)
    except (TypeError, ValueError) as exc:
        raise AdapterError("STT adapter duration_seconds is invalid") from exc
    if not math.isfinite(duration) or duration < 0 or duration > 3600:
        raise AdapterError("STT adapter duration_seconds is out of range")
    terminal = response.get("ends_terminal", False)
    if not isinstance(terminal, bool):
        raise AdapterError("STT adapter ends_terminal must be a boolean")
    return text, terminal, duration


def test_adapter(manifest: STTAdapterManifest) -> dict:
    rows = models(manifest, force=True)
    with tempfile.TemporaryDirectory(prefix="clarp-stt-test-") as temp:
        audio = Path(temp) / "silence.wav"
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(bytes(8_000))
        text, _terminal, _duration = transcribe(
            manifest,
            model_id=f"{manifest.id}:{manifest.default_model}",
            audio_bytes=audio.read_bytes(), content_type="audio/wav",
            vocab_prompt="Clarp adapter validation")
    return {"ok": True, "id": manifest.id, "models": len(rows),
            "default_model": manifest.default_model,
            "test_transcript_chars": len(text)}


def install(source: str | Path, *, replace: bool = False) -> STTAdapterManifest:
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_dir():
        raise AdapterError("STT adapter installation source must be a directory")
    _safe_source_tree(source_path)
    manifest = load_manifest(source_path, portable=True)
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = ROOT / manifest.id
    if destination.exists() and not replace:
        raise AdapterError(f"STT adapter is already installed: {manifest.id}")
    staging = ROOT / f".{manifest.id}.{os.getpid()}.installing"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        shutil.copytree(source_path, staging)
        staged = load_manifest(staging, portable=True)
        test_adapter(staged)
        if destination.exists() and replace:
            from .config import load as load_config
            cfg = load_config()
            if getattr(cfg, "whisper_provider", "") == manifest.id:
                configured = f"{manifest.id}:{cfg.whisper_model}"
                if configured not in {
                        row["id"] for row in models(staged, force=True)}:
                    raise AdapterError(
                        "replacement STT adapter does not provide the active "
                        f"model: {configured}")
        if destination.exists():
            backup = ROOT / f".{manifest.id}.{os.getpid()}.previous"
            destination.replace(backup)
            try:
                staging.replace(destination)
            except BaseException:
                backup.replace(destination)
                raise
            shutil.rmtree(backup)
        else:
            staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    _MODEL_CACHE.pop(manifest.id, None)
    _ERROR_CACHE.pop(manifest.id, None)
    return load_manifest(destination, portable=True)


def inventory() -> list[dict]:
    valid = {item.id: item for item in discover()}
    rows = []
    for path in _candidates():
        try:
            data = json.loads(path.read_bytes()[:MAX_RESPONSE_BYTES])
        except (OSError, ValueError, TypeError):
            data = {}
        raw_id = str((data or {}).get("id") or "").strip().lower()
        manifest = valid.get(raw_id)
        if manifest:
            row = manifest.provider_row()
            try:
                row["models"] = len(models(manifest))
            except AdapterError as exc:
                row["available"] = False
                row["error"] = str(exc)[:300]
        else:
            identity = raw_id if ID_RE.fullmatch(raw_id) else (
                "invalid." + hashlib.sha256(str(path).encode()).hexdigest()[:12])
            row = {"id": identity, "name": str((data or {}).get("name") or
                   path.parent.name)[:80], "description": "", "custom": True,
                   "installed": True, "available": False,
                   "error": "Invalid STT adapter manifest", "models": 0}
        if not any(existing["id"] == row["id"] for existing in rows):
            rows.append(row)
    return rows


def remove(adapter_id: str) -> None:
    normalized = adapter_id.strip().lower()
    candidate = None
    for path in _candidates():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            data = {}
        raw_id = str(data.get("id") or "").strip().lower() \
            if isinstance(data, dict) else ""
        placeholder = "invalid." + hashlib.sha256(
            str(path).encode()).hexdigest()[:12]
        if normalized in {raw_id, placeholder}:
            candidate = path
            break
    if candidate is None:
        raise AdapterError(f"custom STT adapter is not installed: {adapter_id}")
    destination = candidate.parent.resolve()
    root = ROOT.resolve()
    if destination.parent == root:
        shutil.rmtree(destination)
    elif destination == root:
        candidate.unlink()
    else:
        raise AdapterError("only managed STT adapter packages can be removed")
    _MODEL_CACHE.pop(normalized, None)
    _ERROR_CACHE.pop(normalized, None)
