"""Language-independent custom TTS adapter discovery and execution.

Adapters live in ``<config>/tts-adapters.d/<id>/manifest.json``.  Version 1
uses one executable and three mandatory JSON-over-stdin operations: ``voices``,
``preview`` and ``synthesize``.  Audio is written to a server-selected path;
provider secrets and Clarp authentication data never cross the protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any

from .deployment import LAYOUT


SCHEMA_VERSION = 1
ROOT = Path(os.environ.get(
    "CLARP_TTS_ADAPTERS_DIR", LAYOUT.config_dir / "tts-adapters.d"))
PREVIEW_CACHE = Path(os.environ.get(
    "CLARP_VOICE_PREVIEW_CACHE", LAYOUT.cache_dir / "voice-previews"))
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
REQUIRED_OPERATIONS = frozenset({"voices", "preview", "synthesize"})
MAX_VOICES = 500
MAX_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_AUDIO_BYTES = 64 * 1024 * 1024
VOICE_CACHE_SECONDS = 30
_VOICE_CACHE: dict[str, tuple[tuple[str, int, int], float, list[dict[str, str]]]] = {}
_VOICE_ERROR_CACHE: dict[str, tuple[tuple[str, int, int], float, str]] = {}


class AdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdapterManifest:
    id: str
    name: str
    description: str
    path: Path
    executable: Path
    default_voice: str
    can_fallback: bool
    license: str | None
    audio_format: str
    timeout_seconds: int
    protocol: str = "tts"

    def provider_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "kind": "custom",
            "streaming": False,
            "custom": True,
            "can_fallback": self.can_fallback,
            "supports_preview": True,
            "allows_custom_voice": False,
            "license": self.license,
            "installed": True,
            "available": True,
        }


def _manifest_path(source: Path) -> Path:
    source = source.expanduser()
    return source / "manifest.json" if source.is_dir() else source


def load_manifest(
    source: str | Path,
    *,
    reserved_ids: set[str] | frozenset[str] = frozenset(),
    portable: bool = False,
) -> AdapterManifest:
    path = _manifest_path(Path(source))
    if path.is_symlink() or not path.is_file():
        raise AdapterError(f"adapter manifest is not a regular file: {path}")
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AdapterError("adapter manifest is too large")
        data = json.loads(raw)
    except (OSError, ValueError, TypeError) as exc:
        raise AdapterError(f"invalid adapter manifest: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise AdapterError(f"adapter schema_version must be {SCHEMA_VERSION}")
    adapter_id = str(data.get("id") or "").strip().lower()
    if not ID_RE.fullmatch(adapter_id) or adapter_id in reserved_ids:
        raise AdapterError(f"invalid or reserved adapter id: {adapter_id or '<empty>'}")
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 80:
        raise AdapterError("adapter name must contain 1-80 characters")
    operations = data.get("operations")
    if not isinstance(operations, list) or not REQUIRED_OPERATIONS.issubset(
            {str(item).strip().lower() for item in operations}):
        raise AdapterError("adapter must declare voices, preview, and synthesize operations")
    executable_value = str(data.get("executable") or "").strip()
    if not executable_value:
        raise AdapterError("adapter executable is required")
    executable_input = Path(executable_value).expanduser()
    if portable and executable_input.is_absolute():
        raise AdapterError("installed adapter executables must use a relative path")
    executable = (executable_input if executable_input.is_absolute()
                  else path.parent / executable_input).resolve()
    if portable and path.parent.resolve() not in executable.parents:
        raise AdapterError("adapter executable must stay inside its source directory")
    try:
        mode = executable.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise AdapterError(f"adapter executable is unavailable: {executable}") from exc
    if executable.is_symlink() or not stat.S_ISREG(mode) or not os.access(executable, os.X_OK):
        raise AdapterError("adapter executable must be a regular executable file")
    audio_format = str(data.get("audio_format") or "audio/mpeg").strip().lower()
    if audio_format not in {"audio/mpeg", "audio/wav"}:
        raise AdapterError("adapter audio_format must be audio/mpeg or audio/wav")
    try:
        timeout = int(data.get("timeout_seconds", 120))
    except (TypeError, ValueError) as exc:
        raise AdapterError("timeout_seconds must be an integer") from exc
    if timeout < 5 or timeout > 600:
        raise AdapterError("timeout_seconds must be between 5 and 600")
    default_voice = str(data.get("default_voice") or "").strip()
    if not default_voice:
        raise AdapterError("adapter default_voice is required")
    return AdapterManifest(
        id=adapter_id,
        name=name,
        description=str(data.get("description") or "").strip()[:300],
        path=path.resolve(),
        executable=executable,
        default_voice=default_voice,
        can_fallback=bool(data.get("can_fallback", True)),
        license=(str(data.get("license") or "").strip() or None),
        audio_format=audio_format,
        timeout_seconds=timeout,
    )


def discover(*, reserved_ids: set[str] | frozenset[str] = frozenset()) -> list[AdapterManifest]:
    if not ROOT.is_dir() or ROOT.is_symlink():
        return []
    manifests: list[AdapterManifest] = []
    seen: set[str] = set()
    candidates = sorted(ROOT.glob("*/manifest.json")) + sorted(ROOT.glob("*.json"))
    for path in candidates:
        try:
            manifest = load_manifest(
                path, reserved_ids=reserved_ids, portable=True)
        except AdapterError:
            continue
        if manifest.id in seen:
            continue
        seen.add(manifest.id)
        manifests.append(manifest)
    return manifests


def inventory(*, reserved_ids: set[str] | frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Return every installed package, including damaged manifests."""
    if not ROOT.is_dir() or ROOT.is_symlink():
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    candidates = sorted(ROOT.glob("*/manifest.json")) + sorted(ROOT.glob("*.json"))
    for path in candidates:
        try:
            manifest = load_manifest(
                path, reserved_ids=reserved_ids, portable=True)
            row = manifest.provider_row() | {
                "default_voice": manifest.default_voice}
        except AdapterError as exc:
            adapter_id, name, description = _manifest_identity(path)
            if (not ID_RE.fullmatch(adapter_id)
                    or adapter_id in reserved_ids or adapter_id in seen):
                import hashlib
                adapter_id = "invalid." + hashlib.sha256(
                    str(path).encode()).hexdigest()[:12]
            row = {
                "id": adapter_id,
                "name": name or path.parent.name or "Invalid adapter",
                "description": description,
                "kind": "custom",
                "streaming": False,
                "custom": True,
                "can_fallback": False,
                "supports_preview": False,
                "allows_custom_voice": False,
                "license": None,
                "installed": True,
                "available": False,
                "error": str(exc)[:300],
            }
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        rows.append(row)
    return rows


