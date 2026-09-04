from __future__ import annotations

import pathlib
import sys
import json
import os
import sqlite3
import subprocess

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import agents, background_jobs, service_manager, transcription_models  # noqa: E402
from lib.transcription_catalog import CATALOG, public_catalog, recommended_model_id  # noqa: E402


@pytest.fixture(autouse=True)
def _capture_platform_worker_launch(monkeypatch):
    # Async install mechanics use the Faster-Whisper fixtures unless a test
    # explicitly selects macOS/whisper.cpp. Keep them deterministic on both CI
    # operating systems now that production rejects cross-platform installs.
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "linux")

    def launch(command, **_kwargs):
        result = transcription_models.subprocess.run(
            command, text=True, capture_output=True, check=False)
        return result.returncode == 0, result.stderr or ""

    monkeypatch.setattr(service_manager, "launch_detached", launch)


def test_catalog_status_distinguishes_installed_and_available(monkeypatch):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "linux")
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [
        {"id": "faster-whisper:small.en"},
    ])
    rows = {item["id"]: item for item in transcription_models.catalog_status()}
    assert rows["faster-whisper:small.en"]["installed"] is True
    assert rows["faster-whisper:medium"]["installed"] is False
    assert rows["faster-whisper:medium"]["status"] == "available"


def test_transcription_defaults_follow_explicit_share_root(tmp_path):
    environment = dict(os.environ)
    environment["CLARP_SHARE_DIR"] = str(tmp_path / "isolated-share")
    environment["PYTHONPATH"] = str(
        pathlib.Path(__file__).resolve().parents[2] / "server")
    output = subprocess.check_output([
        sys.executable, "-c",
        "from lib import transcription_models as m; print(m.REGISTRY); print(m.MANAGED_MODELS)",
    ], text=True, env=environment).splitlines()
    assert output == [
        str(tmp_path / "isolated-share/transcription-models.json"),
        str(tmp_path / "isolated-share/models"),
    ]


def test_macos_catalog_recommends_whisper_cpp(monkeypatch):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "macos")
    assert recommended_model_id() == "whisper.cpp:small.en"
    rows = {item["id"]: item for item in public_catalog()}
    assert rows["whisper.cpp:small.en"]["recommended"] is True
    assert rows["whisper.cpp:small.en"]["supported"] is True
    assert rows["faster-whisper:small.en"]["supported"] is False


def test_catalog_status_recovers_installing_state_from_durable_job(monkeypatch):
    model_id = "faster-whisper:medium"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(model_id),
        kind="transcription-model-install", title="Install", status="running",
        worker_pid=4242, worker_start_token="boot:worker")
    with transcription_models._lock:
        transcription_models._tasks.pop(model_id, None)

    rows = {item["id"]: item for item in transcription_models.catalog_status()}

    assert rows[model_id]["status"] == "installing"


def test_active_retry_overrides_stale_terminal_monitor_cache(monkeypatch):
    model_id = "faster-whisper:medium"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    old = background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(model_id),
        kind="transcription-model-install", title="Install", status="queued")
    background_jobs.cancel(old["job_id"])
    current = background_jobs.upsert(
        session="mike", job_id=old["job_id"],
        kind="transcription-model-install", title="Install", status="queued",
        restart_cancelled=True)
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    with transcription_models._lock:
        transcription_models._tasks[model_id] = {
            "model_id": model_id, "status": "failed", "error": "cancelled",
            "job_generation": old["generation"],
        }

    rows = {item["id"]: item for item in transcription_models.catalog_status()}

    assert current["generation"] == old["generation"] + 1
    assert rows[model_id]["status"] == "installing"
    assert "error" not in rows[model_id]


def test_start_install_deduplicates_running_task(monkeypatch):
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    monkeypatch.setattr(transcription_models, "MONITOR_INTERVAL_SEC", 0.01)
    with transcription_models._lock:
        transcription_models._tasks["faster-whisper:medium"] = {
            "model_id": "faster-whisper:medium", "status": "installing", "error": "",
        }
    first = transcription_models.start_install("faster-whisper:medium")
    second = transcription_models.start_install("faster-whisper:medium")
    assert first == second


