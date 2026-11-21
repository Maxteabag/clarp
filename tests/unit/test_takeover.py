"""Part B of issue 491: a turn that has printed nothing is taken over by the user's next message.

Driven through the real dispatcher and the real Claude runner, with a stand-in `claude` on
PATH. Each call of the stand-in records its argv and stdin line, appends that line to a
session file (as Claude Code does: the line's uuid becomes the entry's uuid), starts a child
that stands in for an MCP server (it writes a heartbeat every 50 ms, in the same process
group), prints `system init`, then does what the test set for that call: `answer` (an
assistant message and a result), `speak` (one text block start, then wait), `silent`
(nothing, then wait). A waiting call ends when the test writes its `finish-N` file.

The interleavings of FREEZE, JUDGE, END tested here:
- nothing printed: the turn is frozen, judged, killed while frozen (its MCP child with it),
  and one turn takes over with the resume pair and both messages in one line;
- printed before the follow-up: never frozen for a takeover; the follow-up waits;
- printed in the pipe at the freeze: the drain reads it, the turn is thawed and runs on;
- the turn's process gone before the freeze: nothing to freeze, the follow-up waits;
- the drain not settling in its bound: thawed; the MCP child carries on;
- a Stop taking the slot while it is frozen: thawed, no takeover;
- a crash while frozen: the next runtime thaws the group from the durable record;
- a third message to the turn that took over: it waits (never taken over twice);
- an agent's message: never a takeover; a Claude older than the resume pair: none either.
"""
from __future__ import annotations

import json
import os
import pathlib
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
import json, os, pathlib, subprocess, sys, time
d = pathlib.Path(os.environ["TAKEOVER_FAKE_DIR"])
if "--version" in sys.argv:
    print((d / "version").read_text().strip() if (d / "version").exists() else "2.1.321 (Claude Code)")
    raise SystemExit(0)
n = int((d / "count").read_text()) + 1 if (d / "count").exists() else 1
line = sys.stdin.readline()
(d / f"call-{n}.json").write_text(json.dumps({"argv": sys.argv[1:], "stdin": line}))
(d / "count.tmp").write_text(str(n)); os.replace(d / "count.tmp", d / "count")
args = sys.argv
sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
msg = json.loads(line)
session = d / "session.jsonl"
with open(session, "a") as f:
    f.write(json.dumps({"type": "user", "uuid": msg["uuid"], "message": msg["message"]}) + "\n")
mode = (d / f"mode-{n}").read_text().strip() if (d / f"mode-{n}").exists() else "answer"
beat = d / f"mcp-{n}"
child = subprocess.Popen([sys.executable, "-c",
    "import time, pathlib, sys\np = pathlib.Path(sys.argv[1])\nwhile True:\n"
    "    p.write_text(str(time.time()))\n    time.sleep(0.05)\n", str(beat)])
(d / f"pid-{n}").write_text(f"{os.getpid()} {child.pid}")
def out(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def answer(text):
    out({"type": "assistant", "message": {"id": f"m-{n}", "role": "assistant",
         "content": [{"type": "text", "text": text}]}})
    with open(session, "a") as f:
        f.write(json.dumps({"type": "assistant", "uuid": f"a-{n}", "message": {
            "role": "assistant", "content": [{"type": "text", "text": text}]}}) + "\n")
    out({"type": "result", "subtype": "success", "result": text})
out({"type": "system", "subtype": "init", "session_id": sid})
if mode == "answer":
    answer(f"answer {n}")
    child.kill(); raise SystemExit(0)
if mode == "speak":
    out({"type": "stream_event", "event": {"type": "content_block_start",
         "content_block": {"type": "text", "text": ""}}})
deadline = time.time() + 60
while time.time() < deadline:
    if (d / f"speak-now-{n}").exists() and not (d / f"spoke-{n}").exists():
        (d / f"spoke-{n}").write_text("1")
        out({"type": "stream_event", "event": {"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": "Hel"}}})
    if (d / f"finish-{n}").exists():
        answer(f"answer {n}")
        child.kill(); raise SystemExit(0)
    time.sleep(0.02)
child.kill()
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
    monkeypatch.setenv("TAKEOVER_FAKE_DIR", str(fake_dir))
    fake_backend_cli.dir(tmp_path)
    takeover._cli_version.cache_clear()
    session_file = fake_dir / "session.jsonl"
    claude = by_id("claude")
    monkeypatch.setattr(claude, "find_transcript", lambda *a, **k: session_file)
    monkeypatch.setattr(claude, "resume_target", lambda *a, **k: session_file)
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
    w = SimpleNamespace(dir=fake_dir, agent=agent, service=service, session=session_file)
    yield w
    for pidfile in fake_dir.glob("pid-*"):
        for pid in pidfile.read_text().split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
    td.reset_for_tests()