def _manifest_identity(path: Path) -> tuple[str, str, str]:
    try:
        data = json.loads(path.read_bytes()[:MAX_RESPONSE_BYTES])
    except (OSError, ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return (
        str(data.get("id") or "").strip().lower(),
        str(data.get("name") or "").strip()[:80],
        str(data.get("description") or "").strip()[:300],
    )


def get(adapter_id: str, *, reserved_ids: set[str] | frozenset[str] = frozenset()) -> AdapterManifest | None:
    normalized = adapter_id.strip().lower()
    return next((item for item in discover(reserved_ids=reserved_ids)
                 if item.id == normalized), None)


def _environment(manifest: AdapterManifest) -> dict[str, str]:
    keep = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE",
            "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")
    result = {name: os.environ[name] for name in keep if os.environ.get(name)}
    protocol = getattr(manifest, "protocol", "tts")
    result["CLARP_ADAPTER_PROTOCOL"] = protocol
    result["CLARP_ADAPTER_ID"] = manifest.id
    result["CLARP_ADAPTER_ROOT"] = str(manifest.path.parent)
    if protocol == "tts":
        result["CLARP_TTS_ADAPTER_ID"] = manifest.id
        result["CLARP_TTS_ADAPTER_ROOT"] = str(manifest.path.parent)
    return result


def _request(manifest: AdapterManifest, payload: dict[str, Any], *, timeout: int | None = None) -> dict:
    encoded_request = json.dumps(payload, ensure_ascii=False).encode()
    if len(encoded_request) > MAX_RESPONSE_BYTES:
        raise AdapterError(f"{manifest.name} adapter request is too large")
    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                [str(manifest.executable)],
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                cwd=manifest.path.parent,
                env=_environment(manifest),
                start_new_session=True,
            )
            assert process.stdin is not None
            try:
                process.stdin.write(encoded_request)
                process.stdin.close()
            except OSError as exc:
                _kill_process_group(process)
                process.wait(timeout=5)
                raise AdapterError(
                    f"{manifest.name} adapter rejected its request: {exc}") from exc
            deadline = time.monotonic() + (timeout or manifest.timeout_seconds)
            requested_output = Path(str(payload.get("output_path") or ""))
            oversized = False
            while process.poll() is None:
                if (stdout_file.tell() > MAX_RESPONSE_BYTES
                        or stderr_file.tell() > MAX_RESPONSE_BYTES):
                    oversized = True
                    _kill_process_group(process)
                    break
                if requested_output.is_file():
                    try:
                        output_size = requested_output.stat(
                            follow_symlinks=False).st_size
                    except OSError:
                        output_size = 0
                    if output_size > MAX_AUDIO_BYTES:
                        oversized = True
                        _kill_process_group(process)
                        break
                if time.monotonic() >= deadline:
                    _kill_process_group(process)
                    process.wait(timeout=5)
                    raise AdapterError(f"{manifest.name} adapter timed out")
                time.sleep(0.02)
            process.wait(timeout=5)
            # Adapters are one-shot operations. A successful launcher must not
            # leave forked workers holding temporary user audio or running past
            # Clarp's timeout/size fences.
            _kill_process_group(process)
            if oversized:
                raise AdapterError(f"{manifest.name} adapter response is too large")
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(MAX_RESPONSE_BYTES + 1)
            stderr = stderr_file.read(MAX_RESPONSE_BYTES + 1)
            returncode = process.returncode
    except AdapterError:
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"{manifest.name} adapter failed to run: {exc}") from exc
    if len(stdout) > MAX_RESPONSE_BYTES or len(stderr) > MAX_RESPONSE_BYTES:
        raise AdapterError(f"{manifest.name} adapter response is too large")
    if returncode != 0:
        detail = stderr.decode(errors="replace").strip()[:500] or f"exit {returncode}"
        raise AdapterError(f"{manifest.name} adapter failed: {detail}")
    try:
        response = json.loads(stdout or b"{}")
    except (ValueError, TypeError) as exc:
        raise AdapterError(f"{manifest.name} adapter returned invalid JSON") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise AdapterError(str(response.get("error") or "adapter operation failed")[:500])
    return response


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()


