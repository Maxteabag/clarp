"""Opt-in per-test timing for CI diagnosis. A no-op unless pytest gets --clarp-timing-dir.

The CI tests job passes it on the Python step's command line only (it crosses
scripts/ci_contained.sh as an argument; that script clears the environment). Each xdist worker (and the
controller) appends JSON lines to its own file, flushed per line, so a job the
runner cancels at its time limit still leaves everything recorded so far for
the existing `if: always()` artifact upload. JUnit is written only at session
end and is missing on such a cancel.

Per worker file: one `meta` line (worker, CPUs, load, memory, Python and
plugin versions), then a `test` line for every setup / call / teardown phase
of at least MIN_PHASE_SEC (with the live thread and Timer counts after it) and
a `summary` line at the end with the totals of the shorter phases. Output stops
at MAX_BYTES per file; the summary still counts what was not written.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time

MIN_PHASE_SEC = 0.05
MAX_BYTES = 8 * 1024 * 1024

# Bound at import (pytest_configure, before any test), because tests replace
# threading.Timer with factories that are not classes.
_TIMER_TYPE = threading.Timer if isinstance(threading.Timer, type) else None

_state: dict = {}


def _worker() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER") or "controller"


def _note(message: str) -> None:
    """Best-effort stderr note; a broken stderr is ignored. Exception only: an
    interrupt or a timeout signal still propagates as pytest expects."""
    try:
        sys.stderr.write(f"ci_timing: {message}\n")
    except Exception:  # noqa: BLE001
        pass


def _close(handle) -> None:
    try:
        handle.close()
    except Exception as exc:  # noqa: BLE001
        _note(f"close failed: {exc!r}")


def _write(record: dict, *, force: bool = False, handle=None) -> None:
    """Write one line; raises on an I/O error (callers contain it)."""
    handle = handle if handle is not None else _state.get("handle")
    if handle is None:
        return
    line = json.dumps(record, separators=(",", ":")) + "\n"
    if not force and _state["bytes"] + len(line) > MAX_BYTES:
        _state["truncated"] += 1
        return
    handle.write(line)
    handle.flush()
    _state["bytes"] += len(line)
    _state["written"] += 1


def _meta() -> dict:
    try:
        load = os.getloadavg()
    except OSError:
        load = None
    memory = {}
    try:
        with open("/proc/meminfo") as fh:
            for row in fh:
                key, _, value = row.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    memory[key] = value.strip()
    except OSError:
        pass
    versions = {}
    for name in ("pytest", "xdist", "pytest_timeout"):
        module = sys.modules.get(name)
        versions[name] = getattr(module, "__version__", None)
    return {"type": "meta", "worker": _worker(), "pid": os.getpid(), "cpus": os.cpu_count(),
            "loadavg": load, "memory": memory, "python": platform.python_version(),
            "versions": versions, "run": os.environ.get("GITHUB_RUN_ID"),
            "attempt": os.environ.get("GITHUB_RUN_ATTEMPT"), "start": time.time()}


def session_start(target: str) -> None:
    """Never raises: any failure leaves diagnostics off (and the file closed)."""
    if not target or "handle" in _state:
        return
    try:
        os.makedirs(target, exist_ok=True)
        handle = open(os.path.join(target, f"timing-{_worker()}.jsonl"), "a", buffering=1)
    except Exception as exc:  # noqa: BLE001
        _note(f"disabled (no file): {exc!r}")
        return
    _state.update(handle=handle, bytes=0, truncated=0, errors=0, written=0,
                  short={"count": 0, "seconds": 0.0}, started=time.monotonic())
    try:
        _write(_meta())
    except Exception as exc:  # noqa: BLE001
        _state.clear()
        _close(handle)
        _note(f"disabled (meta not written): {exc!r}")


def phase(report) -> None:
    if "handle" not in _state:
        return
    try:
        duration = float(getattr(report, "duration", 0.0) or 0.0)
        if duration < MIN_PHASE_SEC:
            _state["short"]["count"] += 1
            _state["short"]["seconds"] += duration
            return
        live = threading.enumerate()
        timers = sum(isinstance(t, _TIMER_TYPE) for t in live) if _TIMER_TYPE else None
        _write({"type": "test", "id": report.nodeid, "phase": report.when,
                "outcome": report.outcome, "sec": round(duration, 4),
                "at": round(time.monotonic() - _state["started"], 3),
                "threads": len(live) - 1, "timers": timers})
    except Exception:  # noqa: BLE001 - counted in the summary, never raised into pytest
        _state["errors"] = _state.get("errors", 0) + 1


def session_end(exitstatus) -> None:
    """Never raises. The summary reports what was actually written; with any
    writer error its status is 'degraded' and missing lines are unknown."""
    handle = _state.pop("handle", None)
    if handle is None:
        return
    try:
        errors = _state.get("errors", 0)
        _write({"type": "summary", "worker": _worker(), "exit": int(exitstatus),
                "elapsed": round(time.monotonic() - _state["started"], 3),
                "status": "degraded" if errors else "ok", "errors": errors,
                "written_lines": _state["written"], "short_phases": _state["short"],
                "truncated_lines": _state["truncated"]},
               force=True, handle=handle)   # always attempted, past the cap
    except Exception as exc:  # noqa: BLE001
        _note(f"summary not written: {exc!r}")
    finally:
        _state.clear()
        _close(handle)


class Plugin:
    """Registered by tests/conftest.py only with --clarp-timing-dir; owns its own hooks."""

    def __init__(self, target: str, record_tests: bool = True):
        self.target = target
        # Under xdist the controller receives every worker's reports again;
        # only the workers record test lines, the controller its meta/summary.
        self.record_tests = record_tests

    def pytest_sessionstart(self, session):
        session_start(self.target)

    def pytest_runtest_logreport(self, report):
        if self.record_tests:
            phase(report)

    def pytest_sessionfinish(self, session, exitstatus):
        session_end(exitstatus)
