"""A stopped turn must not leave its backend descendants running."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import time

import pytest

from lib import agents, clarp_runner


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the deterministic descendant liveness probe uses /proc",
)


def _process_alive(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat")
    if not stat.is_file():
        return False
    try:
        return stat.read_text().split()[2] != "Z"
    except (OSError, IndexError):
        return False


def test_interrupt_terminates_the_backend_process_tree(tmp_path, monkeypatch):
    """Claude/Codex subagents are descendants, not registry entries of their own.

    The registry currently sends SIGTERM only to the top-level CLI. This proves
    a descendant can survive a user stop even though Clarp reports one process
    interrupted. The adapter runner already uses a new session plus killpg for
    this same lifecycle requirement.
    """
    pid_file = tmp_path / "child-pid"
    executable = tmp_path / "backend.py"
    executable.write_text(
        "import pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(60)\n")
    monkeypatch.setattr(clarp_runner.shutil, "which", lambda _name: sys.executable)
    monkeypatch.setattr(clarp_runner, "build_cmd", lambda *a, **kw: [sys.executable, str(executable)])
    agent_id = agents.create_agent(
        persona="Fixture", voice_id="", cwd=str(tmp_path), session="fixture")
    handle = clarp_runner.spawn_turn(
        text="fixture", cwd=tmp_path, session="fixture", agent_id=agent_id)
    parent = handle.proc
    child_pid = 0
    try:
        deadline = time.monotonic() + 5
        while not pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.is_file(), "fixture parent never spawned its child"
        child_pid = int(pid_file.read_text())

        assert clarp_runner.interrupt(agent_id) == 1
        parent.wait(timeout=5)

        deadline = time.monotonic() + 1
        while _process_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _process_alive(child_pid), (
            "backend descendant survived after the registered parent stopped"
        )
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if child_pid and _process_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)
        if handle.drain_thread:
            handle.drain_thread.join(timeout=5)
