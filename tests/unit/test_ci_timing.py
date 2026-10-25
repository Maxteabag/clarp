"""The opt-in CI timing writer: per-phase lines above a threshold, a bounded file,
a summary that still counts what it did not write."""
import json
from types import SimpleNamespace

import ci_timing


def _report(nodeid, when, seconds):
    return SimpleNamespace(nodeid=nodeid, when=when, outcome="passed", duration=seconds)


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_writes_meta_long_phases_and_a_summary_of_the_short_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(ci_timing, "_state", {})
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw7")
    ci_timing.session_start(str(tmp_path))
    ci_timing.phase(_report("t::slow", "call", 0.5))
    ci_timing.phase(_report("t::fast", "call", 0.001))
    ci_timing.session_end(0)
    rows = _lines(tmp_path / "timing-gw7.jsonl")
    assert [r["type"] for r in rows] == ["meta", "test", "summary"]
    assert rows[0]["worker"] == "gw7" and rows[0]["cpus"]
    assert (rows[1]["id"], rows[1]["phase"], rows[1]["sec"]) == ("t::slow", "call", 0.5)
    assert rows[2]["short_phases"]["count"] == 1 and rows[2]["truncated_lines"] == 0


def test_the_file_is_bounded_and_the_summary_reports_the_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(ci_timing, "_state", {})
    monkeypatch.setattr(ci_timing, "MAX_BYTES", 2048)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    ci_timing.session_start(str(tmp_path))
    for n in range(100):
        ci_timing.phase(_report(f"t::case_{n}", "call", 1.0))
    ci_timing.session_end(1)
    path = tmp_path / "timing-gw0.jsonl"
    rows = _lines(path)
    assert path.stat().st_size <= 2048 + 512        # cap plus the final summary line
    assert rows[-1]["type"] == "summary" and rows[-1]["truncated_lines"] > 0
    assert rows[-1]["exit"] == 1


def test_without_a_directory_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(ci_timing, "_state", {})
    ci_timing.session_start("")
    ci_timing.phase(_report("t::x", "call", 9.0))
    ci_timing.session_end(0)
    assert list(tmp_path.iterdir()) == []


def test_a_test_that_replaces_threading_timer_cannot_break_the_writer(tmp_path, monkeypatch):
    # CI 40223435447: a test patched threading.Timer with a factory function, and
    # isinstance(t, threading.Timer) raised inside the logreport hook, crashing
    # an xdist worker. The writer binds the real class at import instead.
    import threading
    monkeypatch.setattr(ci_timing, "_state", {})
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    ci_timing.session_start(str(tmp_path))
    monkeypatch.setattr(threading, "Timer", lambda *a, **k: None)
    ci_timing.phase(_report("t::patched", "call", 0.5))
    ci_timing.session_end(0)
    rows = _lines(tmp_path / "timing-gw3.jsonl")
    assert [r["type"] for r in rows] == ["meta", "test", "summary"]
    assert isinstance(rows[1]["timers"], int) and rows[-1]["errors"] == 0


def test_any_error_in_the_writer_is_counted_never_raised(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(ci_timing, "_state", {})
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw4")
    ci_timing.session_start(str(tmp_path))

    def broken():
        raise RuntimeError("thread table unavailable")
    monkeypatch.setattr(threading, "enumerate", broken)
    ci_timing.phase(_report("t::x", "call", 0.5))         # must not raise
    ci_timing.session_end(0)
    assert _lines(tmp_path / "timing-gw4.jsonl")[-1]["errors"] == 1


# ---- fault injection through a real inner pytest session -------------------------------

import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parents[1]

_INNER_CONFTEST = """
import pathlib, sys, types
sys.path.insert(0, {tests_dir!r})
import os, ci_timing

MODE = os.environ["CI_TIMING_FAULT"]
CLOSED = pathlib.Path({closed!r})
FAULTED = pathlib.Path({faulted!r})


def _fault(message):
    FAULTED.write_text(message)          # proof the injected fault really fired
    raise OSError(message)


class Faulty:
    def __init__(self, real):
        self.real, self.lines = real, 0
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self.close()
    def write(self, text):
        self.lines += 1
        if MODE == "startup_stderr" or (MODE == "write" and self.lines > 1):
            _fault("disk full")
        return self.real.write(text)
    def flush(self):
        if MODE == "flush" and self.lines > 1:
            _fault("flush failed")
        return self.real.flush()
    def close(self):
        CLOSED.write_text("closed")
        self.real.close()
        if MODE == "close":
            _fault("close failed")


class BrokenStderr:
    def write(self, text):
        raise OSError("stderr gone")


_real_open = open


def _open(path, *args, **kwargs):
    # Only the timing file is faulty; /proc reads in _meta stay real.
    real = _real_open(path, *args, **kwargs)
    return Faulty(real) if str(path).endswith(".jsonl") else real


ci_timing.open = _open
if MODE == "startup_stderr":
    ci_timing.sys = types.SimpleNamespace(stderr=BrokenStderr(), modules=sys.modules)


def pytest_configure(config):
    config.pluginmanager.register(ci_timing.Plugin({target!r}), "clarp-ci-timing")
"""

_INNER_TESTS = """
import time

def test_sentinel_passes():
    time.sleep(0.06)

def test_sentinel_fails():
    time.sleep(0.06)
    assert False, "sentinel failure"
"""


@pytest.mark.parametrize("mode", ["write", "flush", "close", "startup_stderr"])
def test_a_failing_writer_never_changes_the_test_outcome(tmp_path, mode):
    suite = tmp_path / "inner"
    suite.mkdir()
    closed, faulted = tmp_path / "closed-marker", tmp_path / "faulted-marker"
    (suite / "conftest.py").write_text(textwrap.dedent(_INNER_CONFTEST).format(
        tests_dir=str(TESTS_DIR), closed=str(closed), faulted=str(faulted),
        target=str(tmp_path / "timing")))
    (suite / "test_inner.py").write_text(textwrap.dedent(_INNER_TESTS))
    env = {**os.environ, "CI_TIMING_FAULT": mode}
    for key in ("PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER", "PYTEST_CURRENT_TEST"):
        env.pop(key, None)
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-n", "0",
         "--rootdir", str(suite), "--basetemp", str(tmp_path / "inner-tmp"), str(suite)],
        cwd=suite, env=env, capture_output=True, text=True, timeout=120)
    output = done.stdout + done.stderr
    assert done.returncode == 1, output                    # the sentinel's failure, nothing else
    assert "1 failed, 1 passed" in output, output
    assert "INTERNALERROR" not in output, output
    assert faulted.exists(), "the injected fault really fired"
    assert closed.exists(), "the timing file is always closed, also after a startup failure"
    lines = (tmp_path / "timing" / "timing-controller.jsonl").read_text().splitlines()
    if mode == "startup_stderr":
        assert lines == [], "nothing was written; diagnostics were disabled, not faked"
    else:
        assert json.loads(lines[0])["type"] == "meta"     # the writer really started
    if mode == "close":
        assert json.loads(lines[-1])["status"] == "ok"     # the summary was written before close