def test_start_install_registers_queued_generation_and_transient_worker(
    monkeypatch,
):
    model_id = "faster-whisper:medium"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    calls = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        transcription_models.subprocess, "run",
        lambda command, **_kwargs: calls.append(command) or Result())
    monkeypatch.setattr(transcription_models, "_start_monitor", lambda *_args: None)
    with transcription_models._lock:
        transcription_models._tasks.pop(model_id, None)

    result = transcription_models.start_install(model_id, session="mike")

    job_id, generation = background_jobs.parse_job_handle(result["job_handle"])
    job = background_jobs.get(job_id, reconcile=False)
    assert job["status"] == "queued"
    assert job["generation"] == generation
    assert job["kind"] == "transcription-model-install"
    assert job["metadata"]["model_id"] == model_id
    assert calls[0][-4:] == [
        "--handle", result["job_handle"], "--model-id", model_id,
    ]


def test_install_worker_launch_failure_is_terminal(monkeypatch):
    model_id = "faster-whisper:medium"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])

    class Result:
        returncode = 1
        stderr = "systemd failed"

    monkeypatch.setattr(
        transcription_models.subprocess, "run", lambda *_args, **_kwargs: Result())
    with transcription_models._lock:
        transcription_models._tasks.pop(model_id, None)

    result = transcription_models.start_install(model_id, session="mike")

    assert result["status"] == "failed"
    job = background_jobs.get(
        transcription_models.install_job_id(model_id), reconcile=False)
    assert job["status"] == "failed"
    assert job["terminal_reason"] == "systemd_launch_failed"


def test_computer_owner_upsert_race_clears_provisional_state(monkeypatch):
    model_id = "faster-whisper:medium"
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    monkeypatch.setattr(transcription_models, "_start_monitor", lambda *_args: None)
    monkeypatch.setattr(
        transcription_models.subprocess, "run",
        lambda *_args, **_kwargs: type("Result", (), {
            "returncode": 0, "stderr": "",
        })())
    real_upsert = background_jobs.upsert_computer
    calls = 0

    def fail_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("owner race")
        return real_upsert(**kwargs)

    monkeypatch.setattr(background_jobs, "upsert_computer", fail_once)
    with transcription_models._lock:
        transcription_models._tasks.pop(model_id, None)

    with pytest.raises(sqlite3.OperationalError, match="owner race"):
        transcription_models.start_install(model_id, computer_id="computer-a")
    with transcription_models._lock:
        assert model_id not in transcription_models._tasks

    result = transcription_models.start_install(
        model_id, computer_id="computer-a")
    assert result["status"] == "installing"


def test_terminal_owner_reassignment_race_does_not_stick_installing(monkeypatch):
    model_id = "faster-whisper:medium"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    old = background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(model_id),
        kind="transcription-model-install", title="Install", status="queued")
    background_jobs.finish(
        old["job_id"], generation=old["generation"], status="failed",
        reason="old failure")
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    real_reassign = background_jobs.reassign_terminal_computer_owner
    monkeypatch.setattr(
        background_jobs, "reassign_terminal_computer_owner",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("reassign race")))
    with transcription_models._lock:
        transcription_models._tasks.pop(model_id, None)

    with pytest.raises(sqlite3.OperationalError, match="reassign race"):
        transcription_models.start_install(model_id, computer_id="computer-a")
    with transcription_models._lock:
        assert model_id not in transcription_models._tasks

    monkeypatch.setattr(
        background_jobs, "reassign_terminal_computer_owner", real_reassign)
    monkeypatch.setattr(transcription_models, "_start_monitor", lambda *_args: None)
    monkeypatch.setattr(
        transcription_models.subprocess, "run",
        lambda *_args, **_kwargs: type("Result", (), {
            "returncode": 0, "stderr": "",
        })())
    result = transcription_models.start_install(
        model_id, computer_id="computer-a")
    assert result["status"] == "installing"


