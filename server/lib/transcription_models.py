"""Install/remove transcription models and expose thread-safe task status."""
from __future__ import annotations

import os
import fcntl
import hashlib
from pathlib import Path
import json
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager

from .transcription_catalog import model_by_id, public_catalog
from . import service_manager, xdg

_lock = threading.Lock()
_registry_lock = threading.RLock()
_tasks: dict[str, dict] = {}
_activation_lock = threading.Lock()
_activation_inflight: set[tuple[str, str, int]] = set()
_activation_completed: set[tuple[str, str, int]] = set()
_activation_exhausted: set[tuple[str, str, int]] = set()
_activation_retry_after: dict[tuple[str, str, int], float] = {}
_activation_attempts: dict[tuple[str, str, int], int] = {}
DATA_ROOT = Path(os.environ.get("CLARP_SHARE_DIR", xdg.data_dir()))
REGISTRY = Path(os.environ.get(
    "CLARP_TRANSCRIPTION_REGISTRY", DATA_ROOT / "transcription-models.json"))
MANAGED_MODELS = Path(os.environ.get(
    "CLARP_TRANSCRIPTION_MODELS", DATA_ROOT / "models"))
INSTALL_HEARTBEAT_TIMEOUT_MS = 120_000
MONITOR_INTERVAL_SEC = 0.5
ACTIVATION_RETRY_DELAY_SEC = 30.0
ACTIVATION_MAX_ATTEMPTS = 3


def install_job_id(model_id: str) -> str:
    digest = hashlib.sha256(model_id.encode()).hexdigest()[:16]
    return f"transcription-model-install-{digest}"


def _managed_root(model_id: str) -> Path:
    return MANAGED_MODELS / model_id.replace(":", "--")


def _refuse(model_id: str, reason: str) -> None:
    from .log import log
    log("transcriptionModelFolderRefused", f"model={model_id} reason={reason}")
    raise ValueError(f"refusing to remove {model_id}: its folder is a link or outside the managed models folder")


def _deletable(model_id: str, folders) -> list[Path]:
    """Of ``folders`` (computed from the id, _own_folders), those that are there to delete.

    Each must be real and directly inside the managed root: one that is a symbolic link, or
    resolves anywhere else (the root itself, deeper, outside), refuses the whole removal before
    any file or the registry changes, with one log line and an error that names the model and
    not the paths (R112). A folder that is not there is simply not deleted."""
    root = MANAGED_MODELS.resolve()
    found = []
    for folder in folders:
        if folder.is_symlink():
            _refuse(model_id, "link")
        if not folder.exists():
            continue
        if folder.resolve().parent != root or folder.resolve().name != folder.name:
            _refuse(model_id, "outside")
        found.append(folder)
    return found


def _note_stored_path(model_id: str, stored: object) -> None:
    """The stored path decides nothing about what is deleted (R112): only the folders the id
    gives are. When it names anything else (or nothing), that is said once, and the removal
    goes on: a registered model can always be removed."""
    text = str(stored) if isinstance(stored, (str, os.PathLike)) else ""
    path = Path(text)
    if (text and path.is_absolute() and path.resolve() == path
            and any(path == folder for folder in _own_folders(model_id))):
        return
    from .log import log
    # Not a refusal (the removal goes on): its own name, so Refused counts refusals only.
    log("transcriptionModelStoredPathMismatch", f"model={model_id}")


def _shared_hf_folder(model_id: str, records) -> bool:
    """Whether another registered model's id gives the same Hugging Face folder as this one's
    (two faster-whisper ids of one repo): then that folder is not this removal's to delete."""
    mine = _own_folders(model_id)[1:]
    return bool(mine) and any(
        item.get("id") != model_id and _own_folders(str(item.get("id") or ""))[1:] == mine
        for item in records)


def _removal_folders(model_id: str, records) -> list[Path]:
    folders = _own_folders(model_id)
    if _shared_hf_folder(model_id, records):
        folders = folders[:1]
    return _deletable(model_id, folders)


def _own_folders(model_id: str) -> list[Path]:
    """The folders a model's id gives: its managed folder, and for a faster-whisper model the
    Hugging Face cache folder of its own repo under the managed root (models--<org>--<name>),
    where a model whose root was the Hugging Face hub is kept. Never a path read from a record."""
    folders = [_managed_root(model_id)]
    provider, _, model = model_id.partition(":")
    if provider == "faster-whisper" and model:
        folders.append(MANAGED_MODELS / ("models--" + _faster_whisper_repo(model).replace("/", "--")))
    return folders


