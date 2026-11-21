"""Part B of issue 491 for agy: guarded by the one recorded trial (issue 491 rulings, 21:03).

The recording: an agy turn on a conversation with history, held and killed with zero stdout,
left the transcript with only the earlier history; the same conversation resumed with both
questions in one --print input answered both, each once. One trial, so the judge takes over
ONLY when message 1 is NOT in the conversation's transcript. It is judged while the turn is
still frozen, before any kill: message 1 already there, or no readable transcript, means the
turn is thawed and runs on, and the follow-up waits (part A). Both branches run here on a real
process group (a stand-in that heartbeats, as a turn's children would), through
turn_takeover.take_over_if_silent with the dispatcher's own slot state.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import db, takeover, turn_takeover
from lib import turn_dispatch as td
from lib.process_registry import TurnHandle
from lib.turn_command import _TurnSpec

CONV = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    """A running 'agy turn': a process group whose child writes a heartbeat."""
    beat = tmp_path / "beat"
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import time, pathlib, sys\np = pathlib.Path(sys.argv[1])\nwhile True:\n"
         "    p.write_text(str(time.time()))\n    time.sleep(0.05)\n", str(beat)],
        stdout=subprocess.PIPE, start_new_session=True, text=True)
    handle = TurnHandle(proc=proc, drain_thread=None, process_group=proc.pid)
    handle.takeover_capable = True
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    yield SimpleNamespace(proc=proc, handle=handle, beat=beat)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    td.reset_for_tests()


def _beating(beat, within=1.0):
    first = beat.read_text() if beat.exists() else ""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if beat.exists() and beat.read_text() != first:
            return True
        time.sleep(0.02)
    return False


def _scene(tmp_path, monkeypatch, stand_in, transcript_rows):
    agent = agents_db.create_agent(persona="Elli", voice_id="V", cwd=str(tmp_path),
                                   session="elli", backend="agy")
    agents_db.start_runtime(agent, "elli")
    agents_db.bind_backend_session(agent, CONV)
    from lib import message_store
    message_store.record_user_message(agent_id=agent, backend_session_id=CONV,
                                      client_msg_id="m1", text="What is the capital of France?",
                                      trace_id="t-m1")
    agents_db.open_turn(agent_id=agent, source="pwa", trace_id="t-m1")
    td._SLOTS.claim(agent, "t-m1")
    td._SLOTS.mark_spawned(agent, "t-m1")
    path = None
    if transcript_rows is not None:
        path = tmp_path / "transcript.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in transcript_rows) + "\n")
    from lib import agy_transcript
    monkeypatch.setattr(agy_transcript, "find_latest_jsonl", lambda conv, root=None: path)
    monkeypatch.setattr(takeover, "agy_version_ok", lambda exe: True)
    monkeypatch.setattr(takeover, "drained", lambda handle, **k: True)
    service = SimpleNamespace(
        home=tmp_path,
        backends=SimpleNamespace(active_handles=lambda backend, agent_id: [stand_in.handle]))
    spec = _TurnSpec(backend="agy", text="Also: 6 times 7?", cwd=tmp_path,
                     backend_session_id=CONV, is_new_session=False, session="elli",
                     agent_id=agent, trace_id="t-m2", context=None, synthesize_audio=False,
                     model="", effort="", client_msg_id="m2", origin="user",
                     queue_id="m2")
    return agent, service, spec


HISTORY = [
    {"type": "USER_INPUT", "content": "<USER_REQUEST>Remember the word lighthouse.</USER_REQUEST>"},
    {"type": "PLANNER_RESPONSE", "content": "OK"},
]


def test_message_one_not_in_the_transcript_is_taken_over_in_the_same_conversation(
        tmp_path, monkeypatch, stand_in):
    agent, service, spec = _scene(tmp_path, monkeypatch, stand_in, HISTORY)
    taken = turn_takeover.take_over_if_silent(service, spec)
    assert taken is not None
    assert taken.takeover == {"first": "What is the capital of France?"}
    assert taken.backend_session_id == CONV and taken.is_new_session is False
    assert stand_in.proc.wait(timeout=5) == -signal.SIGKILL          # killed while frozen
    assert not _beating(stand_in.beat, within=0.3)
    assert td._INFLIGHT[agent] == "t-m2"
    row = db.conn().execute("SELECT outcome FROM turns WHERE trace_id='t-m1'").fetchone()
    assert row["outcome"] == takeover.REPLACED


def test_message_one_already_in_the_transcript_thaws_and_the_follow_up_waits(
        tmp_path, monkeypatch, stand_in):
    rows = HISTORY + [{"type": "USER_INPUT",
                       "content": "<USER_REQUEST>What is the capital of France?</USER_REQUEST>"}]
    agent, service, spec = _scene(tmp_path, monkeypatch, stand_in, rows)
    assert turn_takeover.take_over_if_silent(service, spec) is None
    assert stand_in.proc.poll() is None and _beating(stand_in.beat)   # thawed, runs on
    assert td._INFLIGHT[agent] == "t-m1"


def test_no_readable_transcript_thaws_and_the_follow_up_waits(tmp_path, monkeypatch, stand_in):
    agent, service, spec = _scene(tmp_path, monkeypatch, stand_in, None)
    assert turn_takeover.take_over_if_silent(service, spec) is None
    assert stand_in.proc.poll() is None and _beating(stand_in.beat)
    assert td._INFLIGHT[agent] == "t-m1"


def test_the_takeover_gives_agy_both_messages_as_one_print_input(tmp_path, monkeypatch,
                                                                  fake_backend_cli):
    """The launch: --conversation <id> and one --print holding message 1 then message 2."""
    from lib.backend.registry import by_id
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_out = tmp_path / "argv.json"
    fake = bin_dir / "agy"
    fake.write_text(f"#!{sys.executable}\nimport sys, json\n"
                    f"json.dump(sys.argv[1:], open({str(argv_out)!r}, 'w'))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    fake_backend_cli.dir(tmp_path)
    agy = by_id("agy")
    handle = agy.start_turn(text="Also: 6 times 7?", cwd=tmp_path, backend_session_id=CONV,
                            is_new_session=False, trace_id="t-m2",
                            takeover={"first": "What is the capital of France?"})
    handle.proc.wait(timeout=10)
    argv = json.loads(argv_out.read_text())
    assert argv[argv.index("--conversation") + 1] == CONV
    [printed] = [a for a in argv if a.startswith("--print=")]
    body = printed[len("--print="):]
    assert body.index("What is the capital of France?") < body.index("Also: 6 times 7?")
    assert body.count("What is the capital of France?") == 1
