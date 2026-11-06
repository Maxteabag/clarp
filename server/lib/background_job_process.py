"""Moved out of server/lib/background_jobs.py by tools/refactor/split_apply.py (split 'background-jobs', recipe tools/refactor/splits/s9-background-jobs.toml).
background_jobs.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import hashlib
import ctypes
import ctypes.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Callable


WORKER_TERM_TIMEOUT_SEC = 2.0
WORKER_KILL_TIMEOUT_SEC = 2.0


def process_start_token(pid: int) -> str:
    """Stable process identity: boot session plus process start time."""
    if pid <= 0:
        return ""
    if sys.platform == "darwin":
        try:
            boot = subprocess.check_output(
                ["/usr/sbin/sysctl", "-n", "kern.boottime"], text=True,
                stderr=subprocess.DEVNULL).strip()
            started = subprocess.check_output(
                ["/bin/ps", "-p", str(pid), "-o", "lstart="], text=True,
                stderr=subprocess.DEVNULL).strip()
            return hashlib.sha256(
                f"{boot}\0{pid}\0{started}".encode()).hexdigest()
        except (OSError, subprocess.SubprocessError):
            return ""
    try:
        boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
        tail = raw.rsplit(")", 1)[1].split()
        return f"{boot_id}:{tail[19]}"
    except (OSError, IndexError):
        return ""


def current_worker_identity() -> tuple[int, str]:
    explicit = os.environ.get("CLARP_BACKGROUND_WORKER_PID", "").strip()
    pid = int(explicit) if explicit.isdigit() else os.getppid()
    command = process_command(pid)
    # Existing message-watch is the first adopted detached workflow. Other
    # callers use explicit heartbeats unless they opt in with the env override.
    if not explicit and "watch_messages.py" not in command:
        return 0, ""
    return pid, process_start_token(pid)


def process_command(pid: int) -> str:
    return " ".join(process_argv(pid))


def process_argv(pid: int) -> list[str]:
    if sys.platform == "darwin":
        try:
            return _parse_macos_procargs(_macos_procargs(pid))
        except (OSError, ValueError):
            return []
    try:
        return [
            part.decode(errors="replace")
            for part in pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if part
        ]
    except OSError:
        return []


def _parse_macos_procargs(raw: bytes) -> list[str]:
    """Parse the kern.procargs2 binary payload without losing spaces."""
    if len(raw) < 5:
        return []
    argc = int.from_bytes(raw[:4], byteorder=sys.byteorder, signed=True)
    if argc <= 0 or argc > 100_000:
        return []
    offset = raw.find(b"\0", 4)
    if offset < 0:
        return []
    offset += 1
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    argv = []
    for _ in range(argc):
        end = raw.find(b"\0", offset)
        if end < 0:
            return []
        argv.append(raw[offset:end].decode(errors="replace"))
        offset = end + 1
    return argv


def _macos_procargs(pid: int) -> bytes:
    """Read another process's argv through the Darwin KERN_PROCARGS2 MIB."""
    if pid <= 0:
        return b""
    library = ctypes.util.find_library("c")
    if not library:
        raise OSError("Darwin libc not found")
    libc = ctypes.CDLL(library, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
    size = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return buffer.raw[:size.value]


def adopted_worker_job_ids(pid: int, session: str) -> list[str]:
    """Exact message-watch targets encoded in the adopted worker argv."""
    argv = process_argv(pid)
    script_index = next(
        (index for index, arg in enumerate(argv) if "watch_messages.py" in arg),
        -1,
    )
    if script_index < 0 or script_index + 1 >= len(argv):
        return []
    subcommand = argv[script_index + 1]
    provider = {"whatsapp": "whatsapp", "himalaya": "email"}.get(
        subcommand, "")
    if not provider:
        return []
    labels: set[str] = set()
    mapping_flags = {"--watch", "--watch-name", "--from"}
    for index, arg in enumerate(argv):
        if arg in mapping_flags:
            if index + 1 < len(argv):
                raw = argv[index + 1]
                labels.add((raw.split("=", 1)[1] if "=" in raw else raw).strip())
        elif any(arg.startswith(f"{flag}=") for flag in mapping_flags):
            raw = arg.split("=", 1)[1]
            labels.add((raw.split("=", 1)[1] if "=" in raw else raw).strip())
        elif arg == "--subject-keyword" and index + 1 < len(argv):
            labels.add(argv[index + 1].strip())
        elif arg.startswith("--subject-keyword="):
            labels.add(arg.split("=", 1)[1].strip())
        if arg == "--reply-watch-json" and index + 1 < len(argv):
            try:
                data = json.loads(pathlib.Path(argv[index + 1]).read_text())
                if isinstance(data, dict):
                    labels.update(str(value).strip() for value in data.values())
            except (OSError, json.JSONDecodeError):
                pass
        elif arg.startswith("--reply-watch-json="):
            try:
                data = json.loads(pathlib.Path(arg.split("=", 1)[1]).read_text())
                if isinstance(data, dict):
                    labels.update(str(value).strip() for value in data.values())
            except (OSError, json.JSONDecodeError):
                pass
    result = []
    for label in sorted(label for label in labels if label):
        digest = hashlib.sha256(
            f"{session}\0{provider}\0{label}".encode()).hexdigest()[:16]
        result.append(f"message-watch-{provider}-{digest}")
    return result


def worker_is_alive(pid: int, expected_token: str) -> bool:
    if not expected_token or process_start_token(pid) != expected_token:
        return False
    try:
        if sys.platform == "darwin":
            state = subprocess.check_output(
                ["/bin/ps", "-p", str(pid), "-o", "stat="], text=True,
                stderr=subprocess.DEVNULL).strip()
        else:
            raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
            state = raw.rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError, subprocess.SubprocessError):
        return False
    return bool(state) and state[0] not in {"Z", "X"}


def terminate_worker(
    pid: int, expected_token: str, *,
    term_timeout: float = WORKER_TERM_TIMEOUT_SEC,
    kill_timeout: float = WORKER_KILL_TIMEOUT_SEC,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Stop one exact PID/start-token identity, escalating only while it matches."""
    if not worker_is_alive(pid, expected_token):
        return True

    def wait_until_gone(timeout: float) -> bool:
        deadline = monotonic() + max(0.0, timeout)
        while worker_is_alive(pid, expected_token):
            if monotonic() >= deadline:
                return False
            sleep(0.05)
        return True

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    if wait_until_gone(term_timeout):
        return True
    if not worker_is_alive(pid, expected_token):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return wait_until_gone(kill_timeout)