def test_launch_preparation_failure_terminalizes_generation_and_retry(monkeypatch):
    model_id = "faster-whisper:medium"
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    real_worker_script = transcription_models._worker_script
    monkeypatch.setattr(
        transcription_models, "_worker_script",
        lambda: (_ for _ in ()).throw(RuntimeError("worker path race")))
    with transcription_models._lock:
        transcription_models._tasks.pop(model_id, None)

    with pytest.raises(RuntimeError, match="worker path race"):
        transcription_models.start_install(model_id, computer_id="computer-a")

    job_id = transcription_models.install_job_id(model_id)
    failed = background_jobs.get(job_id, reconcile=False)
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "install_setup_failed:RuntimeError"
    with transcription_models._lock:
        assert model_id not in transcription_models._tasks

    monkeypatch.setattr(transcription_models, "_worker_script", real_worker_script)
    monkeypatch.setattr(transcription_models, "_start_monitor", lambda *_args: None)
    monkeypatch.setattr(
        transcription_models.subprocess, "run",
        lambda *_args, **_kwargs: type("Result", (), {
            "returncode": 0, "stderr": "",
        })())
    result = transcription_models.start_install(
        model_id, computer_id="computer-a")
    retried = background_jobs.get(job_id, reconcile=False)
    assert result["status"] == "installing"
    assert retried["generation"] == failed["generation"] + 1


def test_container_install_launches_detached_worker_without_systemd(monkeypatch):
    model_id = "faster-whisper:medium"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLARP_SHARE_DIR", "/opt/clarp")
    launches = []
    monkeypatch.setattr(
        transcription_models.subprocess, "Popen",
        lambda command, **kwargs: launches.append((command, kwargs)) or object())
    monkeypatch.setattr(
        transcription_models.subprocess, "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("container install must not use systemd-run")))
    monkeypatch.setattr(transcription_models, "_start_monitor", lambda *_args: None)
    with transcription_models._lock:
        transcription_models._tasks.pop(model_id, None)

    result = transcription_models.start_install(model_id, session="mike")

    assert result["status"] == "installing"
    command, kwargs = launches[0]
    assert command[1] == "/opt/clarp/scripts/transcription_model_job.py"
    assert command[-2:] == ["--model-id", model_id]
    assert kwargs == {"start_new_session": True, "close_fds": True}


def test_start_install_reuses_active_generation_after_server_restart(monkeypatch):
    model_id = "faster-whisper:medium"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    existing = background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(model_id),
        kind="transcription-model-install", title="Install", status="running",
        worker_pid=4242, worker_start_token="boot:worker")
    monkeypatch.setattr(
        transcription_models.subprocess, "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("active generation must not relaunch")))
    monitored = []
    monkeypatch.setattr(
        transcription_models, "_start_monitor",
        lambda *args: monitored.append(args))
    with transcription_models._lock:
        transcription_models._tasks.pop(model_id, None)

    result = transcription_models.start_install(model_id, session="mike")

    assert result["job_handle"] == background_jobs.job_handle(existing)
    assert monitored and monitored[0][:2] == (
        model_id, transcription_models.install_job_id(model_id))


def test_repeated_active_install_requests_share_one_monitor_thread(monkeypatch):
    """A polling/retrying client must not create one monitor per request."""
    model_id = "faster-whisper:medium"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(model_id),
        kind="transcription-model-install", title="Install", status="running",
        worker_pid=4242, worker_start_token="boot:worker")
    monitored = []
    monkeypatch.setattr(
        transcription_models, "_start_monitor",
        lambda *args: monitored.append(args))

    for _ in range(20):
        transcription_models.start_install(model_id, session="mike")

    assert len(monitored) == 1


def test_registry_publication_does_not_outrun_active_job_truth(monkeypatch):
    model_id = "faster-whisper:medium"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    existing = background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(model_id),
        kind="transcription-model-install", title="Install", status="running",
        worker_pid=4242, worker_start_token="boot:worker")
    monkeypatch.setattr(
        transcription_models, "installed_records", lambda: [{"id": model_id}])
    activated = []
    monkeypatch.setattr(
        transcription_models, "_schedule_activation",
        lambda *_args: activated.append(True))
    monkeypatch.setattr(transcription_models, "_start_monitor", lambda *_args: None)
    with transcription_models._lock:
        transcription_models._tasks.pop(model_id, None)

    result = transcription_models.start_install(
        model_id, session="mike", on_complete=lambda _model_id: None)

    assert result["status"] == "installing"
    assert result["job_handle"] == background_jobs.job_handle(existing)
    assert activated == []


