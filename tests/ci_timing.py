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

OPTION = "--clarp-timing-dir"
MIN_PHASE_SEC = 0.05
MAX_BYTES = 8 * 1024 * 1024

_state: dict = {}


def add_option(parser) -> None:
    parser.addoption(OPTION, default="", help="write per-test phase timings (JSON lines) here")


def directory(config) -> str:
    return str(config.getoption(OPTION.lstrip("-").replace("-", "_"), default="") or "")


def _worker() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER") or "controller"


def _write(record: dict, *, force: bool = False) -> None:
    handle = _state.get("handle")
    if handle is None:
        return
    line = json.dumps(record, separators=(",", ":")) + "\n"
    if not force and _state["bytes"] + len(line) > MAX_BYTES:
        _state["truncated"] += 1
        return
    handle.write(line)
    handle.flush()
    _state["bytes"] += len(line)


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
    if not target or "handle" in _state:
        return
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, f"timing-{_worker()}.jsonl")
    _state.update(handle=open(path, "a", buffering=1), bytes=0, truncated=0,
                  short={"count": 0, "seconds": 0.0}, started=time.monotonic())
    _write(_meta())


def phase(report) -> None:
    if "handle" not in _state:
        return
    duration = float(getattr(report, "duration", 0.0) or 0.0)
    if duration < MIN_PHASE_SEC:
        _state["short"]["count"] += 1
        _state["short"]["seconds"] += duration
        return
    live = threading.enumerate()
    _write({"type": "test", "id": report.nodeid, "phase": report.when,
            "outcome": report.outcome, "sec": round(duration, 4),
            "at": round(time.monotonic() - _state["started"], 3),
            "threads": len(live) - 1,
            "timers": sum(isinstance(t, threading.Timer) for t in live)})


def session_end(exitstatus) -> None:
    if "handle" not in _state:
        return
    _write({"type": "summary", "worker": _worker(), "exit": int(exitstatus),
            "elapsed": round(time.monotonic() - _state["started"], 3),
            "short_phases": _state["short"], "truncated_lines": _state["truncated"]},
           force=True)   # the summary is always written, past the cap
    _state.pop("handle").close()


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
