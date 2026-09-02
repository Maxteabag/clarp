#!/usr/bin/env python3
"""Generation-fenced worker for the detached managed server update."""
from __future__ import annotations

import argparse
import errno
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from typing import Callable

sys.path.insert(0, os.environ.get(
    "CLARP_SHARE_DIR", str(pathlib.Path.home() / ".local/share/clarp")))

from lib import background_jobs  # noqa: E402
from lib.server_update import UPDATE_JOB_ID  # noqa: E402


HEARTBEAT_INTERVAL_SEC = 30.0
GATE_INTERVAL_SEC = 0.5
PROCESS_GROUP_TERM_TIMEOUT_SEC = 10.0
PROCESS_GROUP_KILL_TIMEOUT_SEC = 5.0


def _fail_before_adoption(job_id: str, generation: int, reason: str) -> None:
    try:
        current = background_jobs.get(job_id, reconcile=False)
        if (
            current and current["status"] == "queued"
            and int(current.get("generation") or 1) == generation
        ):
            background_jobs.finish(
                job_id, generation=generation, status="failed", reason=reason)
    except Exception:
        # The server-side queued lease remains the fallback reconciler when
        # this worker cannot access SQLite during startup.
        pass


def update_command() -> list[str]:
    executable = os.environ.get("CLARP_ADMIN_BIN", "").strip() or str(
        pathlib.Path.home() / ".local/bin/clarp-admin")
    return [executable, "update"]


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH


def _wait_for_process_group_exit(
    process, process_group: int, timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        process.poll()  # Reap an exited parent while checking surviving children.
        if not _process_group_exists(process_group):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _stop_process(process) -> None:
    if process.poll() is not None:
        return
    pid = getattr(process, "pid", None)
    process_group = None
    if isinstance(pid, int) and pid > 0:
        try:
            process_group = os.getpgid(pid)
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        # Lightweight injected fakes do not model OS process groups.
        process.terminate()
    if process_group is not None:
        if not _wait_for_process_group_exit(
            process, process_group, PROCESS_GROUP_TERM_TIMEOUT_SEC
        ):
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                return
            _wait_for_process_group_exit(
                process, process_group, PROCESS_GROUP_KILL_TIMEOUT_SEC)
        try:
            process.wait(timeout=PROCESS_GROUP_KILL_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def supervise_update(
    *, handle: str, command: list[str],
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    stop_event: threading.Event | None = None,
) -> int:
    job_id, generation = background_jobs.parse_job_handle(handle)
    worker_pid = os.getpid()
    worker_token = background_jobs.process_start_token(worker_pid)
    if not worker_token:
        _fail_before_adoption(
            job_id, generation, "worker_identity_unavailable")
        return 2
    try:
        job = background_jobs.start(
            job_id, generation=generation, worker_pid=worker_pid,
            worker_start_token=worker_token)
    except BaseException as exc:
        _fail_before_adoption(
            job_id, generation,
            f"worker_start_failed:{type(exc).__name__}")
        return 2
    if not job or job["status"] != "running":
        return 2
    try:
        process = popen(command, start_new_session=True)
    except BaseException as exc:
        background_jobs.finish(
            job_id, generation=generation, status="failed",
            reason=f"update_launch_failed:{type(exc).__name__}",
            worker_pid=worker_pid, worker_start_token=worker_token)
        return 1

    stop_event = stop_event or threading.Event()
    next_heartbeat = monotonic()
    while process.poll() is None:
        if stop_event.is_set() or not background_jobs.is_active_for_worker(
            job_id, generation=generation, worker_pid=worker_pid,
            worker_start_token=worker_token,
        ):
            _stop_process(process)
            current = background_jobs.get(job_id, reconcile=False)
            if current and current["status"] in background_jobs.ACTIVE_STATUSES:
                background_jobs.finish(
                    job_id, generation=generation, status="failed",
                    reason="update_worker_stopped", worker_pid=worker_pid,
                    worker_start_token=worker_token)
            return 130
        now = monotonic()
        if now >= next_heartbeat:
            heartbeat = background_jobs.heartbeat(
                job_id, generation=generation, worker_pid=worker_pid,
                worker_start_token=worker_token)
            if not heartbeat or heartbeat["status"] != "running":
                _stop_process(process)
                return 130
            next_heartbeat = now + HEARTBEAT_INTERVAL_SEC
        sleep(GATE_INTERVAL_SEC)

    returncode = int(process.returncode or 0)
    background_jobs.finish(
        job_id, generation=generation,
        status="succeeded" if returncode == 0 else "failed",
        reason="" if returncode == 0 else f"update_exit_{returncode}",
        worker_pid=worker_pid, worker_start_token=worker_token)
    return returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--handle", required=True)
    args = parser.parse_args(argv)
    del args.session  # Owner is persisted in the registered job.
    stop = threading.Event()

    def request_stop(*_args) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return supervise_update(
        handle=args.handle, command=update_command(), stop_event=stop)


if __name__ == "__main__":
    raise SystemExit(main())
