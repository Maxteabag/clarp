"""BACKENDS-07: an interrupt sent one SIGTERM to the turn's process group and
nothing more. A CLI or a tool child that ignores SIGTERM kept running, and
the next message waited behind it forever. An interrupt now escalates: if the
group is still there after STOP_KILL_DEADLINE_SEC, it is killed."""
from __future__ import annotations

import subprocess
import sys
import time

from lib import process_registry
from lib.backend.stream_json import launch
from lib.process_registry import ProcessRegistry

# The chosen deadline (10 s, as account_failover's stop); the fallback lets the
# test show the missing escalation on a build without the constant.
STOP_KILL_DEADLINE_SEC = getattr(process_registry, "STOP_KILL_DEADLINE_SEC", 10.0)

_IGNORES_SIGTERM = ("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "print('ready', flush=True); time.sleep(120)")


def _launched(tmp_path, code):
    proc, handle = launch([sys.executable, "-c", code], cwd=tmp_path, session="synthetic")
    registry = ProcessRegistry(log_exception=lambda *_a, **_k: None)
    registry.register("synthetic", handle)
    return proc, handle, registry


def _cleanup(proc, handle):
    handle.kill()
    handle.wait(timeout=5)
    proc._stderr_drain.text(timeout=1)
    proc.stdout.close()
    proc.stderr.close()


def test_an_interrupted_turn_that_ignores_sigterm_is_killed_at_the_deadline(tmp_path):
    proc, handle, registry = _launched(tmp_path, _IGNORES_SIGTERM)
    try:
        assert proc.stdout.readline().strip() == "ready"
        started = time.monotonic()
        assert registry.interrupt("synthetic", event="synthetic-interrupt") == 1
        try:
            handle.wait(timeout=STOP_KILL_DEADLINE_SEC + 5)
            stopped_after = time.monotonic() - started
        except subprocess.TimeoutExpired:
            stopped_after = None
    finally:
        _cleanup(proc, handle)
    assert stopped_after is not None, "SIGTERM-ignoring turn still alive after the deadline"
    assert stopped_after >= STOP_KILL_DEADLINE_SEC - 0.5   # SIGTERM first, the kill only at the deadline


def test_an_interrupted_turn_that_honours_sigterm_stops_at_once(tmp_path):
    """Control: an ordinary turn stops on SIGTERM, without waiting for the deadline."""
    proc, handle, registry = _launched(tmp_path, "import time; print('ready', flush=True); time.sleep(120)")
    try:
        assert proc.stdout.readline().strip() == "ready"
        started = time.monotonic()
        registry.interrupt("synthetic", event="synthetic-interrupt")
        handle.wait(timeout=STOP_KILL_DEADLINE_SEC)
        assert time.monotonic() - started < STOP_KILL_DEADLINE_SEC / 2
    finally:
        _cleanup(proc, handle)
