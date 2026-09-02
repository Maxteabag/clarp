import importlib.util
import pathlib
import subprocess
import sys

from lib import agents, background_jobs, transcription_models
from lib.background_job_watcher import BackgroundJobWatcher


_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts/transcription_model_job.py"
_SPEC = importlib.util.spec_from_file_location("transcription_model_job", _SCRIPT)
assert _SPEC and _SPEC.loader
worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)


MODEL_ID = "faster-whisper:medium"


def _queued_job() -> dict:
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    return background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(MODEL_ID),
        kind="transcription-model-install", title="Install model",
        status="queued", metadata={"model_id": MODEL_ID, "expire_queued": True})


class CompletingProcess:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.polls = 0
        self.terminated = False

    def poll(self):
        self.polls += 1
        return None if self.polls == 1 else self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        del timeout
        return self.returncode


class RunningProcess(CompletingProcess):
    def __init__(self):
        super().__init__(0)

    def poll(self):
        return self.returncode if self.terminated else None


def _identity(monkeypatch):
    monkeypatch.setattr(worker.os, "getpid", lambda: 5252)
    monkeypatch.setattr(
        background_jobs, "process_start_token",
        lambda pid: "boot:model-worker" if pid == 5252 else "")


def test_worker_resolves_packaged_and_source_library_roots(tmp_path):
    packaged = tmp_path / "packaged"
    (packaged / "lib").mkdir(parents=True)
    source = tmp_path / "source"
    (source / "server/lib").mkdir(parents=True)

    assert worker._library_root(packaged) == packaged
    assert worker._library_root(source) == source / "server"


def test_perform_mode_runs_real_install_function(monkeypatch):
    installed = []
    monkeypatch.setattr(
        transcription_models, "install", lambda model_id: installed.append(model_id))

    assert worker.main(["--perform", MODEL_ID]) == 0
    assert installed == [MODEL_ID]


def test_model_worker_heartbeats_and_succeeds(monkeypatch):
    job = _queued_job()
    _identity(monkeypatch)
    commands = []
    launch_kwargs = []

    result = worker.supervise_install(
        handle=background_jobs.job_handle(job), model_id=MODEL_ID,
        popen=lambda command, **kwargs: commands.append(command)
        or launch_kwargs.append(kwargs)
        or CompletingProcess(0),
        monotonic=lambda: 0, sleep=lambda _seconds: None)

    assert result == 0
    assert commands[0][-2:] == ["--perform", MODEL_ID]
    assert launch_kwargs[0]["start_new_session"] is True
    assert callable(launch_kwargs[0]["preexec_fn"])
    finished = background_jobs.get(job["job_id"], reconcile=False)
    assert finished["status"] == "succeeded"
    assert finished["worker_pid"] == 5252


def test_model_worker_failure_is_terminal(monkeypatch):
    job = _queued_job()
    _identity(monkeypatch)

    result = worker.supervise_install(
        handle=background_jobs.job_handle(job), model_id=MODEL_ID,
        popen=lambda *_args, **_kwargs: CompletingProcess(8),
        monotonic=lambda: 0, sleep=lambda _seconds: None)

    assert result == 8
    failed = background_jobs.get(job["job_id"], reconcile=False)
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "install_exit_8"


def test_missing_model_worker_identity_fails_queued_generation(monkeypatch):
    job = _queued_job()
    monkeypatch.setattr(worker.os, "getpid", lambda: 5252)
    monkeypatch.setattr(background_jobs, "process_start_token", lambda _pid: "")

    result = worker.supervise_install(
        handle=background_jobs.job_handle(job), model_id=MODEL_ID)

    assert result == 2
    failed = background_jobs.get(job["job_id"], reconcile=False)
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "worker_identity_unavailable"


