"""The sim-e2e server harness must not leak its scratch HOME.

Each vitest run spawns tests/sim-e2e/server_harness.py, which boots a real
server inside a fresh temp folder. Those folders were created in /tmp (a small
RAM-backed tmpfs) and never removed: vitest's afterAll ends the harness with
SIGTERM, which killed Python before any cleanup, so about 77 folders of up to
870MB piled up and filled /tmp. Most of that size came from the server's
provider-capability probes: they ran the developer's real `opencode` and
`grok` through mise with HOME pointing into the folder, so mise installed
toolchains there, and those grandchildren outlived the harness and kept
writing after any cleanup.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

HARNESS = pathlib.Path(__file__).resolve().parents[1] / "sim-e2e" / "server_harness.py"


def _start(env):
    proc = subprocess.Popen(
        [sys.executable, str(HARNESS)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True,
    )
    banner = json.loads(proc.stdout.readline())
    assert banner["ready"] is True, banner
    tmpdir = pathlib.Path(banner["tmpdir"])
    assert tmpdir.is_dir()
    return proc, tmpdir


def _env(**overrides):
    env = {k: v for k, v in os.environ.items()
           if k not in ("TMPDIR", "CLARP_CODE_ROOT")}
    env.update(overrides)
    return env


@pytest.fixture
def scratch(tmp_path):
    return _env(TMPDIR=str(tmp_path))


def _finish(proc):
    # stderr reaches EOF only once the harness's reaper, which inherits it,
    # has finished cleaning up.
    try:
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return proc.stderr.read()


def test_exit_command_removes_the_scratch_home(scratch):
    proc, tmpdir = _start(scratch)
    proc.stdin.write(json.dumps({"cmd": "exit"}) + "\n")
    proc.stdin.flush()
    assert json.loads(proc.stdout.readline()) == {"ok": True}
    _finish(proc)
    assert not tmpdir.exists()


def test_sigterm_removes_the_scratch_home(scratch):
    # vitest's afterAll sends `exit` and then proc.kill() (SIGTERM) without
    # waiting, so SIGTERM often lands first.
    proc, tmpdir = _start(scratch)
    proc.send_signal(signal.SIGTERM)
    _finish(proc)
    assert not tmpdir.exists()
    assert proc.returncode != 0  # a kill still reads as a kill, not a clean exit


def test_sigkill_removes_the_scratch_home(scratch):
    proc, tmpdir = _start(scratch)
    proc.kill()
    _finish(proc)
    assert not tmpdir.exists()


def test_closed_stdin_removes_the_scratch_home(scratch):
    # The Node runner crashing closes the pipe without any command.
    proc, tmpdir = _start(scratch)
    proc.stdin.close()
    _finish(proc)
    assert not tmpdir.exists()


def test_scratch_home_avoids_tmp_when_no_tmpdir_is_chosen():
    if not os.path.isdir("/var/tmp"):
        pytest.skip("no /var/tmp on this machine")
    proc, tmpdir = _start(_env())
    try:
        assert tmpdir.parent == pathlib.Path("/var/tmp")
        assert tmpdir.name.startswith("claude-pwa-e2e-")
    finally:
        proc.send_signal(signal.SIGTERM)
        _finish(proc)
    assert not tmpdir.exists()


def test_scratch_home_honours_an_explicit_tmpdir(scratch, tmp_path):
    proc, tmpdir = _start(scratch)
    try:
        assert tmpdir.parent == tmp_path
    finally:
        proc.send_signal(signal.SIGTERM)
        _finish(proc)


def _group_members(pgid):
    members = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # Fields after the parenthesised command: state ppid pgrp ...
        fields = stat.rsplit(")", 1)[1].split()
        if int(fields[2]) == pgid and fields[0] != "Z":
            members.append(int(entry.name))
    return members


def _pgid(pid):
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None


def _children(pid):
    found = []
    for task in pathlib.Path(f"/proc/{pid}/task").iterdir():
        try:
            found += [int(c) for c in (task / "children").read_text().split()]
        except FileNotFoundError:
            pass  # the thread ended while we looked
    return found


@pytest.mark.skipif(not pathlib.Path("/proc/self/task").is_dir(), reason="needs /proc")
def test_harness_runs_no_provider_cli_probes(scratch):
    proc, tmpdir = _start(scratch)
    try:
        # The cache warm-up starts its model-catalogue probe 1.5 s after boot.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            # The cleanup reaper is a child too, in a group of its own.
            assert [c for c in _children(proc.pid) if _pgid(c) == proc.pid] == []
            time.sleep(0.1)
        assert not (tmpdir / "data" / "mise").exists()
        assert not (tmpdir / "cache" / "mise").exists()
    finally:
        proc.send_signal(signal.SIGTERM)
        _finish(proc)


@pytest.mark.skipif(not pathlib.Path("/proc/self/task").is_dir(), reason="needs /proc")
def test_nothing_the_harness_started_survives_it(scratch):
    proc, tmpdir = _start(scratch)
    # Stand-in for any process the server spawns that would outlive it and
    # keep writing into the scratch HOME: it ignores SIGTERM and recreates a
    # folder there every 50 ms.
    proc.stdin.write(json.dumps({"cmd": "spawn_straggler"}) + "\n")
    proc.stdin.flush()
    straggler = json.loads(proc.stdout.readline())["pid"]
    assert os.getpgid(straggler) == proc.pid
    proc.send_signal(signal.SIGTERM)
    _finish(proc)
    time.sleep(1.5)
    assert _group_members(proc.pid) == []
    assert not tmpdir.exists()
