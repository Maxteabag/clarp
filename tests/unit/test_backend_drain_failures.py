"""BACKENDS-03 and the quick part of BACKENDS-04.

A CLI that writes bytes that are not UTF-8 must not stop its pipes from being
read, and a Claude or Codex drain that fails reading must still end its turn:
one terminal outcome and no process left running on the session."""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import Mock

from lib.backend.stream_json import launch
from lib.process_registry import TurnHandle


def _run(tmp_path, code):
    proc, handle = launch([sys.executable, "-c", code], cwd=tmp_path, session="synthetic")
    try:
        try:
            proc.wait(timeout=5)
            exited = True
        except subprocess.TimeoutExpired:
            exited = False
        out = proc.stdout.read() if exited else ""
    finally:
        if proc.poll() is None:
            handle.kill()
        proc.wait(timeout=5)
    err = proc._stderr_drain.text(timeout=2)
    proc.stdout.close()
    proc.stderr.close()
    return exited, out, err


def test_invalid_stderr_bytes_do_not_stop_the_stderr_drain(tmp_path):
    # Past the 64 KiB pipe: a dead drain would leave the child blocked.
    exited, _out, err = _run(
        tmp_path, "import os; os.write(2, b'\\xff\\xfe'); os.write(2, b'x' * 200000)")
    assert exited, "the child blocked on a stderr pipe nobody reads"
    assert err.endswith("x" * 1000)


def test_invalid_stdout_bytes_are_read_not_raised(tmp_path):
    exited, out, _err = _run(
        tmp_path, "import os; os.write(1, b'\\xff noise\\n{\"type\": \"ok\"}\\n')")
    assert exited
    assert out.splitlines()[-1] == '{"type": "ok"}'


class _FailingStdout:
    def __iter__(self):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic")


def _proc():
    proc = Mock(stdout=_FailingStdout())
    proc.poll.return_value = None
    proc.wait.return_value = 0
    return proc


def test_a_codex_drain_that_fails_reading_ends_its_turn():
    from lib.backend.codex import CodexBackend
    proc = _proc()
    handle = TurnHandle(proc, None)
    backend = CodexBackend()
    backend.register_handle("synthetic", handle)
    result, error = Mock(), Mock()
    backend._drain_stdout(
        proc=proc, agent_id="synthetic", session="synthetic", trace_id="synthetic-trace",
        backend_session_id="synthetic-thread", handle=handle, on_session_init=None,
        on_result=result, on_error=error, stream=None, enqueue=lambda **kw: 0)
    assert result.call_count + error.call_count == 1
    assert proc.terminate.called, "the CLI was left running on the session"


def test_a_claude_drain_that_fails_reading_ends_its_turn():
    from lib.backend.claude import ClaudeBackend
    proc = _proc()
    handle = TurnHandle(proc, None)
    backend = ClaudeBackend()
    backend.register_handle("synthetic", handle)
    result, error = Mock(), Mock()
    backend._drain_stdout(proc, None, result, error, "synthetic-trace",
                          agent_id="synthetic", handle=handle, session="synthetic")
    assert result.call_count + error.call_count == 1
    assert proc.terminate.called, "the CLI was left running on the session"