def _activation_key(model_id: str, job: dict | None) -> tuple[str, str, int]:
    from . import db
    return (
        str(db.DB_PATH), model_id,
        int((job or {}).get("generation") or 0),
    )


def _schedule_activation(
    model_id: str, job: dict | None, on_complete, *, explicit_retry: bool = False,
) -> None:
    key = _activation_key(model_id, job)
    now = time.monotonic()
    with _activation_lock:
        if explicit_retry and key in _activation_exhausted:
            _activation_exhausted.discard(key)
            _activation_retry_after.pop(key, None)
            _activation_attempts.pop(key, None)
        if (
            key in _activation_inflight or key in _activation_completed
            or key in _activation_exhausted
        ):
            return
        if now < _activation_retry_after.get(key, 0):
            return
        _activation_inflight.add(key)
        attempt = _activation_attempts.get(key, 0) + 1
        _activation_attempts[key] = attempt

    def activate() -> None:
        try:
            if on_complete is not None:
                on_complete(model_id)
        except Exception as exc:  # noqa: BLE001
            retry = attempt < ACTIVATION_MAX_ATTEMPTS
            with _lock:
                _tasks[model_id] = {
                    "model_id": model_id,
                    "status": "installing" if retry else "failed",
                    "error": str(exc),
                    "job_generation": key[2],
                }
            if retry:
                with _activation_lock:
                    _activation_retry_after[key] = (
                        time.monotonic() + ACTIVATION_RETRY_DELAY_SEC)

                def retry_activation() -> None:
                    with _activation_lock:
                        _activation_retry_after.pop(key, None)
                    _schedule_activation(model_id, job, on_complete)

                timer = threading.Timer(
                    ACTIVATION_RETRY_DELAY_SEC, retry_activation)
                timer.daemon = True
                timer.start()
            else:
                with _activation_lock:
                    _activation_exhausted.add(key)
        else:
            with _lock:
                _tasks[model_id] = {
                    "model_id": model_id, "status": "installed", "error": "",
                    "job_generation": key[2],
                }
            with _activation_lock:
                _activation_completed.add(key)
                _activation_retry_after.pop(key, None)
                _activation_attempts.pop(key, None)
        finally:
            with _activation_lock:
                _activation_inflight.discard(key)

    threading.Thread(
        target=activate, daemon=True, name=f"stt-activate-{model_id}").start()


def recover_completed_installs(on_complete) -> None:
    from . import background_jobs
    for record in installed_records():
        model_id = str(record.get("id") or "")
        if not model_id:
            continue
        job = background_jobs.get(install_job_id(model_id))
        if job and job["status"] == "succeeded":
            _schedule_activation(model_id, job, on_complete)


def _worker_script() -> Path:
    configured = os.environ.get("CLARP_TRANSCRIPTION_MODEL_WORKER", "").strip()
    if configured:
        return Path(configured)
    if os.environ.get("CLARP_DEPLOYMENT_MODE") == "container":
        return Path(os.environ.get("CLARP_SHARE_DIR", "/opt/clarp")) / (
            "scripts/transcription_model_job.py")
    share = Path(os.environ.get("CLARP_SHARE_DIR", xdg.data_dir()))
    return share / "current/scripts/transcription_model_job.py"


