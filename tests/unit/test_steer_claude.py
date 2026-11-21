"""Part C of issue 491: a follow-up written into a running Claude turn, read at its next boundary.

Driven through the real dispatcher and the real Claude runner, with a stand-in `claude` that keeps
its stdin open and replays the event orders of the recordings (Claude Code 2.1.321):
- case 10 and the tool cases of case 2: a follow-up written while a tool call runs is echoed
  (--replay-user-messages) right after the tool's result, inside the same turn: one result;
- case 11: two follow-ups during one tool call, both echoed after its result, in the order sent;
- case 9: a follow-up echoed after the turn's result (the CLI answers it in a second turn of its
  own in the same process);
plus: the process ending before it reads a written follow-up (it runs as its own turn, once); a
follow-up written while the model generates between tool calls (read at the next boundary, in
the same turn) or while the turn only writes (read after its result, in the same process); a
Claude older than the recorded one (not steered).
The row of a follow-up is written at its echo, never at its write; until then it is a queue item
that may not be edited (its receipt says it may have left).
"""
from __future__ import annotations

import json
import os
import signal
import stat
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import backends as real_backends
from lib import db, takeover, turn_queue
from lib import turn_dispatch as td
from lib.backend.claude import prompt_uuid
from lib.backend.registry import by_id
from lib.turn_dispatch import TurnDispatchService

FAKE = r'''
import json, os, pathlib, select, sys, time
d = pathlib.Path(os.environ["STEER_FAKE_DIR"])
if "--version" in sys.argv:
    print((d / "version").read_text().strip() if (d / "version").exists() else "2.1.321 (Claude Code)")
    raise SystemExit(0)
n = int((d / "count").read_text()) + 1 if (d / "count").exists() else 1
(d / f"pid-{n}").write_text(str(os.getpid()))
args = sys.argv
sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
stdin = sys.stdin
first = json.loads(stdin.readline())
(d / f"call-{n}.json").write_text(json.dumps({"argv": args[1:], "first": first}))
(d / "count.tmp").write_text(str(n)); os.replace(d / "count.tmp", d / "count")
mode = (d / f"mode-{n}").read_text().strip() if (d / f"mode-{n}").exists() else "answer"
log = d / f"read-{n}.jsonl"
def out(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def echo(line):
    if "--replay-user-messages" in args:    # the real CLI echoes only when asked to
        out({"type": "user", "isReplay": True, "uuid": line["uuid"], "message": line["message"]})
def answer(text):
    out({"type": "assistant", "message": {"id": f"m-{n}-{text[:6]}", "role": "assistant",
         "content": [{"type": "text", "text": text}]}})
    out({"type": "result", "subtype": "success", "result": text})
def read_lines(until):
    lines = []
    eof = False
    while time.time() < until:
        if (d / f"release-{n}").exists():
            break
        if eof:
            time.sleep(0.02)
            continue
        ready, _, _ = select.select([stdin], [], [], 0.05)
        if not ready:
            continue
        raw = stdin.readline()
        if not raw:
            eof = True                  # stdin closed: the tool call still runs until released
            continue
        line = json.loads(raw)
        lines.append(line)
        with open(log, "a") as f:
            f.write(raw)
    return lines
def wait_eof(until):
    while time.time() < until:
        ready, _, _ = select.select([stdin], [], [], 0.05)
        if ready and not stdin.readline():
            return True
    return False
out({"type": "system", "subtype": "init", "session_id": sid})
if mode == "answer":
    answer(f"answer {n}")
    wait_eof(time.time() + 10)
    raise SystemExit(0)
def waiting_lines(within):
    """What sits on stdin now (the CLI reads its queue only at a boundary)."""
    lines = []
    while True:
        ready, _, _ = select.select([stdin], [], [], within)
        if not ready:
            return lines
        raw = stdin.readline()
        if not raw:
            return lines
        lines.append(json.loads(raw))
        with open(log, "a") as f:
            f.write(raw)
if mode == "writing":                   # the model only writes; recording 9 after it
    out({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text", "text": ""}}})
    while not (d / f"release-{n}").exists():
        time.sleep(0.02)
    answer(f"answer {n}")
    for line in waiting_lines(0.5):     # read after the result, answered in this process
        echo(line)
        answer("answer to the follow-up")
    wait_eof(time.time() + 10)
    raise SystemExit(0)
if mode == "between-tools":             # a short tool call, the model generating, the next one
    out({"type": "stream_event", "event": {"type": "content_block_start",
         "content_block": {"type": "tool_use", "id": f"ta{n}", "name": "Bash", "input": {}}}})
    out({"type": "user", "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": f"ta{n}", "content": "tick"}]}})
    while not (d / f"release-{n}").exists():
        time.sleep(0.02)                # generating: no tool call is open
    out({"type": "stream_event", "event": {"type": "content_block_start",
         "content_block": {"type": "tool_use", "id": f"tb{n}", "name": "Bash", "input": {}}}})
    out({"type": "user", "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": f"tb{n}", "content": "tick"}]}})
    follows = waiting_lines(0.5)        # read at the boundary after that tool call
    for line in follows:
        echo(line)
    answer(f"one answer to {1 + len(follows)} messages")
    wait_eof(time.time() + 10)
    raise SystemExit(0)
# a tool call opens
out({"type": "stream_event", "event": {"type": "content_block_start",
     "content_block": {"type": "tool_use", "id": f"tu{n}", "name": "Bash", "input": {}}}})
follows = read_lines(time.time() + 20)
out({"type": "user", "message": {"role": "user", "content": [
     {"type": "tool_result", "tool_use_id": f"tu{n}", "content": "tick"}]}})
if mode == "exit-unread":
    answer(f"answer {n}")
    raise SystemExit(0)                 # never reads (echoes) what was written
if mode == "after-result":              # recording case 9
    answer(f"answer {n}")
    for line in follows:
        echo(line)
    answer("answer to the follow-up")
else:                                   # recordings 2, 10, 11: joined
    for line in follows:
        echo(line)
    answer(f"one answer to {1 + len(follows)} messages")
wait_eof(time.time() + 10)
'''


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