def test_cancelled_generation_cleans_partial_root_before_registry_write(
    tmp_path, monkeypatch,
):
    model_id = "faster-whisper:medium"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    job = background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(model_id),
        kind="transcription-model-install", title="Install", status="queued")
    background_jobs.cancel(job["job_id"])
    monkeypatch.setattr(transcription_models, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(transcription_models, "MANAGED_MODELS", tmp_path / "models")
    partial = transcription_models._managed_root(model_id)
    partial.mkdir(parents=True)
    (partial / "partial.bin").write_text("partial")

    claimed = transcription_models.rollback_cancelled_install(
        model_id, job_id=job["job_id"], generation=job["generation"])

    assert claimed is True
    assert not partial.exists()
    assert not transcription_models.REGISTRY.exists()


def test_retry_cleans_cancelled_partial_root_before_advancing_generation(
    tmp_path, monkeypatch,
):
    model_id = "faster-whisper:medium"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    old = background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(model_id),
        kind="transcription-model-install", title="Install", status="queued")
    background_jobs.cancel(old["job_id"])
    monkeypatch.setattr(transcription_models, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(transcription_models, "MANAGED_MODELS", tmp_path / "models")
    partial = transcription_models._managed_root(model_id)
    partial.mkdir(parents=True)
    (partial / "partial.bin").write_text("partial")

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        transcription_models.subprocess, "run", lambda *_args, **_kwargs: Result())
    monkeypatch.setattr(transcription_models, "_start_monitor", lambda *_args: None)
    with transcription_models._lock:
        transcription_models._tasks.pop(model_id, None)

    result = transcription_models.start_install(model_id, session="mike")

    current = background_jobs.get(old["job_id"], reconcile=False)
    assert current["generation"] == old["generation"] + 1
    assert result["job_handle"] == background_jobs.job_handle(current)
    assert not partial.exists()


def test_retry_terminates_exact_terminal_worker_before_new_generation(monkeypatch):
    model_id = "faster-whisper:medium"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    old = background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(model_id),
        kind="transcription-model-install", title="Install", status="running",
        worker_pid=4242, worker_start_token="boot:worker")
    background_jobs.cancel(old["job_id"])
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    alive = True

    def worker_alive(_job):
        return alive

    def terminate(pid, token):
        nonlocal alive
        assert (pid, token) == (4242, "boot:worker")
        alive = False
        return True

    monkeypatch.setattr(transcription_models, "_job_worker_alive", worker_alive)
    monkeypatch.setattr(background_jobs, "terminate_worker", terminate)
    monkeypatch.setattr(transcription_models, "_start_monitor", lambda *_args: None)
    monkeypatch.setattr(
        transcription_models.subprocess, "run",
        lambda *_args, **_kwargs: type("Result", (), {
            "returncode": 0, "stderr": "",
        })())
    with transcription_models._lock:
        transcription_models._tasks.pop(model_id, None)

    result = transcription_models.start_install(model_id, session="mike")

    current = background_jobs.get(old["job_id"], reconcile=False)
    assert current["generation"] == old["generation"] + 1
    assert result["status"] == "installing"


def test_remove_honors_hf_home(tmp_path, monkeypatch):
    monkeypatch.setattr(transcription_models, "REGISTRY", tmp_path / "registry.json")
    hf_home = tmp_path / "hf"
    model_root = hf_home / "hub/models--Systran--faster-whisper-base.en"
    snapshot = model_root / "snapshots/complete"
    snapshot.mkdir(parents=True)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (snapshot / name).write_text("ok")
    monkeypatch.setenv("HF_HOME", str(hf_home))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setattr(transcription_models, "MANAGED_MODELS", hf_home / "hub")
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [{
        "id": "faster-whisper:base.en", "provider": "faster-whisper",
        "_local_path": str(snapshot), "_managed_root": str(model_root),
    }])
    transcription_models.REGISTRY.write_text(json.dumps([{
        "id": "faster-whisper:base.en", "provider": "faster-whisper",
        "_local_path": str(snapshot), "_managed_root": str(model_root),
    }]))
    transcription_models.remove("faster-whisper:base.en")
    assert not model_root.exists()