@contextmanager
def _registry_file_lock(*, exclusive: bool):
    lock_path = REGISTRY.with_suffix(REGISTRY.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_registry() -> list[dict]:
    try:
        value = json.loads(REGISTRY.read_text())
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_registry(records: list[dict]) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    temporary = REGISTRY.with_name(
        f".{REGISTRY.name}.{os.getpid()}.{threading.get_ident()}.next")
    temporary.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")
    temporary.replace(REGISTRY)


def _valid_record(record: dict) -> bool:
    path = Path(str(record.get("_local_path") or ""))
    if record.get("provider") == "faster-whisper":
        return path.is_dir() and all(
            (path / name).is_file()
            for name in ("config.json", "model.bin", "tokenizer.json"))
    if record.get("provider") == "openai-whisper":
        return path.is_file() and path.suffix == ".pt" and path.stat().st_size > 1_000_000
    if record.get("provider") == "whisper.cpp":
        runtime = Path(str(record.get("_runtime_path") or ""))
        return (path.is_file() and path.suffix == ".bin"
                and path.stat().st_size > 1_000_000 and runtime.is_file()
                and os.access(runtime, os.X_OK))
    return False


def installed_records() -> list[dict]:
    with _registry_lock:
        with _registry_file_lock(exclusive=False):
            return [item for item in _read_registry() if _valid_record(item)]


def register(
    model_id: str, local_path: str, *, managed_root: str | None = None,
    runtime_path: str | None = None,
) -> dict:
    item = model_by_id(model_id)
    if item is None:
        try:
            provider, model = model_id.split(":", 1)
        except ValueError as exc:
            raise ValueError(f"invalid transcription model id: {model_id}") from exc
        if provider not in {"faster-whisper", "openai-whisper", "whisper.cpp"} or not model:
            raise ValueError(f"unsupported transcription provider: {provider}")
        item = {"id": model_id, "provider": provider, "model": model,
                "name": model, "weight": "custom", "languages": []}
    record = dict(item, _local_path=str(Path(local_path).expanduser().resolve()))
    if runtime_path:
        record["_runtime_path"] = str(Path(runtime_path).expanduser().resolve())
    if managed_root:
        record["_managed_root"] = str(Path(managed_root).expanduser().resolve())
    if not _valid_record(record):
        raise ValueError(f"model path is incomplete or incompatible: {local_path}")
    with _registry_lock:
        with _registry_file_lock(exclusive=True):
            records = [entry for entry in _read_registry() if entry.get("id") != model_id]
            records.append(record)
            _write_registry(records)
    return record


def rollback_terminal_install(
    model_id: str, *, job_id: str, generation: int,
    statuses: frozenset[str] = frozenset({"cancelled"}),
) -> bool:
    """Atomically detach terminal managed files from any future generation."""
    from . import background_jobs

    def quarantine() -> Path | None:
        with _registry_lock:
            with _registry_file_lock(exclusive=True):
                records = _read_registry()
                record = next(
                    (item for item in records if item.get("id") == model_id), None)
                tombstones = []
                if record is None or "_managed_root" in record:
                    # Only the folders the id gives, each real and inside the managed root
                    # (R112): with no record, the id's own folder; with one, as a removal.
                    if record is not None:
                        _note_stored_path(model_id, record["_managed_root"])
                    targets = (_removal_folders(model_id, records) if record is not None
                               else _deletable(model_id, [_managed_root(model_id)]))
                    for target in targets:
                        tombstone = target.with_name(
                            f".{target.name}.cancelled-{uuid.uuid4().hex}")
                        target.rename(tombstone)
                        tombstones.append(tombstone)
                if record:
                    _write_registry([
                        item for item in records if item.get("id") != model_id])
                return tombstones

    claimed, tombstones = background_jobs.run_terminal_generation_cleanup(
        job_id, generation=generation, cleanup=quarantine, statuses=statuses)
    for tombstone in (tombstones or []) if claimed else []:
        if tombstone.is_dir():
            shutil.rmtree(tombstone, ignore_errors=True)
        else:
            tombstone.unlink(missing_ok=True)
    return claimed


def rollback_cancelled_install(
    model_id: str, *, job_id: str, generation: int,
) -> bool:
    return rollback_terminal_install(
        model_id, job_id=job_id, generation=generation)


def catalog_status() -> list[dict]:
    installed = {item["id"] for item in installed_records()}
    with _lock:
        tasks = {key: dict(value) for key, value in _tasks.items()}
    result = public_catalog(installed)
    for item in result:
        task = tasks.get(item["id"])
        from . import background_jobs
        job = background_jobs.get(install_job_id(item["id"]))
        if job and job["status"] in background_jobs.ACTIVE_STATUSES:
            task = {
                "status": "installing", "error": "",
                "job_generation": job["generation"],
            }
        else:
            task_generation = (task or {}).get("job_generation")
            if (
                task_generation is not None and job
                and int(task_generation) != int(job["generation"])
            ):
                task = None
            if task is None and job and job["status"] == "failed":
                task = {
                    "status": "failed",
                    "error": job.get("terminal_reason") or "Model installation failed",
                }
            elif task is None and job and job["status"] == "cancelled":
                task = {"status": "failed", "error": "Model installation cancelled"}
        item["status"] = (task or {}).get(
            "status", "installed" if item["installed"] else
            "available" if item.get("supported", True) else "unsupported")
        if task and task.get("error"):
            item["error"] = task["error"]
    return result


# Systran publishes most faster-whisper conversions, but never built a turbo
# one, so the catalogue offered a model that could not be installed at all:
# the download failed with "Repository Not Found". Anything absent from this
# map keeps the Systran default.
_FASTER_WHISPER_REPOS: dict[str, str] = {
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
}


def _faster_whisper_repo(model: str) -> str:
    """The HuggingFace repo that actually holds this conversion."""
    return _FASTER_WHISPER_REPOS.get(model, f"Systran/faster-whisper-{model}")


def install(model_id: str) -> None:
    item = model_by_id(model_id)
    if item is None:
        raise ValueError(f"unsupported transcription model: {model_id}")
    if any(record["id"] == model_id for record in installed_records()):
        return
    from .transcription_catalog import platform_kind
    if platform_kind() not in item.get("platforms", ["linux", "macos"]):
        raise ValueError(
            f"{model_id} is not supported on {platform_kind()}")
    if item["provider"] == "faster-whisper":
        from huggingface_hub import snapshot_download
        managed_root = _managed_root(model_id)
        managed_root.mkdir(parents=True, exist_ok=True)
        try:
            local_path = snapshot_download(
                repo_id=_faster_whisper_repo(item["model"]),
                local_dir=str(managed_root),
            )
        except BaseException:
            shutil.rmtree(managed_root, ignore_errors=True)
            raise
    elif item["provider"] == "openai-whisper":
        try:
            import whisper  # type: ignore
        except ImportError as exc:
            raise ValueError(
                "OpenAI Whisper runtime is not installed on this server") from exc
        managed_root = _managed_root(model_id)
        managed_root.mkdir(parents=True, exist_ok=True)
        try:
            url = whisper._MODELS[item["model"]]  # type: ignore[attr-defined]
            local_path = whisper._download(  # type: ignore[attr-defined]
                url, str(managed_root), False)
        except BaseException:
            shutil.rmtree(managed_root, ignore_errors=True)
            raise
    elif item["provider"] == "whisper.cpp":
        from .whispercpp import install as install_whispercpp
        managed_root = _managed_root(model_id)
        try:
            local_path, runtime_path = install_whispercpp(
                item["model"], managed_root)
        except BaseException:
            shutil.rmtree(managed_root, ignore_errors=True)
            raise
    else:
        raise ValueError(f"unsupported managed provider: {item['provider']}")
    register(
        model_id, str(local_path), managed_root=str(managed_root),
        runtime_path=str(runtime_path) if item["provider"] == "whisper.cpp" else None)
    if not any(record["id"] == model_id for record in installed_records()):
        raise RuntimeError(f"download did not produce a valid model: {model_id}")


def _monitor_install(model_id: str, job_id: str, on_complete) -> None:
    from . import background_jobs
    while True:
        job = background_jobs.get(job_id)
        if not job or job["status"] not in background_jobs.ACTIVE_STATUSES:
            break
        time.sleep(MONITOR_INTERVAL_SEC)
    while job and _job_worker_alive(job):
        time.sleep(MONITOR_INTERVAL_SEC)
        current = background_jobs.get(job_id, reconcile=False)
        if not current or current["generation"] != job["generation"]:
            return
        job = current
    if job and job["status"] == "succeeded":
        _schedule_activation(model_id, job, on_complete)
        return
    elif job and job["status"] == "cancelled":
        status, error = "failed", "Model installation cancelled"
    else:
        status = "failed"
        error = (job or {}).get("terminal_reason") or "Model installation failed"
    if job and job["status"] in {"cancelled", "failed"}:
        rollback_terminal_install(
            model_id, job_id=job_id, generation=job["generation"],
            statuses=frozenset({job["status"]}))
    with _lock:
        _tasks[model_id] = {
            "model_id": model_id, "status": status, "error": error,
            "job_generation": int((job or {}).get("generation") or 0),
        }


def _start_monitor(model_id: str, job_id: str, on_complete) -> None:
    threading.Thread(
        target=_monitor_install, args=(model_id, job_id, on_complete),
        daemon=True, name=f"stt-install-monitor-{model_id}",
    ).start()


def _job_worker_alive(job: dict) -> bool:
    from . import background_jobs
    pid = int(job.get("worker_pid") or 0)
    token = str(job.get("worker_start_token") or "")
    return bool(pid and token and background_jobs.worker_is_alive(pid, token))


def start_install(
    model_id: str, *, session: str = "", computer_id: str = "", on_complete=None,
) -> dict:
    item = model_by_id(model_id)
    if item is None:
        raise ValueError(f"unsupported transcription model: {model_id}")
    from .transcription_catalog import platform_kind
    host = platform_kind()
    if host not in item.get("platforms", ["linux", "macos"]):
        raise ValueError(f"{model_id} is not supported on {host}")
    from . import background_jobs
    session = session.strip()
    computer_id = computer_id.strip()
    job_id = install_job_id(model_id)
    existing = background_jobs.get(job_id)
    installed = any(record["id"] == model_id for record in installed_records())
    if existing and existing["status"] in background_jobs.ACTIVE_STATUSES:
        with _lock:
            _tasks[model_id] = {
                "model_id": model_id, "status": "installing", "error": "",
                "job_handle": background_jobs.job_handle(existing),
                "job_generation": existing["generation"],
            }
        _start_monitor(model_id, job_id, on_complete)
        return dict(_tasks[model_id])
    if existing and existing["status"] in {"cancelled", "failed"}:
        pid = int(existing.get("worker_pid") or 0)
        token = str(existing.get("worker_start_token") or "")
        if pid and token and _job_worker_alive(existing):
            background_jobs.terminate_worker(pid, token)
            current = background_jobs.get(job_id, reconcile=False)
            if (
                not current or current["generation"] != existing["generation"]
                or _job_worker_alive(current)
            ):
                with _lock:
                    _tasks[model_id] = {
                        "model_id": model_id, "status": "failed",
                        "error": (
                            "Previous model installer could not be stopped; "
                            "retry shortly"
                        ),
                        "job_generation": existing["generation"],
                    }
                return dict(_tasks[model_id])
            existing = current
    if existing and existing["status"] in background_jobs.TERMINAL_STATUSES:
        desired_kind = "computer" if computer_id else "agent"
        owner_mismatch = (
            existing.get("owner_kind", "agent") != desired_kind
            or (
                desired_kind == "computer"
                and existing.get("computer_id") != computer_id
            )
            or (desired_kind == "agent" and existing.get("session") != session)
        )
        if owner_mismatch:
            existing = (
                background_jobs.reassign_terminal_computer_owner(
                    job_id, computer_id=computer_id)
                if computer_id else background_jobs.reassign_terminal_owner(
                    job_id, session=session)
            )
    if existing and existing["status"] in {"cancelled", "failed"}:
        rollback_terminal_install(
            model_id, job_id=job_id, generation=existing["generation"],
            statuses=frozenset({existing["status"]}))
        installed = any(
            record["id"] == model_id for record in installed_records())
    if installed and (not existing or existing["status"] == "succeeded"):
        _schedule_activation(
            model_id, existing, on_complete, explicit_retry=True)
        return {"model_id": model_id, "status": "installed", "error": ""}
    with _lock:
        current = _tasks.get(model_id)
        if current and current.get("status") == "installing":
            return dict(current)
    if not session and not computer_id:
        raise ValueError(
            "transcription model install requires an Agent or Computer owner")
    provisional_id = uuid.uuid4().hex
    with _lock:
        current = _tasks.get(model_id)
        if current and current.get("status") == "installing":
            return dict(current)
        _tasks[model_id] = {
            "model_id": model_id, "status": "installing", "error": "",
            "job_generation": int((existing or {}).get("generation") or 0),
            "provisional_id": provisional_id,
        }
    job = None
    try:
        common = {
            "job_id": job_id, "kind": "transcription-model-install",
            "title": f"Install transcription model: {model_id}",
            "detail": "Preparing model download", "status": "queued",
            "heartbeat_timeout_ms": INSTALL_HEARTBEAT_TIMEOUT_MS,
            "restart_cancelled": True,
            "metadata": {"model_id": model_id, "expire_queued": True},
        }
        job = (
            background_jobs.upsert_computer(computer_id=computer_id, **common)
            if computer_id else background_jobs.upsert(session=session, **common)
        )
        handle = background_jobs.job_handle(job)
        with _lock:
            _tasks[model_id] = {
                "model_id": model_id, "status": "installing", "error": "",
                "job_handle": handle, "job_generation": job["generation"],
                "provisional_id": provisional_id,
            }
        unit = (
            f"clarp-transcription-{hashlib.sha256(model_id.encode()).hexdigest()[:12]}"
            f"-g{job['generation']}"
        )
        worker_command = [
            sys.executable, str(_worker_script()),
            "--handle", handle, "--model-id", model_id,
        ]
        try:
            if os.environ.get("CLARP_DEPLOYMENT_MODE") == "container":
                subprocess.Popen(
                    worker_command, start_new_session=True, close_fds=True)
                launched = subprocess.CompletedProcess(
                    args=worker_command, returncode=0, stdout="", stderr="")
            else:
                ok, error = service_manager.launch_detached(
                    worker_command, unit=unit)
                launched = subprocess.CompletedProcess(
                    worker_command, 0 if ok else 1, "", error)
        except OSError as launch_error:
            launched = subprocess.CompletedProcess(
                args=worker_command, returncode=1, stdout="",
                stderr=str(launch_error))
        if launched.returncode != 0:
            background_jobs.finish(
                job_id, generation=job["generation"], status="failed",
                reason="systemd_launch_failed")
            with _lock:
                _tasks[model_id] = {
                    "model_id": model_id, "status": "failed",
                    "error": (
                        launched.stderr.strip() or "Could not start model installer"
                    ),
                    "job_generation": job["generation"],
                }
            return dict(_tasks[model_id])
        _start_monitor(model_id, job_id, on_complete)
        return dict(_tasks[model_id])
    except BaseException as exc:
        try:
            if job is not None:
                background_jobs.finish(
                    job_id, generation=job["generation"], status="failed",
                    reason=f"install_setup_failed:{type(exc).__name__}")
        finally:
            with _lock:
                current = _tasks.get(model_id)
                if current and current.get("provisional_id") == provisional_id:
                    _tasks.pop(model_id, None)
        raise


def remove(model_id: str, *, allow_active: bool = False) -> None:
    with _lock:
        if (_tasks.get(model_id) or {}).get("status") == "installing":
            raise ValueError(f"model installation is still in progress: {model_id}")
    from . import background_jobs
    active_job = background_jobs.get(install_job_id(model_id))
    if active_job and active_job["status"] in background_jobs.ACTIVE_STATUSES:
        raise ValueError(f"model installation is still in progress: {model_id}")
    from .config import load
    cfg = load()
    active_default = (f"{getattr(cfg, 'whisper_provider', 'faster-whisper')}:{cfg.whisper_model}"
                      if cfg.whisper_enabled else "")
    if model_id == active_default and not allow_active:
        raise ValueError(
            "cannot remove the configured server default; choose another "
            "default or disable server transcription first")
    with _registry_lock:
        with _registry_file_lock(exclusive=True):
            # Its line whether or not its files are whole (R138): a half download, a folder
            # deleted by hand or a runtime that moved fails _valid_record, and returning here
            # left that line and those files for ever, out of sight. What is deleted never
            # depends on the record's files: only the folders the id gives (R112).
            record = next((item for item in _read_registry() if item.get("id") == model_id), None)
            if record is None:
                return
            if "_managed_root" not in record:
                # A model registered from the user's own folder: only its registry line goes.
                _write_registry([
                    item for item in _read_registry() if item.get("id") != model_id])
                with _lock:
                    _tasks.pop(model_id, None)
                return
            # Only the folders the id gives, each real and inside the managed root (R112);
            # the stored path is compared and noted, never deleted, never a reason to refuse.
            records = _read_registry()
            targets = _removal_folders(model_id, records)
            _note_stored_path(model_id, record["_managed_root"])
            for target in targets:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            _write_registry([
                item for item in _read_registry() if item.get("id") != model_id])
    with _lock:
        _tasks.pop(model_id, None)
