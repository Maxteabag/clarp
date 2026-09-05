"""Stop reaches descendants of each real runner, without signaling other turns."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from lib import agents

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="liveness probe uses /proc")


def _alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except (OSError, IndexError):
        return False


@pytest.mark.parametrize("backend", ["clarp", "codex", "agy", "grok", "opencode"])
def test_stopping_runner_terminates_descendants_only(tmp_path, monkeypatch, backend):
    runner = importlib.import_module(f"lib.{backend}_runner")
    pid_file = tmp_path / "child.pid"
    executable = tmp_path / "backend.py"
    executable.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(60)\n")
    monkeypatch.setattr(runner.shutil, "which", lambda _name: sys.executable)
    monkeypatch.setattr(runner, "build_cmd", lambda *a, **kw: [sys.executable, str(executable)])
    agent_id = agents.create_agent(
        persona="Fixture", voice_id="", cwd=str(tmp_path), session="fixture",
        backend="claude" if backend == "clarp" else backend)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    handle = None
    child_pid = 0
    try:
        handle = runner.spawn_turn(
            text="fixture", cwd=tmp_path, session="fixture", agent_id=agent_id)
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text())
        assert runner.interrupt(agent_id) == 1
        handle.wait(timeout=5)
        deadline = time.monotonic() + 2
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _alive(child_pid), "stopped turn left its child running"
        assert other.poll() is None, "stop reached a process outside this turn"
    finally:
        if handle is not None and handle.is_alive():
            handle.proc.kill()
            handle.wait(timeout=5)
        if child_pid and _alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)
        other.terminate()
        other.wait(timeout=5)
        if handle is not None and handle.drain_thread:
            handle.drain_thread.join(timeout=5)


def test_account_recovery_kills_detached_output_descendant(tmp_path):
    from lib.claude_failover import finish_owned_group
    from lib.process_registry import TurnHandle
    import threading
    child_code = ("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  "print('ready',flush=True); time.sleep(60)")
    child_pid_file = tmp_path / "survivor.pid"
    parent_code = (
        "import subprocess,sys,time,pathlib; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdout=subprocess.PIPE); "
        "child.stdout.readline(); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
        "time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", parent_code],
                            stdout=subprocess.PIPE, start_new_session=True)
    drain = threading.Thread(target=lambda: proc.stdout.read(), daemon=True)
    handle = TurnHandle(proc, drain, process_group=proc.pid)
    drain.start()
    child_pid = 0
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        child_pid = int(child_pid_file.read_text())
        handle.terminate()
        handle.wait(timeout=2)
        assert not drain.is_alive()
        assert _alive(child_pid), "fixture must reproduce a surviving tool"
        finish_owned_group(handle)
        assert not _alive(child_pid)
    finally:
        handle.kill()
        handle.wait(timeout=5)
        proc.stdout.close()
