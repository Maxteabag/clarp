"""Emulate systemd's default control-group kill, safely, for restart tests.

`systemd/clarp.service` sets no `KillMode`, so it gets the default,
`control-group`: on `systemctl --user restart clarp` every process in the
unit's cgroup (and every sub-cgroup) is sent SIGTERM, then SIGKILL after
`TimeoutStopSec`. `start_new_session=True` creates a new session and process
group but *not* a new cgroup, so it does not escape that sweep. These helpers
reproduce the sweep in-process.

Safety rule for every function here: a process is only ever signalled when it
carries a caller-supplied marker token in its `/proc/<pid>/cmdline`. The marker
is a random hex string minted per test, so the sweep can only ever reach
processes the test itself spawned. The cgroup match alone is *not* enough --
this test process shares its cgroup with whatever launched pytest.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

PROC = pathlib.Path("/proc")

# A marker must look like the token `new_marker()` mints. Anything else is a
# programming error and must never reach `os.kill`.
_MARKER_RE = re.compile(r"^clarp-restart-test-[0-9a-f]{16}$")

WORKER_SOURCE = '''\
import pathlib, sys, time
# argv: <marker> <ready-file>
pathlib.Path(sys.argv[2]).write_text(str(__import__("os").getpid()))
while True:
    time.sleep(0.05)
'''


def new_marker() -> str:
    return f"clarp-restart-test-{uuid.uuid4().hex[:16]}"


def write_worker(directory: pathlib.Path) -> pathlib.Path:
    script = directory / "restart_fake_worker.py"
    script.write_text(WORKER_SOURCE)
    return script


def own_cgroup() -> str:
    return cgroup_of(os.getpid())


def cgroup_of(pid: int) -> str:
    """The cgroup-v2 path of a pid, or '' when it cannot be read."""
    try:
        raw = (PROC / str(pid) / "cgroup").read_text()
    except OSError:
        return ""
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            return parts[2].strip()
    return ""


def argv_of(pid: int) -> list[str]:
    try:
        raw = (PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def is_alive(pid: int) -> bool:
    """True while the pid exists and is not a zombie.

    Detached workers are still our children, so a dead one lingers as a zombie
    until it is reaped. `background_jobs.worker_is_alive` makes the same
    Z/X exclusion (server/lib/background_jobs.py:195-202).
    """
    try:
        raw = (PROC / str(pid) / "stat").read_text()
        state = raw.rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return bool(state) and state[0] not in {"Z", "X"}


def marked_pids(marker: str) -> list[int]:
    """Every live pid whose argv carries the marker."""
    _require_marker(marker)
    found = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if marker in argv_of(pid) and is_alive(pid):
            found.append(pid)
    return found


def control_group_kill(marker: str, *, cgroup: str | None = None,
                       sig: int = signal.SIGTERM) -> list[int]:
    """Signal every marked process sharing `cgroup` (default: our own).

    This is what `systemctl restart` does to a unit with the default
    `KillMode=control-group`, narrowed to processes this test created.
    """
    _require_marker(marker)
    target = own_cgroup() if cgroup is None else cgroup
    if not target:
        return []
    signalled = []
    for pid in marked_pids(marker):
        if cgroup_of(pid) != target:
            continue
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue
        signalled.append(pid)
    return signalled


def wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while is_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def wait_for_ready(ready: pathlib.Path, timeout: float = 10.0) -> int:
    """The worker's own pid, written once it is running in its final cgroup.

    Waiting on the marker alone is racy for the systemd-run paths: systemd-run
    carries the marker in its own argv, in the *old* cgroup, until it execs.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = ready.read_text().strip()
        except OSError:
            raw = ""
        if raw.isdigit():
            return int(raw)
        time.sleep(0.02)
    raise AssertionError(f"worker never became ready: {ready}")


def wait_for_marked(marker: str, count: int = 1, timeout: float = 10.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while True:
        pids = marked_pids(marker)
        if len(pids) >= count:
            return pids
        if time.monotonic() >= deadline:
            return pids
        time.sleep(0.02)


def reap(marker: str) -> None:
    """Teardown: SIGKILL anything still carrying the marker, then reap it."""
    for pid in marked_pids(marker):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    for pid in list(marked_pids(marker)):
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def spawn_same_session_worker(script: pathlib.Path, marker: str,
                              ready: pathlib.Path) -> subprocess.Popen:
    """Launch the worker the way five call sites in this codebase do.

    `start_new_session=True` and nothing else: server/lib/server_update.py:263,
    server/lib/service_manager.py:261, server/lib/transcription_models.py:529,
    server/lib/portrait_generation.py:131, scripts/server_update_job.py:140.
    """
    return subprocess.Popen(
        [sys.executable, str(script), marker, str(ready)],
        start_new_session=True, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)


def systemd_run_available() -> bool:
    """True when a transient user unit can actually be started here."""
    if sys.platform != "linux" or shutil.which("systemd-run") is None:
        return False
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    try:
        probe = subprocess.run(
            ["systemd-run", "--user", "--scope", "--collect", "--quiet",
             "--", "true"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def spawn_scope_worker(script: pathlib.Path, marker: str,
                       ready: pathlib.Path) -> subprocess.Popen:
    """Launch the worker into its own transient scope, i.e. its own cgroup."""
    return subprocess.Popen(
        ["systemd-run", "--user", "--scope", "--collect", "--quiet", "--",
         sys.executable, str(script), marker, str(ready)],
        start_new_session=True, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)


def scope_launcher(record: list[tuple[list[str], str]]):
    """A `launch_detached`-shaped launcher that really makes a new cgroup.

    `service_manager.launch_detached` shells out through `service_manager._run`,
    which the shared conftest fixture blocks from touching the live service
    manager. This stand-in keeps that guard intact while still exercising the
    only property under test: the worker ends up in a cgroup of its own.
    """
    def launch(command: list[str], *, unit: str,
               environment: dict[str, str] | None = None) -> tuple[bool, str]:
        record.append((list(command), unit))
        try:
            subprocess.Popen(
                ["systemd-run", "--user", "--scope", "--collect", "--quiet",
                 "--", *command],
                start_new_session=True, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, **(environment or {})})
        except OSError as exc:
            return False, str(exc)
        return True, ""
    return launch


def _require_marker(marker: str) -> None:
    if not _MARKER_RE.match(marker or ""):
        raise AssertionError(
            f"refusing to sweep processes for an unminted marker: {marker!r}")
