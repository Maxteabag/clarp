#!/usr/bin/env python3
"""Generation-fenced supervisor for managed transcription model downloads."""
from __future__ import annotations

import argparse
import ctypes
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from typing import Callable

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
APP_ROOT = SCRIPT_DIR.parent


def _library_root(app_root: pathlib.Path) -> pathlib.Path:
    return app_root if (app_root / "lib").is_dir() else app_root / "server"


sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, os.environ.get(
    "CLARP_SHARE_DIR", str(pathlib.Path.home() / ".local/share/clarp")))
sys.path.insert(0, str(_library_root(APP_ROOT)))

from lib import background_jobs, transcription_models  # noqa: E402
from server_update_job import _stop_process  # noqa: E402


HEARTBEAT_INTERVAL_SEC = 30.0
GATE_INTERVAL_SEC = 0.5


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
        pass


def _rollback_cancelled_install(
    model_id: str, *, job_id: str, generation: int,
) -> None:
    try:
        transcription_models.rollback_cancelled_install(
            model_id, job_id=job_id, generation=generation)
    except Exception:
        # The managed installer already removes incomplete provider downloads.
        # A later remove/retry can surface any exceptional cleanup failure.
        pass


def _terminate_with_parent(expected_parent_pid: int) -> None:
    """Make the perform child die even if its detached supervisor crashes."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM) != 0:  # Linux PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


def supervise_install(
    *, handle: str, model_id: str,
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
        preexec = (lambda: _terminate_with_parent(worker_pid)) \
            if sys.platform.startswith("linux") else None
        process = popen([
            sys.executable, str(pathlib.Path(__file__).resolve()),
            "--perform", model_id,
        ], start_new_session=True,
            preexec_fn=preexec)
    except BaseException as exc:
        background_jobs.finish(
            job_id, generation=generation, status="failed",
            reason=f"install_launch_failed:{type(exc).__name__}",
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
            _rollback_cancelled_install(
                model_id, job_id=job_id, generation=generation)
            current = background_jobs.get(job_id, reconcile=False)
            if current and current["status"] in background_jobs.ACTIVE_STATUSES:
                background_jobs.finish(
                    job_id, generation=generation, status="failed",
                    reason="install_worker_stopped", worker_pid=worker_pid,
                    worker_start_token=worker_token)
            return 130
        now = monotonic()
        if now >= next_heartbeat:
            heartbeat = background_jobs.heartbeat(
                job_id, generation=generation, worker_pid=worker_pid,
                worker_start_token=worker_token)
            if not heartbeat or heartbeat["status"] != "running":
                _stop_process(process)
                _rollback_cancelled_install(
                    model_id, job_id=job_id, generation=generation)
                return 130
            next_heartbeat = now + HEARTBEAT_INTERVAL_SEC
        sleep(GATE_INTERVAL_SEC)

    returncode = int(process.returncode or 0)
    terminal = background_jobs.finish(
        job_id, generation=generation,
        status="succeeded" if returncode == 0 else "failed",
        reason="" if returncode == 0 else f"install_exit_{returncode}",
        worker_pid=worker_pid, worker_start_token=worker_token)
    if not terminal or terminal["status"] == "cancelled":
        _rollback_cancelled_install(
            model_id, job_id=job_id, generation=generation)
        return 130
    return returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--perform")
    parser.add_argument("--handle")
    parser.add_argument("--model-id")
    args = parser.parse_args(argv)
    if args.perform:
        transcription_models.install(args.perform)
        return 0
    if not args.handle or not args.model_id:
        parser.error("--handle and --model-id are required")
    stop = threading.Event()

    def request_stop(*_args) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return supervise_install(
        handle=args.handle, model_id=args.model_id, stop_event=stop)


if __name__ == "__main__":
    raise SystemExit(main())
