import importlib.util
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time

from lib import agents, background_jobs
from lib.background_job_watcher import BackgroundJobWatcher


_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts/server_update_job.py"
_SPEC = importlib.util.spec_from_file_location("server_update_job", _SCRIPT)
assert _SPEC and _SPEC.loader
worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)


def _queued_job() -> dict:
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    return background_jobs.upsert(
        session="mike", job_id="managed-server-update", kind="server-update",
        title="Update Clarp", status="queued")


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
    monkeypatch.setattr(worker.os, "getpid", lambda: 4242)
    monkeypatch.setattr(
        background_jobs, "process_start_token",
        lambda pid: "boot:worker" if pid == 4242 else "")


def test_worker_uses_absolute_installed_clarp_admin_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLARP_ADMIN_BIN", raising=False)

    assert worker.update_command() == [
        str(tmp_path / ".local/bin/clarp-admin"), "update"]


def test_stop_process_signals_entire_group_then_escalates(monkeypatch):
    signals = []

    class GroupProcess:
        pid = 4242
        waits = 0

        def poll(self):
            return None

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("update", timeout)
            return -9

        def terminate(self):
            raise AssertionError("real process must use group SIGTERM")

        def kill(self):
            raise AssertionError("real process must use group SIGKILL")

    monkeypatch.setattr(worker.os, "getpgid", lambda _pid: 9001)
    monkeypatch.setattr(
        worker.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    waits = iter([False, True])
    monkeypatch.setattr(
        worker, "_wait_for_process_group_exit",
        lambda *_args, **_kwargs: next(waits))

    worker._stop_process(GroupProcess())

    assert signals == [(9001, signal.SIGTERM), (9001, signal.SIGKILL)]


def test_stop_process_ignores_already_exited_group(monkeypatch):
    class GoneProcess:
        pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(
        worker.os, "getpgid",
        lambda _pid: (_ for _ in ()).throw(ProcessLookupError()))

    worker._stop_process(GoneProcess())


def test_stop_process_group_does_not_leave_spawned_child_running():
    parent = subprocess.Popen(
        [
            sys.executable, "-c",
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            "print(child.pid,flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline().strip())

        worker._stop_process(parent)

        def still_running(pid: int) -> bool:
            try:
                status = pathlib.Path(f"/proc/{pid}/status").read_text()
            except FileNotFoundError:
                return False
            return "\nState:\tZ" not in status

        for _ in range(50):
            if not still_running(child_pid):
                break
            time.sleep(0.01)
        assert parent.poll() is not None
        assert not still_running(child_pid)
    finally:
        if parent.poll() is None:
            os.killpg(os.getpgid(parent.pid), signal.SIGKILL)


def test_stop_process_kills_child_that_ignores_sigterm_after_parent_exits(
    monkeypatch,
):
    monkeypatch.setattr(worker, "PROCESS_GROUP_TERM_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(worker, "PROCESS_GROUP_KILL_TIMEOUT_SEC", 0.5)
    parent = subprocess.Popen(
        [
            sys.executable, "-c",
            "import subprocess,sys,time; "
            "code='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "print(\\\"ready\\\",flush=True);time.sleep(60)'; "
            "child=subprocess.Popen([sys.executable,'-c',code],"
            "stdout=subprocess.PIPE,text=True); child.stdout.readline(); "
            "print(child.pid,flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline().strip())

        worker._stop_process(parent)

        for _ in range(50):
            try:
                status = pathlib.Path(f"/proc/{child_pid}/status").read_text()
                child_running = "\nState:\tZ" not in status
            except FileNotFoundError:
                child_running = False
            if not child_running:
                break
            time.sleep(0.01)
        assert parent.poll() is not None
        assert not child_running
    finally:
        if parent.poll() is None:
            os.killpg(os.getpgid(parent.pid), signal.SIGKILL)


def test_update_worker_heartbeats_and_succeeds(monkeypatch):
    job = _queued_job()
    _identity(monkeypatch)
    process = CompletingProcess(0)

    result = worker.supervise_update(
        handle=background_jobs.job_handle(job), command=["clarp-admin", "update"],
        popen=lambda *_args, **_kwargs: process,
        monotonic=lambda: 0, sleep=lambda _seconds: None)

    assert result == 0
    finished = background_jobs.get(job["job_id"], reconcile=False)
    assert finished["status"] == "succeeded"
    assert finished["worker_pid"] == 4242
    assert finished["worker_start_token"] == "boot:worker"


def test_update_worker_transitions_are_visible_on_typed_sse(monkeypatch):
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
    worker.supervise_update(
        handle=background_jobs.job_handle(job), command=["clarp-admin", "update"],
        popen=lambda *_args, **_kwargs: CompletingProcess(0),
        monotonic=lambda: 0, sleep=lambda _seconds: None)

    watcher._poll_once()

    assert len(stream.events) == 2
    assert all(event["type"] == "background-job-updated" for event in stream.events)
    assert stream.events[-1]["status"] == "succeeded"
    assert stream.events[-1]["job"]["kind"] == "server-update"


def test_update_worker_failure_is_terminal(monkeypatch):
    job = _queued_job()
    _identity(monkeypatch)

    result = worker.supervise_update(
        handle=background_jobs.job_handle(job), command=["clarp-admin", "update"],
        popen=lambda *_args, **_kwargs: CompletingProcess(7),
        monotonic=lambda: 0, sleep=lambda _seconds: None)

    assert result == 7
    failed = background_jobs.get(job["job_id"], reconcile=False)
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "update_exit_7"


def test_missing_worker_identity_fails_queued_generation(monkeypatch):
    job = _queued_job()
    monkeypatch.setattr(worker.os, "getpid", lambda: 4242)
    monkeypatch.setattr(background_jobs, "process_start_token", lambda _pid: "")

    result = worker.supervise_update(
        handle=background_jobs.job_handle(job), command=["clarp-admin", "update"])

    assert result == 2
    failed = background_jobs.get(job["job_id"], reconcile=False)
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "worker_identity_unavailable"


def test_vanished_update_worker_reconciles_to_failed(monkeypatch):
    job = _queued_job()
    _identity(monkeypatch)
    running = background_jobs.start(
        job["job_id"], generation=job["generation"], worker_pid=4242,
        worker_start_token="boot:worker")

    changed = background_jobs.reconcile_stale(
        job_id=job["job_id"],
        now_ms=running["heartbeat_at"] + running["heartbeat_timeout_ms"] + 1,
        process_probe=lambda _pid, _token: False)

    assert changed == [job["job_id"]]
    failed = background_jobs.get(job["job_id"], reconcile=False)
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "worker_vanished"


def test_update_worker_that_never_attaches_expires_from_queue():
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    queued = background_jobs.upsert(
        session="mike", job_id="managed-server-update", kind="server-update",
        title="Update Clarp", status="queued", heartbeat_timeout_ms=30_000,
        metadata={"expire_queued": True})

    changed = background_jobs.reconcile_stale(
        job_id=queued["job_id"],
        now_ms=queued["heartbeat_at"] + queued["heartbeat_timeout_ms"] + 1)

    assert changed == [queued["job_id"]]
    failed = background_jobs.get(queued["job_id"], reconcile=False)
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "worker_never_started"


def test_cancelled_update_stops_child_without_overwriting_cancel(monkeypatch):
    job = _queued_job()
    _identity(monkeypatch)
    process = RunningProcess()
    sleeps = 0

    def cancel_after_start(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            background_jobs.cancel(job["job_id"])

    result = worker.supervise_update(
        handle=background_jobs.job_handle(job), command=["clarp-admin", "update"],
        popen=lambda *_args, **_kwargs: process,
        monotonic=lambda: 0, sleep=cancel_after_start)

    assert result == 130
    assert process.terminated is True
    cancelled = background_jobs.get(job["job_id"], reconcile=False)
    assert cancelled["status"] == "cancelled"


def test_stale_generation_never_launches_updater(monkeypatch):
    old = _queued_job()
    background_jobs.cancel(old["job_id"])
    background_jobs.upsert(
        session="mike", job_id=old["job_id"], kind="server-update",
        title="Update Clarp", status="queued", restart_cancelled=True)
    _identity(monkeypatch)
    launched = False

    def forbidden_launch(*_args, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("stale generation must not launch")

    result = worker.supervise_update(
        handle=background_jobs.job_handle(old), command=["clarp-admin", "update"],
        popen=forbidden_launch, stop_event=threading.Event())

    assert result == 2
    assert launched is False


def test_restarted_update_generation_does_not_inherit_old_worker_identity(
    monkeypatch,
):
    old = _queued_job()
    background_jobs.start(
        old["job_id"], generation=old["generation"], worker_pid=111,
        worker_start_token="boot:old")
    background_jobs.finish(
        old["job_id"], generation=old["generation"], status="failed",
        reason="old_failed", worker_pid=111, worker_start_token="boot:old")
    restarted = background_jobs.upsert(
        session="mike", job_id=old["job_id"], kind="server-update",
        title="Update Clarp", status="queued")
    assert restarted["generation"] == old["generation"] + 1
    assert restarted["worker_pid"] is None
    assert restarted["worker_start_token"] == ""
    _identity(monkeypatch)

    result = worker.supervise_update(
        handle=background_jobs.job_handle(restarted),
        command=["clarp-admin", "update"],
        popen=lambda *_args, **_kwargs: CompletingProcess(0),
        monotonic=lambda: 0, sleep=lambda _seconds: None)

    assert result == 0
    current = background_jobs.get(old["job_id"], reconcile=False)
    assert current["status"] == "succeeded"
    assert current["worker_pid"] == 4242