@pytest.fixture
def world(fake_backend_cli, tmp_path, monkeypatch):
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "clarp"):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(FAKE))
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STEER_FAKE_DIR", str(fake_dir))
    fake_backend_cli.dir(tmp_path)
    takeover._cli_version.cache_clear()
    claude = by_id("claude")
    monkeypatch.setattr(claude, "resume_target", lambda *a, **k: tmp_path / "session.jsonl")
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    agent = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path),
                                   session="mike", backend="claude")
    agents_db.start_runtime(agent, "mike")
    scheduled = []
    service = TurnDispatchService(
        SimpleNamespace(default_session="mike", agents_path=tmp_path / "unused.json",
                        stream=_Stream()),
        backend_registry=real_backends, home=tmp_path,
        retry_scheduler=lambda _delay, fn: scheduled.append(fn))
    yield SimpleNamespace(dir=fake_dir, agent=agent, service=service, scheduled=scheduled)
    for pidfile in fake_dir.glob("pid-*"):
        try:
            os.killpg(int(pidfile.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError, PermissionError):
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
    td.reset_for_tests()


def _wait(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _send(w, cid):
    return w.service.dispatch(text=cid, requested_session="mike", trace_id=f"t-{cid}",
                              client_msg_id=cid, synthesize_audio=False, origin="user",
                              sender_agent_id="")


def _calls(w):
    return int((w.dir / "count").read_text()) if (w.dir / "count").exists() else 0


def _read(w, n):
    path = w.dir / f"read-{n}.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def _row(w, cid):
    return next((t for t in agents_db.list_messages(agent_id=w.agent, limit=100)
                 if t["id"] == f"u-{cid}"), None)


def _queued(w):
    return [i["client_msg_id"] for i in turn_queue.waiting_items(w.agent)]


def _first_turn(w, mode):
    (w.dir / "mode-1").write_text(mode)
    _send(w, "m1")
    assert _wait(lambda: _calls(w) == 1)
    assert _wait(lambda: any(getattr(getattr(h, "steer_state", None), "tools", None)
                             for h in real_backends.active_handles("claude", w.agent))
                 if mode not in ("writing",) else True)


def test_a_follow_up_during_a_tool_is_read_into_the_same_turn(world):
    w = world
    _first_turn(w, "joined")
    answer = _send(w, "m2")
    assert answer.queued is True                       # written, not read yet
    assert _wait(lambda: len(_read(w, 1)) == 1)
    line = _read(w, 1)[0]
    assert line["uuid"] == prompt_uuid("t-m2")
    assert [b["text"] for b in line["message"]["content"]][-1].endswith("m2")
    assert _row(w, "m2") is None and _queued(w) == ["m2"]     # in the queue until read
    assert turn_queue.update_text("m2", "other words") is False
    (w.dir / "release-1").write_text("1")
    assert _wait(lambda: _row(w, "m2") is not None)            # its row at the echo
    assert _wait(lambda: turn_queue.status("m2") != "queued")
    assert _queued(w) == []
    time.sleep(0.5)
    assert _calls(w) == 1                                      # never run as its own turn
    # The steering launch asks for the echo: without it nothing says a follow-up was read,
    # and the real CLI's process end would run it again (reviewer 4).
    argv = json.loads((w.dir / "call-1.json").read_text())["argv"]
    assert "--replay-user-messages" in argv and "stream-json" in argv


def test_two_follow_ups_during_one_tool_are_read_in_the_order_sent(world):
    w = world
    _first_turn(w, "joined")
    _send(w, "m2")
    _send(w, "m3")
    assert _wait(lambda: len(_read(w, 1)) == 2)
    assert [l["uuid"] for l in _read(w, 1)] == [prompt_uuid("t-m2"), prompt_uuid("t-m3")]
    (w.dir / "release-1").write_text("1")
    assert _wait(lambda: _row(w, "m2") is not None and _row(w, "m3") is not None)
    rows = [t["id"] for t in agents_db.list_messages(agent_id=w.agent, limit=100)
            if t["role"] == "user"]
    assert rows.index("u-m2") < rows.index("u-m3")
    time.sleep(0.5)
    assert _calls(w) == 1


def test_case_9_the_echo_after_the_result_is_a_race_handled_in_place(world):
    """Recording case 9, the known behaviour of the race (ruling 10:1x): the follow-up is read
    after the running turn's result, so the CLI answers it in a second turn of its own in the
    same process. It was written once; its row appears once, where it was read; nothing runs
    twice and nothing is lost; the CLI's answer counts inside the running turn until the process
    exits; and its receipt settles (the idle sweep that settles every steered receipt)."""
    w = world
    _first_turn(w, "after-result")
    _send(w, "m2")
    assert _wait(lambda: len(_read(w, 1)) == 1)                # written once
    (w.dir / "release-1").write_text("1")
    assert _wait(lambda: _row(w, "m2") is not None)
    assert _wait(lambda: not real_backends.active_handles("claude", w.agent))
    for fn in list(w.scheduled):
        fn()
    time.sleep(0.5)
    assert _calls(w) == 1                                      # the CLI answered it itself
    assert len(_read(w, 1)) == 1
    rows = [t for t in agents_db.list_messages(agent_id=w.agent, limit=100) if t["id"] == "u-m2"]
    assert len(rows) == 1                                      # once, never lost
    assert _queued(w) == [] and turn_queue.status("m2") == "started"
    turn_queue.terminalize_idle_agent_receipts(lambda _agent: False,
                                               now_ms=db.now_ms() + 120_000)
    assert turn_queue.status("m2") == "ended"                  # its receipt settles


def test_a_follow_up_the_process_never_read_runs_as_its_own_turn_once(world):
    w = world
    _first_turn(w, "exit-unread")
    (w.dir / "mode-2").write_text("answer")
    _send(w, "m2")
    assert _wait(lambda: len(_read(w, 1)) == 1)
    (w.dir / "release-1").write_text("1")
    assert _wait(lambda: not real_backends.active_handles("claude", w.agent))
    for fn in list(w.scheduled):
        fn()
    assert _wait(lambda: _calls(w) == 2)
    second = json.loads((w.dir / "call-2.json").read_text())["first"]
    assert [b["text"] for b in second["message"]["content"]][-1].endswith("m2")
    assert _wait(lambda: _row(w, "m2") is not None)
    for fn in list(w.scheduled):
        fn()
    time.sleep(0.5)
    assert _calls(w) == 2


def test_a_follow_up_between_tool_calls_is_read_into_the_same_turn(world):
    """The common case the old rule refused: the model is generating between two short tool
    calls (no tool call open). The follow-up is written now and read at the next boundary,
    inside the same turn; it never waits for the turn to end."""
    w = world
    (w.dir / "mode-1").write_text("between-tools")
    _send(w, "m1")
    assert _wait(lambda: any(getattr(h, "lines_read", 0) >= 3      # init, tool, its result
                             for h in real_backends.active_handles("claude", w.agent)))
    assert not any(h.steer_state.tools for h in real_backends.active_handles("claude", w.agent))
    answer = _send(w, "m2")
    assert answer.queued is True                               # written, read later
    assert _row(w, "m2") is None and _queued(w) == ["m2"]
    (w.dir / "release-1").write_text("1")
    assert _wait(lambda: _row(w, "m2") is not None)            # its row at the echo
    assert _wait(lambda: turn_queue.status("m2") != "queued")
    time.sleep(0.5)
    assert _calls(w) == 1                                      # one turn, one process
    assert [l["uuid"] for l in _read(w, 1)] == [prompt_uuid("t-m2")]


def test_a_follow_up_while_the_turn_only_writes_is_answered_in_the_same_process(world):
    """No tool call comes: the CLI reads the follow-up right after the turn's result and
    answers it in the same process (recording 9). Written at once, never held."""
    w = world
    _first_turn(w, "writing")
    time.sleep(0.3)
    answer = _send(w, "m2")
    assert answer.queued is True                               # written, read later
    (w.dir / "release-1").write_text("1")
    assert _wait(lambda: _row(w, "m2") is not None)
    assert _wait(lambda: turn_queue.status("m2") != "queued")
    time.sleep(0.5)
    assert _calls(w) == 1                                      # no second launch
    assert [l["uuid"] for l in _read(w, 1)] == [prompt_uuid("t-m2")]


def test_a_claude_older_than_the_recording_is_not_steered(world):
    w = world
    (w.dir / "version").write_text("2.1.300 (Claude Code)")
    takeover._cli_version.cache_clear()
    (w.dir / "mode-1").write_text("joined")
    _send(w, "m1")
    assert _wait(lambda: _calls(w) == 1)
    assert "--replay-user-messages" not in json.loads((w.dir / "call-1.json").read_text())["argv"]
    time.sleep(0.3)
    answer = _send(w, "m2")
    assert answer.queued is True                       # it waits (part A); nothing written
    time.sleep(0.3)
    assert _read(w, 1) == []
    (w.dir / "release-1").write_text("1")


@pytest.mark.parametrize("in_session", [True, False], ids=["read by the CLI", "never read"])
def test_a_runtime_that_dies_before_the_echo_settles_the_follow_up_at_its_start(
        world, monkeypatch, tmp_path, in_session):
    """Reviewer 4 on 7e351ba4 (the C5 class): from the stdin write until the CLI's echo the
    follow-up is 'queued', not shown, its receipt 'attempted'. A runtime that dies there left
    it to nobody. Now the next runtime's start looks its uuid up in the session file: there,
    its row is written and it is started, never run; not there, it runs as its own turn, once.
    Either way: exactly one read, never run twice, never left queued."""
    from lib import settings_store
    w = world
    _first_turn(w, "joined")
    (w.dir / "mode-2").write_text("answer")
    _send(w, "m2")
    assert _wait(lambda: len(_read(w, 1)) == 1)              # the CLI has it on its stdin
    assert settings_store.keys_with_prefix("steer.written.") == ["steer.written.t-m2"]
    # The runtime dies: its memory of the written follow-up is gone. The CLI outlives it,
    # still inside its tool call (reviewer 4 on 4e2cd479): it would read the line later.
    for handle in real_backends.active_handles("claude", w.agent):
        handle.steer_state.pending.clear()
    old_cli = int((w.dir / "pid-1").read_text())
    assert takeover.start_ticks(old_cli) is not None
    session = tmp_path / "session-file.jsonl"
    lines = [{"type": "user", "uuid": "an-earlier-entry", "message": {"content": "m1"}}]
    if in_session:
        lines.append({"type": "user", "uuid": prompt_uuid("t-m2"), "message": {"content": "m2"}})
    session.write_text("".join(json.dumps(line) + "\n" for line in lines))
    monkeypatch.setattr(by_id("claude"), "find_transcript", lambda *a, **k: session)
    td.reset_for_tests()                                      # the next runtime starts
    w.service.recover_queued()
    # The old group is ended before the lookup: it can no longer read the line.
    assert takeover.start_ticks(old_cli) is None or takeover._state(old_cli) == "Z"
    for _ in range(3):
        for fn in list(w.scheduled):
            fn()
        w.service.recover_queued()
        time.sleep(0.2)
    item = db.conn().execute("SELECT status, shown_at FROM queued_turns "
                             "WHERE client_msg_id = 'm2'").fetchone()
    if in_session:
        assert _calls(w) == 1                                 # never run
        assert item["status"] != "queued" and item["shown_at"] is not None
    else:
        assert _wait(lambda: _calls(w) == 2)                  # runs as its own turn
        second = json.loads((w.dir / "call-2.json").read_text())["first"]
        assert [b["text"] for b in second["message"]["content"]][-1].endswith("m2")
        assert _wait(lambda: _row(w, "m2") is not None)
        for fn in list(w.scheduled):
            fn()
        time.sleep(0.3)
        assert _calls(w) == 2                                 # once
    rows = [t for t in agents_db.list_messages(agent_id=w.agent, limit=100) if t["id"] == "u-m2"]
    assert len(rows) == 1 and "m2" not in _queued(w)          # exactly one read
    assert settings_store.keys_with_prefix("steer.written.") == []


def test_a_steered_cli_that_survives_its_kill_defers_the_judgement(world, monkeypatch, tmp_path):
    """Reviewer 4 on 6b0b0ac4: a group still there after the kill's bound could still read
    the line, so nothing is judged: the record stays, the item is untouched and not run. The
    next start, with the group gone, settles it."""
    from lib import settings_store
    w = world
    _first_turn(w, "joined")
    (w.dir / "mode-2").write_text("answer")
    _send(w, "m2")
    assert _wait(lambda: len(_read(w, 1)) == 1)
    for handle in real_backends.active_handles("claude", w.agent):
        handle.steer_state.pending.clear()                 # the runtime dies
    session = tmp_path / "session-file.jsonl"
    session.write_text(json.dumps({"type": "user", "uuid": prompt_uuid("t-m2")}) + "\n")
    monkeypatch.setattr(by_id("claude"), "find_transcript", lambda *a, **k: session)
    real_kill = takeover.kill_group_if_same
    monkeypatch.setattr(takeover, "kill_group_if_same", lambda *a, **k: None)
    td.reset_for_tests()
    w.service.recover_queued()
    item = db.conn().execute("SELECT status, shown_at FROM queued_turns "
                             "WHERE client_msg_id = 'm2'").fetchone()
    assert item["status"] == "queued" and item["shown_at"] is None and _row(w, "m2") is None
    assert settings_store.keys_with_prefix("steer.written.") == ["steer.written.t-m2"]
    assert _calls(w) == 1
    monkeypatch.setattr(takeover, "kill_group_if_same", real_kill)
    td.reset_for_tests()                                    # the next start
    w.service.recover_queued()
    assert _row(w, "m2") is not None and _calls(w) == 1     # read by the CLI: row, never run
    assert settings_store.keys_with_prefix("steer.written.") == []


def test_a_reused_pid_is_never_killed_and_the_recorded_group_is():
    """The steer record's group is ended at start only while its pid is still the process
    recorded (the same start ticks): a pid since reused by another process is left alone."""
    import subprocess
    other = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        ticks = takeover.start_ticks(other.pid)
        assert takeover.kill_group_if_same(other.pid, other.pid, ticks + 1) is False
        assert other.poll() is None                         # another process: untouched
        assert takeover.kill_group_if_same(other.pid, other.pid, None) is False
        assert other.poll() is None
        assert takeover.kill_group_if_same(other.pid, other.pid, ticks) is True
        assert other.wait(5) == -signal.SIGKILL
    finally:
        if other.poll() is None:
            other.kill()


def test_a_follow_up_bigger_than_the_pipe_never_blocks_the_drain(monkeypatch):
    """Reviewer 4 on 7e351ba4: the write to the CLI's stdin held the lock the drain takes for
    every line; a follow-up bigger than the pipe, while the CLI does not read its stdin, then
    blocked the drain, and the CLI blocked on its stdout. Now the write is the writer
    thread's, outside the lock: the steer returns, the drain goes on, the line arrives whole."""
    import threading
    from lib.backend.claude import SteerState, note_steer
    r, wfd = os.pipe()
    state = SteerState(os.fdopen(wfd, "w"))
    state.tools.add("tu1")
    handle = SimpleNamespace(steer_state=state)
    claude = by_id("claude")
    monkeypatch.setattr(claude, "active_handles", lambda agent_id: [handle])
    reads, done = [], {}
    big = "x" * 200_000

    def steer():
        done["ok"] = claude.steer("a", big, trace_id="t-big",
                                  on_read=reads.append, on_unread=lambda: reads.append("unread"))
    caller = threading.Thread(target=steer, daemon=True)
    caller.start()
    caller.join(1.0)
    assert done.get("ok") is True                             # returned, nothing read yet
    drain = threading.Thread(target=note_steer, daemon=True, args=(handle, {
        "type": "stream_event", "event": {"type": "content_block_delta"}}))
    drain.start()
    drain.join(1.0)
    assert not drain.is_alive()                               # the drain is never held
    time.sleep(0.3)                                           # the CLI reads its stdin at last
    line = json.loads(os.fdopen(r, "r").readline())
    assert line["uuid"] == prompt_uuid("t-big")
    assert line["message"]["content"][0]["text"] == big
    note_steer(handle, {"type": "user", "isReplay": True, "uuid": prompt_uuid("t-big")})
    assert reads == [True]