def _mode(w, n, mode):
    (w.dir / f"mode-{n}").write_text(mode)


def _calls(w):
    return int((w.dir / "count").read_text()) if (w.dir / "count").exists() else 0


def _call(w, n):
    return json.loads((w.dir / f"call-{n}.json").read_text())


def _wait(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _send(w, cid, origin="user", sender=""):
    return w.service.dispatch(text=cid, requested_session="mike", trace_id=f"t-{cid}",
                              client_msg_id=cid, synthesize_audio=False, origin=origin,
                              sender_agent_id=sender)


def _pids(w, n):
    return [int(p) for p in (w.dir / f"pid-{n}").read_text().split()]


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def _beating(w, n, within=1.0):
    beat = w.dir / f"mcp-{n}"
    if not beat.exists():
        return False
    first = beat.read_text()
    return _wait(lambda: beat.read_text() != first, timeout=within)


def _outcome(trace):
    row = db.conn().execute("SELECT outcome FROM turns WHERE trace_id = ? ORDER BY turn_id DESC",
                            (trace,)).fetchone()
    return row["outcome"] if row else None


def _the_turn_before(w):
    """m0 runs and is answered, so the session has an entry before message 1."""
    _mode(w, 1, "answer")
    _send(w, "m0")
    assert _wait(lambda: _outcome("t-m0") == "completed"), _outcome("t-m0")


def _running(w, n, mode="silent"):
    _mode(w, n, mode)
    _send(w, f"m{n - 1}")
    assert _wait(lambda: (w.dir / f"pid-{n}").exists())
    assert _wait(lambda: agents_db.live_backend_session(w.agent))
    time.sleep(0.3)                                 # its init is read


def test_a_silent_turn_is_taken_over_with_both_messages_in_one_line(world):
    w = world
    _the_turn_before(w)
    _running(w, 2)                                  # m1, printing nothing
    cli, mcp = _pids(w, 2)
    _mode(w, 3, "answer")
    result = _send(w, "m2")                         # the follow-up
    assert result.queued is False                   # it started at once, as the takeover
    assert _wait(lambda: _calls(w) == 3)
    call = _call(w, 3)
    argv = call["argv"]
    assert argv[argv.index("--resume-drops-turn") + 1] == prompt_uuid("t-m1")
    assert argv[argv.index("--resume-session-at") + 1] == "a-1"        # m0's answer
    line = json.loads(call["stdin"])
    assert [block["text"] for block in line["message"]["content"]] == ["m1", "m2"]
    assert line["uuid"] == prompt_uuid("t-m2")
    assert not _alive(cli) and not _alive(mcp)     # killed while frozen, MCP child with it
    assert _wait(lambda: _outcome("t-m2") == "completed")
    assert _outcome("t-m1") == takeover.REPLACED
    rows = [(t["id"], t["text"]) for t in agents_db.list_messages(agent_id=w.agent, limit=50)
            if t["role"] == "user"]
    assert rows == [("u-m0", "m0"), ("u-m1", "m1"), ("u-m2", "m2")]
    assert not any("interrupt" in str(t.get("kind") or "") for t in
                   agents_db.list_messages(agent_id=w.agent, limit=50))
    assert settings_frozen() == []


def settings_frozen():
    from lib import settings_store
    return settings_store.keys_with_prefix("takeover.frozen.")


def test_a_turn_that_printed_is_never_cut(world):
    w = world
    _the_turn_before(w)
    _running(w, 2, mode="speak")
    assert _wait(lambda: any(getattr(h, "output_seen", False) for h in
                             real_backends.active_handles("claude", w.agent)))
    cli, _mcp = _pids(w, 2)
    result = _send(w, "m2")
    assert result.queued is True and _calls(w) == 2   # it waits (part A)
    assert _alive(cli) and _beating(w, 2)            # never frozen for good, never cut
    (w.dir / "finish-2").write_text("1")
    _mode(w, 3, "answer")
    assert _wait(lambda: _calls(w) == 3)
    assert "--resume-drops-turn" not in _call(w, 3)["argv"]
    assert _wait(lambda: _outcome("t-m1") == "completed")


def test_output_in_the_pipe_at_the_freeze_is_read_and_the_turn_runs_on(world, monkeypatch):
    w = world
    _the_turn_before(w)
    _running(w, 2)
    real_freeze = takeover.freeze

    def freeze_after_a_word(handle):
        (w.dir / "speak-now-2").write_text("1")      # it prints just before the freeze
        assert _wait(lambda: (w.dir / "spoke-2").exists())
        time.sleep(0.05)
        return real_freeze(handle)

    monkeypatch.setattr(takeover, "freeze", freeze_after_a_word)
    result = _send(w, "m2")
    assert result.queued is True and _calls(w) == 2
    cli, _mcp = _pids(w, 2)
    assert _alive(cli) and _beating(w, 2)            # thawed: it runs on
    assert settings_frozen() == []


def test_a_turn_gone_before_the_freeze_leaves_the_follow_up_to_wait(world, monkeypatch):
    w = world
    _the_turn_before(w)
    _running(w, 2)
    real_freeze = takeover.freeze

    def gone_first(handle):
        os.killpg(handle.process_group, signal.SIGKILL)
        handle.proc.wait(timeout=5)
        return real_freeze(handle)

    monkeypatch.setattr(takeover, "freeze", gone_first)
    _mode(w, 3, "answer")
    _send(w, "m2")
    assert _wait(lambda: _calls(w) == 3)             # it ran as its own turn
    assert "--resume-drops-turn" not in _call(w, 3)["argv"]
    assert settings_frozen() == []


def test_a_drain_that_does_not_settle_thaws_and_the_mcp_child_carries_on(world, monkeypatch):
    w = world
    _the_turn_before(w)
    _running(w, 2)
    seen = {}

    def never_settles(handle, **kwargs):
        cli, mcp = _pids(w, 2)
        seen["frozen"] = not _beating(w, 2, within=0.3)   # the MCP child is stopped too
        return False

    monkeypatch.setattr(takeover, "drained", never_settles)
    result = _send(w, "m2")
    assert seen["frozen"] is True
    assert result.queued is True and _calls(w) == 2
    assert _beating(w, 2)                                  # thawed: the child carries on


def test_a_stop_taking_the_slot_while_frozen_thaws_and_takes_nothing_over(world, monkeypatch):
    w = world
    _the_turn_before(w)
    _running(w, 2)
    real_drained = takeover.drained

    def stop_meanwhile(handle, **kwargs):
        td.begin_stop(w.agent, user_stop=True)            # the Stop takes the slot
        return real_drained(handle, **kwargs)

    monkeypatch.setattr(takeover, "drained", stop_meanwhile)
    _send(w, "m2")
    assert _calls(w) == 2                                  # no takeover launched
    assert _beating(w, 2)                                  # thawed (the Stop ends it its way)
    assert settings_frozen() == []


@pytest.mark.parametrize("raises_at", ["slot claim", "trace write"])
def test_a_raise_before_the_end_thaws_the_turn_and_gives_its_slot_back(world, monkeypatch,
                                                                       raises_at):
    """Reviewer 4 on df7af42c: once the plan is made, the slot claim or the session's trace
    write can raise (slots exhausted, a locked database) before the end. The frozen turn
    must run on (never left stopped), keep its slot, and its record go only once it runs;
    the follow-up waits."""
    import sqlite3
    from lib import turn_takeover
    from lib.turn_dispatch_state import _INFLIGHT
    w = world
    _the_turn_before(w)
    _running(w, 2)
    if raises_at == "slot claim":
        real_claim = turn_takeover._SLOTS.claim

        def claim(agent_id, trace_id):
            if trace_id == "t-m2":
                raise RuntimeError("slots exhausted")
            return real_claim(agent_id, trace_id)
        monkeypatch.setattr(turn_takeover._SLOTS, "claim", claim)
    else:
        real_trace = agents_db.set_trace_for_session

        def set_trace(session, trace_id):
            if trace_id == "t-m2":
                raise sqlite3.OperationalError("database is locked")
            return real_trace(session, trace_id)
        monkeypatch.setattr(agents_db, "set_trace_for_session", set_trace)
    result = _send(w, "m2")
    cli, _mcp = _pids(w, 2)
    assert result.queued is True and _calls(w) == 2         # nothing taken over: m2 waits
    assert _alive(cli) and not takeover.stopped(cli)         # runs on, never left in state T
    assert _beating(w, 2)                                    # its MCP child too
    assert _INFLIGHT.get(w.agent) == "t-m1"                  # the slot is the running turn's
    assert settings_frozen() == []                           # the record went after the thaw


def test_a_thaw_that_fails_keeps_the_record_for_the_next_runtime(world, monkeypatch):
    """If the group cannot be thawed, its record stays, so the next runtime thaws it."""
    from lib import turn_takeover
    w = world
    _the_turn_before(w)
    _running(w, 2)
    real_thaw = takeover.thaw

    def claim(agent_id, trace_id):
        raise RuntimeError("slots exhausted")

    def thaw(handle):
        raise PermissionError("not permitted")
    monkeypatch.setattr(turn_takeover._SLOTS, "claim", claim)
    monkeypatch.setattr(takeover, "thaw", thaw)
    _send(w, "m2")
    cli, _mcp = _pids(w, 2)
    assert takeover.stopped(cli) and len(settings_frozen()) == 1
    monkeypatch.setattr(takeover, "thaw", real_thaw)
    assert takeover.thaw_left_frozen() == 1                 # the next runtime's pass
    assert _wait(lambda: not takeover.stopped(cli)) and settings_frozen() == []
    assert _beating(w, 2)


def test_a_crash_while_frozen_is_thawed_by_the_next_runtime(world):
    w = world
    _the_turn_before(w)
    _running(w, 2)
    [handle] = real_backends.active_handles("claude", w.agent)
    takeover.record_freeze(agent_id=w.agent, trace_id="t-m1", pgid=handle.process_group,
                           pid=handle.pid)
    assert takeover.freeze(handle)
    assert not _beating(w, 2, within=0.3)                  # frozen, and the Host "dies" here
    td.reset_for_tests()
    w.service.recover_queued()                             # the next runtime's first recovery
    assert _beating(w, 2)
    assert settings_frozen() == []


def test_a_crash_between_the_record_and_the_stop_thaws_harmlessly(world):
    w = world
    _the_turn_before(w)
    _running(w, 2)
    [handle] = real_backends.active_handles("claude", w.agent)
    takeover.record_freeze(agent_id=w.agent, trace_id="t-m1", pgid=handle.process_group,
                           pid=handle.pid)
    assert takeover.thaw_left_frozen() == 1                # a SIGCONT to a running group
    assert _beating(w, 2) and settings_frozen() == []


def test_the_turn_that_took_over_is_never_taken_over_again(world):
    w = world
    _the_turn_before(w)
    _running(w, 2)
    _mode(w, 3, "silent")
    _send(w, "m2")
    assert _wait(lambda: _calls(w) == 3)
    assert "--resume-drops-turn" in _call(w, 3)["argv"]
    time.sleep(0.3)
    result = _send(w, "m3")                                # while the takeover prints nothing
    assert result.queued is True and _calls(w) == 3
    (w.dir / "finish-3").write_text("1")
    _mode(w, 4, "answer")
    assert _wait(lambda: _calls(w) == 4)
    assert "--resume-drops-turn" not in _call(w, 4)["argv"]


def test_an_agents_message_never_takes_over(world):
    w = world
    _the_turn_before(w)
    _running(w, 2)
    _send(w, "peer", origin="agent", sender="other-agent")
    time.sleep(0.5)
    assert _calls(w) == 2 and _beating(w, 2)


def test_a_claude_without_the_resume_pair_never_takes_over(world):
    w = world
    (w.dir / "version").write_text("2.1.100 (Claude Code)")
    takeover._cli_version.cache_clear()
    _the_turn_before(w)
    _running(w, 2)
    result = _send(w, "m2")
    assert result.queued is True and _calls(w) == 2 and _beating(w, 2)


def test_the_session_facts_follow_the_recorded_rules(tmp_path):
    path = tmp_path / "s.jsonl"
    m1 = prompt_uuid("t-m1")
    path.write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "uuid": "u0"}, {"type": "assistant", "uuid": "a0"},
        {"type": "user", "uuid": m1}]) + "\n{half a line")
    facts = takeover.session_facts(path, m1)
    assert facts == {"readable": True, "found": True, "resume_at": "a0", "answered": False}
    assert takeover.takeover_plan(facts, m1, "m1")["resume_at"] == "a0"
    path.write_text(json.dumps({"type": "user", "uuid": m1}) + "\n"
                    + json.dumps({"type": "assistant", "uuid": "a1"}) + "\n")
    assert takeover.takeover_plan(takeover.session_facts(path, m1), m1, "m1") is None  # answered
    path.write_text(json.dumps({"type": "user", "uuid": m1}) + "\n")      # a first turn
    assert takeover.takeover_plan(takeover.session_facts(path, m1), m1, "m1")["resume_at"] == ""
    path.write_text("")                                    # a first turn not yet in the file
    assert takeover.takeover_plan(takeover.session_facts(path, m1), m1, "m1") is None
    assert takeover.takeover_plan(takeover.session_facts(tmp_path / "none", m1), m1, "m1") is None