def test_remove_import_unregisters_without_deleting_user_files(tmp_path, monkeypatch):
    hf_root = tmp_path / "hub"
    imported = hf_root / "custom-model"
    neighbor = hf_root / "keep-me"
    imported.mkdir(parents=True); neighbor.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (imported / name).write_text("ok")
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(transcription_models, "REGISTRY", registry)
    monkeypatch.setenv("HF_HUB_CACHE", str(hf_root))
    transcription_models.register("faster-whisper:base.en", str(imported))
    transcription_models.remove("faster-whisper:base.en")
    assert imported.exists()
    assert neighbor.exists()
    assert transcription_models.installed_records() == []


def test_remove_rejects_in_progress_install(monkeypatch):
    model_id = "faster-whisper:medium"
    with transcription_models._lock:
        transcription_models._tasks[model_id] = {
            "model_id": model_id, "status": "installing", "error": "",
        }
    try:
        import pytest
        with pytest.raises(ValueError, match="in progress"):
            transcription_models.remove(model_id)
    finally:
        with transcription_models._lock:
            transcription_models._tasks.pop(model_id, None)


def test_managed_catalog_includes_supported_platform_providers():
    assert {item["provider"] for item in CATALOG} == {
        "faster-whisper", "whisper.cpp",
    }


def test_async_install_rejects_unsupported_host_before_creating_job(monkeypatch):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "macos")

    with pytest.raises(ValueError, match="not supported on macos"):
        transcription_models.start_install(
            "faster-whisper:large-v3-turbo", computer_id="mac")

    assert background_jobs.get(
        transcription_models.install_job_id("faster-whisper:large-v3-turbo"),
        reconcile=False,
    ) is None


def test_whisper_cpp_registry_requires_model_and_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(transcription_models, "REGISTRY", tmp_path / "models.json")
    model = tmp_path / "ggml-small.en.bin"
    runtime = tmp_path / "whisper-cli"
    model.write_bytes(b"x" * 1_000_001)
    runtime.write_bytes(b"runtime")
    runtime.chmod(0o700)
    record = transcription_models.register(
        "whisper.cpp:small.en", str(model), runtime_path=str(runtime))
    assert record["_runtime_path"] == str(runtime.resolve())
    assert transcription_models.installed_records()[0]["id"] == "whisper.cpp:small.en"


def test_managed_whisper_cpp_install_registers_pinned_runtime(tmp_path, monkeypatch):
    from lib import whispercpp
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "macos")
    monkeypatch.setattr(transcription_models, "REGISTRY", tmp_path / "models.json")
    monkeypatch.setattr(transcription_models, "MANAGED_MODELS", tmp_path / "models")

    def install(_name, root):
        root.mkdir(parents=True)
        model = root / "ggml-small.en.bin"
        runtime = root / "whisper-cli"
        model.write_bytes(b"x" * 1_000_001)
        runtime.write_bytes(b"runtime")
        runtime.chmod(0o700)
        return model, runtime

    monkeypatch.setattr(whispercpp, "install", install)
    transcription_models.install("whisper.cpp:small.en")
    record = transcription_models.installed_records()[0]
    assert record["id"] == "whisper.cpp:small.en"
    assert pathlib.Path(record["_runtime_path"]).name == "whisper-cli"


def test_whisper_cpp_registry_rejects_non_executable_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(transcription_models, "REGISTRY", tmp_path / "models.json")
    model = tmp_path / "ggml-small.en.bin"
    runtime = tmp_path / "whisper-cli"
    model.write_bytes(b"x" * 1_000_001)
    runtime.write_bytes(b"not executable")
    with pytest.raises(ValueError, match="incomplete or incompatible"):
        transcription_models.register(
            "whisper.cpp:small.en", str(model), runtime_path=str(runtime))


