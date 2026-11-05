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


_LEADER_EXITS_CHILD_IGNORES = (
    "import os, signal, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', "
    "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print(\"ready\", flush=True); time.sleep(120)'], stdout=subprocess.PIPE, text=True)\n"
    "child.stdout.readline()\n"            # the child ignores SIGTERM from here on
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n")


def _dead(pid):
    """Gone, or a zombie: killed and waiting for whoever inherited it to reap
    it. os.kill(pid, 0) still succeeds for a zombie, which made a killed
    child read as alive on a runner that reaps orphans late."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            stat = fh.read()
    except (FileNotFoundError, ProcessLookupError):
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def test_a_tool_child_keeps_the_full_grace_after_its_leader_exits_on_sigterm(tmp_path):
    """Reviewer A: the leader honours SIGTERM at once while a tool child in its
    group ignores it. The child is killed at the deadline, not straight away."""
    proc, handle, registry = _launched(tmp_path, _LEADER_EXITS_CHILD_IGNORES)
    child = int(proc.stdout.readline().strip())
    try:
        started = time.monotonic()
        registry.interrupt("synthetic", event="synthetic-interrupt")
        handle.wait(timeout=5)                       # the leader is gone at once
        time.sleep(max(0.0, STOP_KILL_DEADLINE_SEC - 2 - (time.monotonic() - started)))
        assert not _dead(child), "the group was killed before its grace ran out"
        deadline = time.monotonic() + 7
        while not _dead(child) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert _dead(child), "the SIGTERM-ignoring child outlived the deadline"
    finally:
        _cleanup(proc, handle)


def test_the_death_check_counts_a_zombie_as_dead_and_a_running_child_as_alive(tmp_path):
    """Controls for _dead: a killed child nobody has reaped yet is dead; a
    child nobody kills stays alive."""
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert not _dead(running.pid)
        running.kill()
        deadline = time.monotonic() + 5
        while not _dead(running.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _dead(running.pid), "a killed, unreaped child (a zombie) read as alive"
    finally:
        running.kill()
        running.wait(timeout=5)
    assert _dead(running.pid)


def test_a_child_that_is_never_killed_still_reads_as_alive(tmp_path, monkeypatch):
    """Control: with the escalation disabled, the SIGTERM-ignoring child is
    never killed, and the check must keep seeing it, so the deadline
    assertion above still fails for a child that truly outlives its stop."""
    proc, handle, registry = _launched(tmp_path, _LEADER_EXITS_CHILD_IGNORES)
    child = int(proc.stdout.readline().strip())
    monkeypatch.setattr(registry, "_escalate_later", lambda *_a, **_k: None)
    try:
        registry.interrupt("synthetic", event="synthetic-interrupt")
        handle.wait(timeout=5)                       # the leader is gone at once
        time.sleep(2.0)
        assert not _dead(child), "a running child read as dead"
    finally:
        import os
        import signal
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _cleanup(proc, handle)

