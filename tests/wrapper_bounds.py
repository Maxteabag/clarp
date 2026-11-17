"""Contain disposable wrapper probes; never retry after a breached bound."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess

import pytest


def bounded_run(cmd, *, env=None, cwd=None, input=None, check=False):
    required = {name: shutil.which(name) for name in ("timeout", "prlimit", "pgrep")}
    if not all(required.values()) or not Path("/proc/loadavg").is_file():
        pytest.skip("wrapper containment requires GNU timeout/prlimit and Linux /proc")
    assert os.geteuid() != 0, "RLIMIT_NPROC must be enforced for the probe user"
    uid = os.environ.get("PYTEST_XDIST_TESTRUNUID", f"serial-{os.getpid()}")
    key = hashlib.sha256(uid.encode()).hexdigest()[:20]
    controls = Path(os.environ.get("TMPDIR", "/var/tmp")) / f"wrapper-bounds-{key}"
    controls.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = controls / "STOP.json"
    if stop.exists():
        pytest.exit("WRAPPER_BOUNDS_STOP: earlier bound breached; ask Arnold", returncode=77)
    current = int(Path("/proc/loadavg").read_text().split()[3].split("/")[1])
    soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    limit = min([current + 32] + [n for n in (soft, hard) if n >= 0])
    command = [required["timeout"], "-s", "KILL", "2", required["prlimit"],
               f"--nproc={limit}:{limit}", "--", *map(str, cmd)]
    proc = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    expired = False
    try:
        out, err = proc.communicate(input, timeout=3)
    except subprocess.TimeoutExpired:
        expired = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        remaining = subprocess.run([required["pgrep"], "-g", str(proc.pid)], capture_output=True)
        record = {"pgid": proc.pid, "timeout_seconds": 2, "nproc_limit": limit,
                  "status": proc.returncode, "outer_timeout": expired,
                  "pgrep_after_cleanup": remaining.returncode}
        (controls / f"{proc.pid}.json").write_text(json.dumps(record))
        breached = expired or proc.returncode in (-signal.SIGKILL, 128 + signal.SIGKILL) or remaining.returncode != 1
        if breached:
            stop.write_text(json.dumps(record))
            pytest.exit("WRAPPER_BOUNDS_STOP: killed probe or nonempty group; ask Arnold, no retry", returncode=77)
    result = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if check:
        result.check_returncode()
    return result