def test_perform_child_is_tied_to_exact_supervisor_parent(monkeypatch):
    calls = []

    class LibC:
        @staticmethod
        def prctl(option, signal_number):
            calls.append((option, signal_number))
            return 0

    monkeypatch.setattr(worker.ctypes, "CDLL", lambda *_args, **_kwargs: LibC())
    monkeypatch.setattr(worker.os, "getppid", lambda: 5252)
    monkeypatch.setattr(
        worker.os, "kill",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("matching parent must not self-terminate")))

    worker._terminate_with_parent(5252)

    assert calls == [(1, worker.signal.SIGTERM)]


def test_parent_death_mismatch_terminates_real_subprocess():
    code = (
        "import importlib.util,os,pathlib;"
        f"p=pathlib.Path({str(_SCRIPT)!r});"
        "s=importlib.util.spec_from_file_location('tm_worker_child',p);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "m._terminate_with_parent(os.getppid()+100000)"
    )

    result = subprocess.run([sys.executable, "-c", code], timeout=5)

    assert result.returncode == -worker.signal.SIGTERM


def test_cancelled_model_install_stops_process_tree(monkeypatch):
    job = _queued_job()
    _identity(monkeypatch)
    process = RunningProcess()
    stopped = []
    monkeypatch.setattr(
        worker, "_stop_process",
        lambda target: stopped.append(target) or target.terminate())
    rollbacks = []
    monkeypatch.setattr(
        transcription_models, "rollback_cancelled_install",
        lambda model_id, *, job_id, generation: rollbacks.append(
            (model_id, job_id, generation)) or True)
    sleeps = 0

    def cancel_after_start(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            background_jobs.cancel(job["job_id"])

    result = worker.supervise_install(
        handle=background_jobs.job_handle(job), model_id=MODEL_ID,
        popen=lambda *_args, **_kwargs: process,
        monotonic=lambda: 0, sleep=cancel_after_start)

    assert result == 130
    assert stopped == [process]
    assert rollbacks == [(MODEL_ID, job["job_id"], job["generation"])]
    assert background_jobs.get(
        job["job_id"], reconcile=False)["status"] == "cancelled"


def test_heartbeat_cancellation_stops_and_rolls_back_exact_generation(
    monkeypatch,
):
    job = _queued_job()
    _identity(monkeypatch)
    process = RunningProcess()
    stopped = []
    rollbacks = []
    monkeypatch.setattr(
        worker, "_stop_process",
        lambda target: stopped.append(target) or target.terminate())
    monkeypatch.setattr(
        transcription_models, "rollback_cancelled_install",
        lambda model_id, *, job_id, generation: rollbacks.append(
            (model_id, job_id, generation)) or True)
    real_heartbeat = background_jobs.heartbeat

    def cancel_at_heartbeat(job_id, **kwargs):
        background_jobs.cancel(job_id)
        return real_heartbeat(job_id, **kwargs)

    monkeypatch.setattr(background_jobs, "heartbeat", cancel_at_heartbeat)

    result = worker.supervise_install(
        handle=background_jobs.job_handle(job), model_id=MODEL_ID,
        popen=lambda *_args, **_kwargs: process,
        monotonic=lambda: 0, sleep=lambda _seconds: None)

    assert result == 130
    assert stopped == [process]
    assert rollbacks == [(MODEL_ID, job["job_id"], job["generation"])]


def test_cancellation_winning_at_process_exit_rolls_back_exact_generation(
    monkeypatch,
):
    job = _queued_job()
    _identity(monkeypatch)
    rollbacks = []
    monkeypatch.setattr(
        transcription_models, "rollback_cancelled_install",
        lambda model_id, *, job_id, generation: rollbacks.append(
            (model_id, job_id, generation)) or True)

    class CancelOnExit(CompletingProcess):
        def poll(self):
            self.polls += 1
            if self.polls == 1:
                return None
            background_jobs.cancel(job["job_id"])
            return 0

    result = worker.supervise_install(
        handle=background_jobs.job_handle(job), model_id=MODEL_ID,
        popen=lambda *_args, **_kwargs: CancelOnExit(0),
        monotonic=lambda: 0, sleep=lambda _seconds: None)

    assert result == 130
    assert rollbacks == [(MODEL_ID, job["job_id"], job["generation"])]
    assert background_jobs.get(
        job["job_id"], reconcile=False)["status"] == "cancelled"


def test_old_cancelled_generation_cannot_remove_restarted_install(
    tmp_path, monkeypatch,
):
    old = _queued_job()
    background_jobs.cancel(old["job_id"])
    current = background_jobs.upsert(
        session="mike", job_id=old["job_id"],
        kind="transcription-model-install", title="Install model",
        status="queued", restart_cancelled=True,
        metadata={"model_id": MODEL_ID, "expire_queued": True})
    managed_root = tmp_path / "models" / "faster-whisper--medium"
    model_path = managed_root / "snapshot"
    model_path.mkdir(parents=True)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (model_path / name).write_text("ok")
    monkeypatch.setattr(
        transcription_models, "MANAGED_MODELS", tmp_path / "models")
    monkeypatch.setattr(
        transcription_models, "REGISTRY", tmp_path / "registry.json")
    transcription_models.register(
        MODEL_ID, str(model_path), managed_root=str(managed_root))

    claimed = transcription_models.rollback_cancelled_install(
        MODEL_ID, job_id=old["job_id"], generation=old["generation"])

    assert claimed is False
    assert current["generation"] == old["generation"] + 1
    assert managed_root.exists()
    assert [record["id"] for record in transcription_models.installed_records()] == [
        MODEL_ID,
    ]


def test_model_worker_transitions_are_visible_on_sse(monkeypatch):
    job = _queued_job()
    _identity(monkeypatch)

    class Stream:
        def __init__(self):
            self.events = []

        def broadcast(self, event):
            self.events.append(event)

    stream = Stream()
    watcher = BackgroundJobWatcher(stream)
    watcher._last_id = background_jobs.latest_event_id()
    worker.supervise_install(
        handle=background_jobs.job_handle(job), model_id=MODEL_ID,
        popen=lambda *_args, **_kwargs: CompletingProcess(0),
        monotonic=lambda: 0, sleep=lambda _seconds: None)
    watcher._poll_once()

    assert len(stream.events) == 2
    assert stream.events[-1]["status"] == "succeeded"
    assert stream.events[-1]["job"]["kind"] == "transcription-model-install"


def test_vanished_model_worker_reconciles_failed(monkeypatch):
    job = _queued_job()
    _identity(monkeypatch)
    running = background_jobs.start(
        job["job_id"], generation=job["generation"], worker_pid=5252,
        worker_start_token="boot:model-worker")

    changed = background_jobs.reconcile_stale(
        job_id=job["job_id"],
        now_ms=running["heartbeat_at"] + running["heartbeat_timeout_ms"] + 1,
        process_probe=lambda _pid, _token: False)

    assert changed == [job["job_id"]]
    failed = background_jobs.get(job["job_id"], reconcile=False)
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "worker_vanished"


def test_model_worker_that_never_attaches_expires_from_queue():
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    queued = background_jobs.upsert(
        session="mike", job_id=transcription_models.install_job_id(MODEL_ID),
        kind="transcription-model-install", title="Install", status="queued",
        heartbeat_timeout_ms=30_000, metadata={"expire_queued": True})

    changed = background_jobs.reconcile_stale(
        job_id=queued["job_id"],
        now_ms=queued["heartbeat_at"] + queued["heartbeat_timeout_ms"] + 1)

    assert changed == [queued["job_id"]]
    assert background_jobs.get(
        queued["job_id"], reconcile=False)["terminal_reason"] == "worker_never_started"


def test_stale_model_generation_never_launches_download(monkeypatch):
    old = _queued_job()
    background_jobs.cancel(old["job_id"])
    background_jobs.upsert(
        session="mike", job_id=old["job_id"],
        kind="transcription-model-install", title="Install", status="queued",
        restart_cancelled=True)
    _identity(monkeypatch)
    launched = False

    def forbidden(*_args, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("stale generation must not launch")

    result = worker.supervise_install(
        handle=background_jobs.job_handle(old), model_id=MODEL_ID,
        popen=forbidden)

    assert result == 2
    assert launched is False