def test_install_completion_callback_runs_before_installed_status(monkeypatch):
    import time
    model_id = "faster-whisper:base.en"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [])
    monkeypatch.setattr(transcription_models, "MONITOR_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(
        transcription_models.subprocess, "run",
        lambda *_args, **_kwargs: type("Result", (), {
            "returncode": 0, "stderr": "",
        })())
    seen = []
    with transcription_models._lock:
        transcription_models._tasks.pop(model_id, None)
    result = transcription_models.start_install(
        model_id, session="mike", on_complete=lambda value: seen.append(value))
    job_id, generation = background_jobs.parse_job_handle(result["job_handle"])
    background_jobs.start(job_id, generation=generation)
    background_jobs.finish(job_id, generation=generation)
    for _ in range(50):
        with transcription_models._lock:
            status = (transcription_models._tasks.get(model_id) or {}).get("status")
        if status == "installed": break
        time.sleep(0.01)
    assert seen == [model_id]
    assert status == "installed"


def test_completed_install_recovers_activation_after_server_restart(monkeypatch):
    import time
    model_id = "faster-whisper:base.en"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    job = background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(model_id),
        kind="transcription-model-install", title="Install", status="running")
    background_jobs.finish(job["job_id"], generation=job["generation"])
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [{
        "id": model_id, "provider": "faster-whisper", "model": "base.en",
    }])
    with transcription_models._activation_lock:
        transcription_models._activation_inflight.clear()
        transcription_models._activation_completed.clear()
        transcription_models._activation_exhausted.clear()
        transcription_models._activation_attempts.clear()
    seen = []

    transcription_models.recover_completed_installs(
        lambda value: seen.append(value))
    for _ in range(100):
        if seen:
            break
        time.sleep(0.01)

    assert seen == [model_id]


def test_failed_activation_remains_retryable(monkeypatch):
    import time
    model_id = "faster-whisper:base.en"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    job = background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(model_id),
        kind="transcription-model-install", title="Install", status="running")
    background_jobs.finish(job["job_id"], generation=job["generation"])
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [{
        "id": model_id, "provider": "faster-whisper", "model": "base.en",
    }])
    monkeypatch.setattr(transcription_models, "ACTIVATION_RETRY_DELAY_SEC", 0.01)
    monkeypatch.setattr(transcription_models, "ACTIVATION_MAX_ATTEMPTS", 2)
    with transcription_models._activation_lock:
        transcription_models._activation_inflight.clear()
        transcription_models._activation_completed.clear()
        transcription_models._activation_exhausted.clear()
        transcription_models._activation_retry_after.clear()
        transcription_models._activation_attempts.clear()
    calls = 0

    def activate(_model_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary load failure")

    transcription_models.recover_completed_installs(activate)
    for _ in range(200):
        with transcription_models._lock:
            status = (transcription_models._tasks.get(model_id) or {}).get("status")
        if status == "installed":
            break
        time.sleep(0.01)
    assert calls == 2
    assert status == "installed"


def test_explicit_install_retry_resets_exhausted_activation_budget(monkeypatch):
    import time
    model_id = "faster-whisper:base.en"
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    job = background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(model_id),
        kind="transcription-model-install", title="Install", status="running")
    job = background_jobs.finish(job["job_id"], generation=job["generation"])
    monkeypatch.setattr(transcription_models, "installed_records", lambda: [{
        "id": model_id, "provider": "faster-whisper", "model": "base.en",
    }])
    key = transcription_models._activation_key(model_id, job)
    with transcription_models._activation_lock:
        transcription_models._activation_inflight.clear()
        transcription_models._activation_completed.clear()
        transcription_models._activation_exhausted.add(key)
        transcription_models._activation_attempts[key] = 3
    seen = []

    transcription_models.start_install(
        model_id, session="mike", on_complete=lambda value: seen.append(value))
    for _ in range(100):
        if seen:
            break
        time.sleep(0.01)

    assert seen == [model_id]
    with transcription_models._activation_lock:
        assert key not in transcription_models._activation_exhausted
        assert key in transcription_models._activation_completed


def test_remove_rejects_configured_default(monkeypatch):
    from types import SimpleNamespace
    from lib import config
    monkeypatch.setattr(config, "load", lambda: SimpleNamespace(
        whisper_enabled=True, whisper_model="small.en"))
    with __import__("pytest").raises(ValueError, match="configured server default"):
        transcription_models.remove("faster-whisper:small.en")