def voices(manifest: AdapterManifest, *, force: bool = False) -> list[dict[str, str]]:
    try:
        signature = (
            str(manifest.path),
            manifest.path.stat().st_mtime_ns,
            manifest.executable.stat().st_mtime_ns,
        )
    except OSError as exc:
        raise AdapterError(f"{manifest.name} adapter files are unavailable") from exc
    cached = _VOICE_CACHE.get(manifest.id)
    if (not force and cached is not None and cached[0] == signature
            and cached[1] > time.monotonic()):
        return [dict(item) for item in cached[2]]
    cached_error = _VOICE_ERROR_CACHE.get(manifest.id)
    if (not force and cached_error is not None
            and cached_error[0] == signature
            and cached_error[1] > time.monotonic()):
        raise AdapterError(cached_error[2])
    try:
        result = _uncached_voices(manifest)
    except AdapterError as exc:
        _VOICE_ERROR_CACHE[manifest.id] = (
            signature, time.monotonic() + VOICE_CACHE_SECONDS, str(exc))
        raise
    _VOICE_ERROR_CACHE.pop(manifest.id, None)
    _VOICE_CACHE[manifest.id] = (
        signature, time.monotonic() + VOICE_CACHE_SECONDS,
        [dict(item) for item in result])
    return result


def _uncached_voices(manifest: AdapterManifest) -> list[dict[str, str]]:
    response = _request(
        manifest, {"schema_version": 1, "operation": "voices"}, timeout=30)
    raw = response.get("voices")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_VOICES:
        raise AdapterError("adapter voices must be a non-empty bounded list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise AdapterError("adapter voice entries must be objects")
        voice_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not voice_id or len(voice_id) > 256 or not name or len(name) > 100:
            raise AdapterError("adapter voice id or name is invalid")
        if voice_id in seen:
            raise AdapterError(f"duplicate adapter voice id: {voice_id}")
        seen.add(voice_id)
        result.append({
            "id": voice_id,
            "name": name,
            "description": str(item.get("description") or "").strip()[:300],
        })
    if manifest.default_voice not in seen:
        raise AdapterError("default_voice is not present in the voices response")
    return result


def _audio_operation(
    manifest: AdapterManifest,
    operation: str,
    *,
    text: str,
    voice: str,
    out_path: Path,
    on_chunk=None,
) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = ".wav" if manifest.audio_format == "audio/wav" else ".mp3"
    with tempfile.TemporaryDirectory(prefix=f"clarp-adapter-{manifest.id}-") as temp:
        produced = Path(temp) / f"audio{suffix}"
        _request(manifest, {
            "schema_version": 1,
            "operation": operation,
            "text": text,
            "voice_id": voice or manifest.default_voice,
            "output_path": str(produced),
            "audio_format": manifest.audio_format,
        })
        try:
            info = produced.stat(follow_symlinks=False)
        except OSError as exc:
            raise AdapterError("adapter did not produce its requested audio file") from exc
        if produced.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise AdapterError("adapter audio output must be a regular file")
        if info.st_size <= 0 or info.st_size > MAX_AUDIO_BYTES:
            raise AdapterError("adapter audio output size is invalid")
        if manifest.audio_format == "audio/wav":
            from .tts_providers import _wav_to_mp3
            _wav_to_mp3(produced, out_path)
        else:
            shutil.copyfile(produced, out_path)
    try:
        normalized_size = out_path.stat(follow_symlinks=False).st_size
    except OSError as exc:
        raise AdapterError("normalized adapter audio output is unavailable") from exc
    if normalized_size <= 0 or normalized_size > MAX_AUDIO_BYTES:
        raise AdapterError("normalized adapter audio output is invalid")
    _validate_mp3(out_path)
    if on_chunk is not None:
        with out_path.open("rb") as source:
            index = 0
            while chunk := source.read(64 * 1024):
                on_chunk(index, chunk)
                index += 1
    return normalized_size


def _validate_mp3(path: Path) -> None:
    try:
        import av
        with av.open(str(path)) as container:
            stream = next(
                item for item in container.streams if item.type == "audio")
            if next(container.decode(stream), None) is None:
                raise AdapterError("adapter MP3 contains no decodable audio frames")
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError("adapter did not produce playable MP3 audio") from exc


def synthesize(manifest: AdapterManifest, *, text: str, voice: str,
               out_path: Path, on_chunk=None) -> int:
    return _audio_operation(
        manifest, "synthesize", text=text, voice=voice,
        out_path=out_path, on_chunk=on_chunk)


def preview(manifest: AdapterManifest, *, text: str, voice: str,
            out_path: Path) -> int:
    return _audio_operation(
        manifest, "preview", text=text, voice=voice, out_path=out_path)


def _safe_source_tree(source: Path) -> None:
    files = 0
    total = 0
    for path in source.rglob("*"):
        if path.is_symlink():
            raise AdapterError(f"adapter packages cannot contain symlinks: {path}")
        if path.is_file():
            files += 1
            total += path.stat().st_size
        if files > 100 or total > 50 * 1024 * 1024:
            raise AdapterError("adapter package exceeds the file or size limit")


def install(source: str | Path, *, reserved_ids: set[str] | frozenset[str],
            replace: bool = False) -> AdapterManifest:
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_dir():
        raise AdapterError("adapter installation source must be a directory")
    _safe_source_tree(source_path)
    manifest = load_manifest(source_path, reserved_ids=reserved_ids, portable=True)
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = ROOT / manifest.id
    if destination.exists() and not replace:
        raise AdapterError(f"adapter is already installed: {manifest.id}")
    staging = ROOT / f".{manifest.id}.{os.getpid()}.installing"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        shutil.copytree(source_path, staging)
        staged_manifest = load_manifest(
            staging, reserved_ids=reserved_ids, portable=True)
        # Installation is transactional: every mandatory operation must work
        # before an existing adapter can be replaced.
        test_adapter(staged_manifest)
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
    _VOICE_CACHE.pop(manifest.id, None)
    _VOICE_ERROR_CACHE.pop(manifest.id, None)
    preview_cache = PREVIEW_CACHE
    if preview_cache.is_dir() and not preview_cache.is_symlink():
        shutil.rmtree(preview_cache)
    return load_manifest(destination, reserved_ids=reserved_ids)


def remove(adapter_id: str, *, reserved_ids: set[str] | frozenset[str]) -> None:
    normalized = adapter_id.strip().lower()
    manifest = get(normalized, reserved_ids=reserved_ids)
    candidate = manifest.path if manifest is not None else None
    if candidate is None:
        import hashlib
        for path in sorted(ROOT.glob("*/manifest.json")) + sorted(ROOT.glob("*.json")):
            raw_id = _manifest_identity(path)[0]
            placeholder = "invalid." + hashlib.sha256(
                str(path).encode()).hexdigest()[:12]
            if normalized in {raw_id, placeholder}:
                candidate = path
                break
    if candidate is None:
        raise AdapterError(f"custom adapter is not installed: {adapter_id}")
    destination = candidate.parent.resolve()
    root = ROOT.resolve()
    if destination.parent == root:
        shutil.rmtree(destination)
    elif candidate.parent.resolve() == root and candidate.is_file():
        candidate.unlink()
    else:
        raise AdapterError("only managed adapter packages can be removed")
    _VOICE_CACHE.pop(normalized, None)
    _VOICE_ERROR_CACHE.pop(normalized, None)


def test_adapter(manifest: AdapterManifest) -> dict[str, Any]:
    rows = voices(manifest, force=True)
    voice = manifest.default_voice
    with tempfile.TemporaryDirectory(prefix="clarp-adapter-test-") as temp:
        root = Path(temp)
        preview_bytes = preview(
            manifest, text="This is a Clarp voice preview.", voice=voice,
            out_path=root / "preview.mp3")
        synthesis_bytes = synthesize(
            manifest, text="Custom voice adapter test successful.", voice=voice,
            out_path=root / "synthesis.mp3")
    return {
        "ok": True,
        "id": manifest.id,
        "voices": len(rows),
        "tested_voice": voice,
        "preview_bytes": preview_bytes,
        "synthesis_bytes": synthesis_bytes,
    }
