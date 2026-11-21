"""Every Claude stdin line carries a uuid (issue 491, part B, first commit).

Claude Code keeps a stdin line's uuid as that entry's uuid in its session file; a takeover
names message 1 by it (--resume-drops-turn). Writing it changes every Claude turn's launch,
so this pins that a turn without any follow-up runs exactly as before on the stand-in: the
same stdin line but for the one added key, the same events read, the same callbacks."""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import time
import uuid

import pytest

from lib.backend.claude import prompt_uuid
from lib.backend.registry import by_id

CLAUDE = by_id("claude")
EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "s-1"},
    {"type": "assistant", "message": {"id": "m-1", "role": "assistant",
                                      "content": [{"type": "text", "text": "Paris."}]}},
    {"type": "result", "subtype": "success", "result": "Paris."},
]


@pytest.fixture
def stand_in(fake_backend_cli, tmp_path, monkeypatch):
    """A fake Claude CLI on PATH that writes the stdin it was given to a file and prints the
    fixed events; this test's own file under tmp_path (the real-CLI guard's opt-in)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    received = tmp_path / "stdin.jsonl"
    payload = "\n".join(json.dumps(e) for e in EVENTS)
    script = textwrap.dedent(f"""\
        #!{sys.executable}
        import sys
        if "--version" in sys.argv:
            print("2.1.321 (Claude Code)")
            raise SystemExit(0)
        open({str(received)!r}, "w").write(sys.stdin.read())
        sys.stdout.write({payload!r} + "\\n")
        sys.stdout.flush()
    """)
    for name in ("claude", "clarp"):
        path = bin_dir / name
        path.write_text(script)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    fake_backend_cli.dir(tmp_path)
    return received


def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_the_uuid_is_the_traces_and_the_same_every_time():
    first = prompt_uuid("t-abc")
    assert first == prompt_uuid("t-abc") and first != prompt_uuid("t-abd")
    assert str(uuid.UUID(first)) == first
    assert prompt_uuid("") != prompt_uuid("")          # no trace: a fresh one each time


def test_a_turn_without_a_follow_up_runs_as_before(stand_in, tmp_path):
    sids, results, errors = [], [], []
    handle = CLAUDE.start_turn(
        text="What is the capital of France?", cwd=tmp_path, trace_id="t-plain",
        on_session_init=sids.append, on_result=results.append, on_error=errors.append)
    handle.wait(timeout=10.0)
    assert _wait_for(lambda: len(results) == 1), (results, errors)
    assert sids == ["s-1"] and errors == []
    assert results[0].get("result") == "Paris."
    [line] = stand_in.read_text().splitlines()           # one line, stdin then closed
    sent = json.loads(line)
    assert set(sent) == {"type", "uuid", "message"}       # before: type and message only
    assert sent["type"] == "user"
    assert sent["message"] == {"role": "user", "content": [
        {"type": "text", "text": "What is the capital of France?"}]}
    assert sent["uuid"] == prompt_uuid("t-plain")
