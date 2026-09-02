#!/usr/bin/env python3
"""Durable worker for two OpenAI-backed portrait alternatives."""
from __future__ import annotations

import argparse
import os
import pathlib
import signal
import sys
import threading
import time

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
APP_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, os.environ.get(
    "CLARP_SHARE_DIR", str(pathlib.Path.home() / ".local/share/clarp")))
sys.path.insert(0, str(APP_ROOT if (APP_ROOT / "lib").is_dir() else APP_ROOT / "server"))

from lib import background_jobs, portrait_generation  # noqa: E402


def _fail_queued(job_id: str, generation: int, reason: str) -> None:
    try:
        current = background_jobs.get(job_id, reconcile=False)
        if (current and current["status"] == "queued"
                and int(current.get("generation") or 1) == generation):
            background_jobs.finish(
                job_id, generation=generation, status="failed", reason=reason)
    except Exception:
        pass


def run(*, handle: str, session: str, stop: threading.Event) -> int:
    job_id, generation = background_jobs.parse_job_handle(handle)
    pid = os.getpid()
    token = background_jobs.process_start_token(pid)
    if not token:
        _fail_queued(job_id, generation, "portrait_worker_identity_unavailable")
        return 2
    try:
        job = background_jobs.start(
            job_id, generation=generation, worker_pid=pid,
            worker_start_token=token)
    except Exception as exc:
        _fail_queued(
            job_id, generation,
            f"portrait_worker_start_failed:{type(exc).__name__}")
        return 2
    if not job or job["status"] != "running":
        return 2

    def active() -> bool:
        return not stop.is_set() and background_jobs.is_active_for_worker(
            job_id, generation=generation, worker_pid=pid,
            worker_start_token=token)

    def heartbeats() -> None:
        while not stop.wait(30.0):
            refreshed = background_jobs.heartbeat(
                job_id, generation=generation, worker_pid=pid,
                worker_start_token=token)
            if not refreshed or refreshed["status"] != "running":
                stop.set()
                return

    background_jobs.heartbeat(
        job_id, generation=generation, worker_pid=pid,
        worker_start_token=token)
    thread = threading.Thread(target=heartbeats, daemon=True)
    thread.start()
    try:
        portrait_generation.generate_two(
            session, handle=handle, should_continue=active)
    except portrait_generation.GenerationCancelled:
        current = background_jobs.get(job_id, reconcile=False)
        if current and current["status"] in background_jobs.ACTIVE_STATUSES:
            background_jobs.finish(
                job_id, generation=generation, status="failed",
                reason="portrait_worker_interrupted",
                worker_pid=pid, worker_start_token=token)
        return 130
    except Exception as exc:  # noqa: BLE001
        reason = str(exc).strip() or f"Portrait generation failed ({type(exc).__name__})"
        background_jobs.finish(
            job_id, generation=generation, status="failed",
            reason=reason[:1000],
            worker_pid=pid, worker_start_token=token)
        return 1
    finally:
        stop.set()
        thread.join(timeout=1.0)
    terminal = background_jobs.finish(
        job_id, generation=generation, status="succeeded",
        worker_pid=pid, worker_start_token=token)
    return 0 if terminal and terminal["status"] == "succeeded" else 130


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--handle", required=True)
    parser.add_argument("--session", required=True)
    args = parser.parse_args(argv)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    return run(handle=args.handle, session=args.session, stop=stop)


if __name__ == "__main__":
    raise SystemExit(main())
