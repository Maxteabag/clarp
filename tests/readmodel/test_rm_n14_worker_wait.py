"""The retained oracle worker's pipe framing, deadline and high-FD contract."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
import resource
import select
import subprocess
import sys

import pytest

from tests.readmodel import rm_n14_harness as harness, world


def _high_owned_stdout(lane):
    # Duplicate ONLY our child's pipe, never occupy/close runner-owned FDs.
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert soft == resource.RLIM_INFINITY or soft > 1024, "BLOCKER: no high fd available"
    high = fcntl.fcntl(lane.process.stdout.fileno(), fcntl.F_DUPFD_CLOEXEC, 1024)
    lane.process.stdout.close()
    lane.process.stdout = os.fdopen(high, "rb", buffering=0)
    assert high >= 1024
    with pytest.raises(ValueError, match="filedescriptor out of range"):
        select.select([lane.process.stdout], [], [], 0)


@pytest.mark.parametrize("high_fd", [False, True])
def test_real_lane_keeps_worker_and_results_across_ticks(tmp_path, high_fd):
    database, transcript = tmp_path / "reference.sqlite", tmp_path / "synthetic.jsonl"
    world.prepare(database, "claude")
    transcript.write_bytes(harness.append_bytes("claude", 0))
    with harness.PersistentLane(database, transcript, "claude", "reference") as lane:
        if high_fd:
            _high_owned_stdout(lane)
        first = lane.tick(0)
        with transcript.open("ab") as output:
            output.write(harness.append_bytes("claude", 1))
        second = lane.tick(1)
        assert (first["tick"], first["rows"], second["tick"], second["rows"]) == (0, 3, 1, 6)
        assert first["process_id"] == second["process_id"] == lane.process.pid
    assert lane.process.poll() is not None
    assert lane.process.stdout.closed and lane.error_log.closed


@contextmanager
def _transport_lane(tmp_path, script, *, timeout=2):
    """Real child pipes isolate the IPC owner from transcript/SQLite work."""
    lane = harness.PersistentLane.__new__(harness.PersistentLane)
    lane.timeout, lane.buffer = timeout, b""
    lane.error_log = (tmp_path / "transport.stderr").open("wb")
    prefix = "import sys,os,time\nsys.stdout.buffer.write(b'ready\\n');sys.stdout.buffer.flush()\n"
    lane.process = subprocess.Popen([sys.executable, "-u", "-c", prefix + script],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=lane.error_log)
    try:
        assert lane.process.stdout.readline() == b"ready\n"
        with lane:
            yield lane
    finally:
        # __enter__ cannot fail; still reap if the startup handshake failed.
        if lane.process.poll() is None:
            lane.process.terminate()
            lane.process.wait(timeout=5)
        lane.error_log.close()
    assert lane.process.poll() is not None
    assert lane.process.stdout.closed and lane.error_log.closed


def test_partial_json_and_buffered_next_line_are_preserved(tmp_path):
    script = ("sys.stdin.readline()\n"
              "os.write(1,b'{\"tick\":')\n"
              "time.sleep(0.03)\n"
              "os.write(1,b'0}\\n{\"tick\":1}\\n')\n"
              "sys.stdin.readline()\nsys.stdin.readline()\n")
    with _transport_lane(tmp_path, script) as lane:
        assert lane.tick(0) == {"tick": 0}
        assert lane.buffer == b'{"tick":1}\n'
        assert lane.tick(1) == {"tick": 1}
        assert lane.buffer == b""


@pytest.mark.parametrize("drip", [False, True])
def test_worker_wait_deadline_is_absolute_even_with_partial_bytes(tmp_path, drip):
    script = "sys.stdin.readline()\n"
    if drip:
        script += "for _ in range(30):\n os.write(1,b' ');time.sleep(0.03)\n"
        script += "os.write(1,b'{\"tick\":0}\\n')\n"
    script += "sys.stdin.readline()\n"
    with _transport_lane(tmp_path, script, timeout=0.2) as lane:
        with pytest.raises(TimeoutError, match="persistent oracle worker deadline"):
            lane.tick(0)
        if drip:
            assert lane.buffer, "the deadline must hold after actual partial input"


@pytest.mark.parametrize("response,error,match", [
    ("raise SystemExit(7)\n", RuntimeError, "persistent oracle worker exited code="),
    ("os.write(1,b'not-json\\n')\nsys.stdin.readline()\n", ValueError, "Expecting value"),
])
def test_eof_and_malformed_completed_frames_remain_errors(tmp_path, response, error, match):
    with _transport_lane(tmp_path, "sys.stdin.readline()\n" + response) as lane:
        with pytest.raises(error, match=match):
            lane.tick(0)
