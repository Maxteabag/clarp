"""OpenCode runner and transcript tests."""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import stat
import sys

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib.protocol import AgentState
from lib import activity  # noqa: E402
from lib import agents as agents_db  # noqa: E402
from lib.backend import opencode  # noqa: E402
from lib.backend.registry import by_id  # noqa: E402
from lib import opencode_transcript  # noqa: E402
from lib import turn_lifecycle  # noqa: E402

OPENCODE = by_id("opencode")


@pytest.fixture(autouse=True)
def _run_path(monkeypatch):
    """These cover the ``opencode run`` path; test_opencode_serve covers live."""
    monkeypatch.setenv("CLARP_OPENCODE_LIVE", "0")


def test_build_cmd_fresh_and_resume():
    fresh = OPENCODE.build_cmd("", is_new_session=True,
                                      model="anthropic/claude-sonnet-4-5",
                                      effort="high")
    assert fresh[:4] == ["opencode", "run", "--format", "json"]
    assert "--auto" in fresh
    assert fresh[fresh.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
    assert fresh[fresh.index("--variant") + 1] == "high"
    resume = OPENCODE.build_cmd("ses_1")
    assert resume[resume.index("--session") + 1] == "ses_1"


def _install_fake_opencode(tmp_bin: pathlib.Path, stdout: str, *, rc: int = 0) -> None:
    fake = tmp_bin / "opencode"
    payload = json.dumps(stdout)
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "out = os.environ.get('OPENCODE_FAKE_ARGV_OUT')\n"
        "if out:\n"
        "    json.dump(sys.argv, open(out, 'w'))\n"
        f"sys.stdout.write({payload})\n"
        f"raise SystemExit({rc})\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


def test_spawn_turn_reads_json_events(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("OPENCODE_FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    events = [
        {"type": "session", "sessionID": "ses_abc"},
        {"type": "text", "part": {"text": "<speak>OpenCode here.</speak>"}},
    ]
    _install_fake_opencode(
        bin_dir, "".join(json.dumps(row) + "\n" for row in events))
    results: list[dict] = []
    sessions: list[str] = []
    handle = OPENCODE.start_turn(
        text="hello", cwd=tmp_path,
        on_session_init=lambda sid: sessions.append(sid) or True,
        on_result=results.append, enqueue=lambda **_k: 1,
    )
    handle.drain_thread.join(timeout=5)
    assert results
    assert "OpenCode here" in results[0]["last_agent_message"]
    assert sessions == ["ses_abc"]


def test_missing_opencode_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    try:
        OPENCODE.start_turn(text="hi", cwd=tmp_path)
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError as error:
        assert "opencode" in str(error)


def test_sqlite_transcript_roundtrip(tmp_path):
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE session (
          id text PRIMARY KEY,
          project_id text NOT NULL,
          directory text NOT NULL,
          title text NOT NULL,
          version text NOT NULL,
          time_created integer NOT NULL,
          time_updated integer NOT NULL,
          time_archived integer
        );
        CREATE TABLE message (
          id text PRIMARY KEY,
          session_id text NOT NULL,
          time_created integer NOT NULL,
          time_updated integer NOT NULL,
          data text NOT NULL
        );
        """
    )
    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
        ("ses_1", "p", "/tmp/proj", "Hello", "1", 1, 1_700_000_000, None),
    )
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("m1", "ses_1", 1, 1, json.dumps({"role": "user", "content": "hi there"})),
    )
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("m2", "ses_1", 2, 2, json.dumps({"role": "assistant", "content": "hello"})),
    )
    con.commit()
    con.close()
    listed = opencode_transcript.list_sessions("/tmp/proj", home=tmp_path)
    assert listed[0]["id"] == "ses_1"
    assert listed[0]["preview"] == "hi there"
    path = opencode_transcript.find_latest_jsonl("ses_1", home=tmp_path)
    turns = opencode_transcript.parse_turns(path)
    assert [row["role"] for row in turns] == ["user", "assistant"]


def _opencode_db(tmp_path, *, with_parts=True):
    """The schema OpenCode 1.2 writes: message.data is an envelope and the
    conversation itself is in part rows."""
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE session (id text PRIMARY KEY, project_id text, directory text,
          title text, version text, time_created integer, time_updated integer,
          time_archived integer);
        CREATE TABLE message (id text PRIMARY KEY, session_id text,
          time_created integer, time_updated integer, data text);
        """
        + ("""CREATE TABLE part (id text PRIMARY KEY, message_id text,
          session_id text, time_created integer, time_updated integer, data text);"""
           if with_parts else "")
    )
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
                ("ses_1", "p", "/tmp/proj", "Login", "1", 1, 1_789_544_308_627, None))
    messages = [
        ("msg_1", 1_789_544_308_627, {"role": "user", "agent": "build"}),
        ("msg_2", 1_789_544_309_000, {"role": "assistant", "modelID": "m"}),
        ("msg_3", 1_789_544_310_000, {"role": "user", "agent": "build"}),
        ("msg_4", 1_789_544_311_000, {"role": "assistant", "error": {
            "name": "APIError", "data": {"message": "Account is suspended"}}}),
    ]
    for message_id, created, data in messages:
        con.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                    (message_id, "ses_1", created, created, json.dumps(data)))
    parts = [
        ("prt_1", "msg_1", {"type": "text", "text": "log into my codex account"}),
        ("prt_2", "msg_2", {"type": "step-start"}),
        ("prt_3", "msg_2", {"type": "reasoning", "text": "private chain of thought"}),
        ("prt_4", "msg_2", {"type": "tool", "tool": "read", "callID": "c1",
                            "state": {"status": "completed", "input": {"filePath": "/a"}}}),
        ("prt_5", "msg_2", {"type": "tool", "tool": "bash", "callID": "c2",
                            "state": {"status": "error", "input": {"command": "x"}}}),
        ("prt_6", "msg_2", {"type": "text", "text": "Opened the login page."}),
        ("prt_7", "msg_2", {"type": "text", "text": "Waiting for the code."}),
        ("prt_8", "msg_2", {"type": "step-finish"}),
        ("prt_9", "msg_3", {"type": "text", "text": "injected context", "synthetic": True}),
        ("prt_a", "msg_3", {"type": "text", "text": "try again"}),
    ]
    if with_parts:
        for part_id, message_id, data in parts:
            con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                        (part_id, message_id, "ses_1", 1, 1, json.dumps(data)))
    con.commit()
    con.close()
    return db


def test_conversation_is_read_from_the_part_table(tmp_path):
    _opencode_db(tmp_path)
    path = opencode_transcript.find_latest_jsonl("ses_1", home=tmp_path)
    turns = opencode_transcript.parse_turns(path)
    assert [(t["role"], t["text"]) for t in turns] == [
        ("user", "log into my codex account"),
        ("assistant", "Opened the login page.\n\nWaiting for the code."),
        ("user", "try again"),
        ("assistant", "OpenCode APIError: Account is suspended"),
    ]
    assert [(tool["name"], tool["status"], tool["input"]) for tool in turns[1]["tools"]] == [
        ("Read", "ok", {"file_path": "/a"}), ("Bash", "error", {"command": "x"})]
    assert turns[1]["tools"][0]["file_path"] == "/a"
    assert turns[1]["tools"][0]["id"] == "c1"
    assert "chain of thought" not in json.dumps(turns)


def test_tool_only_steps_are_shown_with_the_reply_they_lead_to(tmp_path):
    db = _opencode_db(tmp_path)
    con = sqlite3.connect(db)
    # One reply spread over three step messages, then an interrupted one.
    for message_id, created in (("msg_5", 1_789_544_312_000), ("msg_6", 1_789_544_313_000),
                                ("msg_7", 1_789_544_314_000), ("msg_8", 1_789_544_315_000),
                                ("msg_9", 1_789_544_316_000)):
        role = "user" if message_id == "msg_8" else "assistant"
        con.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                    (message_id, "ses_1", created, created, json.dumps({"role": role})))
    for part_id, message_id, data in (
            ("prt_b", "msg_5", {"type": "tool", "tool": "grep", "state": {"input": {"pattern": "a"}}}),
            ("prt_c", "msg_6", {"type": "tool", "tool": "glob", "state": {"input": {"pattern": "b"}}}),
            ("prt_d", "msg_7", {"type": "text", "text": "Found it."}),
            ("prt_e", "msg_8", {"type": "text", "text": "and now?"}),
            ("prt_f", "msg_9", {"type": "tool", "tool": "bash", "state": {"input": {"command": "ls"}}})):
        con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                    (part_id, message_id, "ses_1", 1, 1, json.dumps(data)))
    con.commit()
    con.close()
    turns = opencode_transcript.parse_turns(f"{db}#ses_1")[4:]
    assert [(t["role"], t["text"], [tool["name"] for tool in t["tools"]]) for t in turns] == [
        ("assistant", "Found it.", ["Grep", "Glob"]),
        ("user", "and now?", []),
        ("assistant", "", ["Bash"]),
    ]


def test_timestamps_sort_with_iso_rows_from_other_sources(tmp_path):
    _opencode_db(tmp_path)
    turns = opencode_transcript.parse_turns(
        opencode_transcript.find_latest_jsonl("ses_1", home=tmp_path))
    assert turns[0]["timestamp"] == "2026-09-16T07:38:28.627Z"
    stamps = [t["timestamp"] for t in turns]
    assert stamps == sorted(stamps)
    assert "2026-09-16T07:38:00.000Z" < stamps[0] < "2026-09-16T07:39:00.000Z"


def test_session_list_previews_the_first_real_user_text(tmp_path):
    _opencode_db(tmp_path)
    listed = opencode_transcript.list_sessions("/tmp/proj", home=tmp_path)
    assert listed[0]["preview"] == "log into my codex account"
    assert listed[0]["mtime"] == 1_789_544_308


def test_database_without_a_part_table_still_parses(tmp_path):
    _opencode_db(tmp_path, with_parts=False)
    turns = opencode_transcript.parse_turns(
        opencode_transcript.find_latest_jsonl("ses_1", home=tmp_path))
    assert [t["text"] for t in turns] == ["OpenCode APIError: Account is suspended"]


class _Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return len(self.calls)


def _voice_agent(monkeypatch, *, focused="someone-else"):
    monkeypatch.setattr(agents_db,
                        "latest_turn_synthesize_audio", lambda _agent_id: True)
    monkeypatch.setattr(agents_db, "get_by_agent_id",
                        lambda _agent_id: {"persona": "Mike", "voice_id": "voice-1"})
    monkeypatch.setattr(agents_db, "get_focus", lambda: focused)
    monkeypatch.setattr(agents_db, "get_trace", lambda _agent_id: "trace-db")


def test_speak_enqueues_with_voice_identity(monkeypatch):
    _voice_agent(monkeypatch)
    enqueue = _Recorder()
    st = opencode._TurnState()
    text = "<speak>Pong.</speak>\n\npong"
    OPENCODE._speak(text, st, agent_id="a1", session="mike-1",
                           trace_id="trace-arg", enqueue=enqueue)
    # Same block again in a later event: spoken once.
    OPENCODE._speak(text, st, agent_id="a1", session="mike-1",
                           trace_id="trace-arg", enqueue=enqueue)
    assert len(enqueue.calls) == 1
    call = enqueue.calls[0]
    assert call["voice_id"] == "voice-1"
    assert call["source"] == "pwa"
    assert call["session"] == "mike-1"
    assert call["agent_id"] == "a1"
    assert call["trace_id"] == "trace-db"
    assert call["synthesize_audio"] is True
    assert call["text"] == "Mike here. Pong."


def test_speak_skips_unmarked_text_and_focused_prefix(monkeypatch):
    _voice_agent(monkeypatch, focused="a1")
    enqueue = _Recorder()
    st = opencode._TurnState()
    OPENCODE._speak("plain prose only", st, agent_id="a1",
                           session="s", trace_id="", enqueue=enqueue)
    assert enqueue.calls == []
    OPENCODE._speak("<speak>Hi.</speak>", st, agent_id="a1",
                           session="s", trace_id="", enqueue=enqueue)
    assert [c["text"] for c in enqueue.calls] == ["Hi."]


def test_speak_is_silent_without_voice_turn(monkeypatch):
    monkeypatch.setattr(agents_db,
                        "latest_turn_synthesize_audio", lambda _agent_id: False)
    enqueue = _Recorder()
    OPENCODE._speak("<speak>Hi.</speak>", opencode._TurnState(),
                           agent_id="a1", session="s", trace_id="", enqueue=enqueue)
    assert enqueue.calls == []


def test_broadcast_sends_one_event_dict():
    class Stream:
        def __init__(self):
            self.events = []

        def broadcast(self, event_dict):
            self.events.append(event_dict)

    stream = Stream()
    OPENCODE._broadcast(stream, "a1", "mike-1")
    assert stream.events == [{
        "type": "transcript-updated", "agent_id": "a1", "session": "mike-1",
    }]


def test_step_start_is_thinking_not_a_tool(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    events = [
        {"type": "step_start", "sessionID": "ses_x", "part": {"type": "step-start"}},
        {"type": "text", "sessionID": "ses_x", "part": {"type": "text", "text": "ok"}},
        {"type": "step_finish", "sessionID": "ses_x", "part": {"type": "step-finish"}},
    ]
    _install_fake_opencode(
        bin_dir, "".join(json.dumps(row) + "\n" for row in events))
    states: list[str] = []
    monkeypatch.setattr(OPENCODE, "_transition",
                        lambda _agent_id, event, _detail: states.append(
                            turn_lifecycle.target(event)))
    monkeypatch.setattr(agents_db, "get_by_agent_id", lambda _id: None)
    monkeypatch.setattr(agents_db,
                        "latest_turn_synthesize_audio", lambda _id: False)
    handle = OPENCODE.start_turn(
        text="hello", cwd=tmp_path, agent_id="a1", session="s",
        on_session_init=lambda _sid: True, enqueue=lambda **_k: 1,
    )
    handle.drain_thread.join(timeout=5)
    assert "thinking" in states
    assert "tool" not in states


def test_turn_reports_tool_detail_usage_last_step_text_and_denies_question(
        tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("OPENCODE_PERMISSION", json.dumps({"webfetch": "deny"}))
    step_finish = {"type": "step-finish", "reason": "tool-calls", "cost": 0.25,
                   "tokens": {"input": 10, "output": 4, "cache": {"read": 100, "write": 1}}}
    events = [
        {"type": "step_start", "sessionID": "ses_x", "part": {}},
        {"type": "text", "sessionID": "ses_x", "part": {"text": "Checking."}},
        {"type": "tool_use", "sessionID": "ses_x", "part": {
            "tool": "read", "state": {"status": "completed", "input": {"filePath": "/a.py"}}}},
        {"type": "tool_use", "sessionID": "ses_x", "part": {
            "tool": "todowrite", "state": {"status": "completed", "input": {"todos": [
                {"content": "Read the tests", "status": "completed", "priority": "high"},
                {"content": "Fix the parser", "status": "in_progress", "priority": "high"},
            ]}}}},
        {"type": "step_finish", "sessionID": "ses_x", "part": step_finish},
        {"type": "step_start", "sessionID": "ses_x", "part": {}},
        {"type": "text", "sessionID": "ses_x", "part": {"text": "All good."}},
        {"type": "step_finish", "sessionID": "ses_x", "part": {**step_finish, "reason": "stop"}},
    ]
    fake = bin_dir / "opencode"
    fake.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        f"open({str(tmp_path / 'perm')!r}, 'w').write(os.environ.get('OPENCODE_PERMISSION', ''))\n"
        f"sys.stdout.write({json.dumps(''.join(json.dumps(e) + chr(10) for e in events))})\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    details: list[dict] = []
    monkeypatch.setattr(OPENCODE, "_transition",
                        lambda _agent_id, _event, detail: details.append(detail))
    monkeypatch.setattr(agents_db, "get_by_agent_id", lambda _id: None)
    monkeypatch.setattr(agents_db, "latest_turn_synthesize_audio", lambda _id: False)
    results: list[dict] = []
    handle = OPENCODE.start_turn(
        text="hello", cwd=tmp_path, agent_id="a1", session="s",
        on_session_init=lambda _sid: True, on_result=results.append,
        enqueue=lambda **_k: 1)
    handle.drain_thread.join(timeout=5)
    assert json.loads((tmp_path / "perm").read_text()) == {
        "webfetch": "deny", "question": "deny"}
    tool = next(d for d in details if d.get("tool"))
    assert (tool["tool"], tool["input"]) == ("Read", {"file_path": "/a.py"})
    # The todo list is the plan's status line; it used to be dropped as a
    # non-scalar input, and every update read "0 items".
    plan = next(d for d in details if d.get("tool") == "TodoWrite")
    assert activity.summarize_tool_activity("TodoWrite", plan["input"])["summary"] == "Fix the parser"
    assert results == [{
        "last_agent_message": "All good.",
        "usage": {"input_tokens": 20, "output_tokens": 8,
                  "cache_read_input_tokens": 200, "cache_creation_input_tokens": 2},
        "total_cost_usd": 0.5,
    }]


def test_error_event_shows_the_provider_message(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    _install_fake_opencode(bin_dir, json.dumps({"type": "error", "error": {
        "name": "APIError", "data": {"message": "Account is suspended"}}}) + "\n")
    errors: list[str] = []
    handle = OPENCODE.start_turn(text="hi", cwd=tmp_path, on_error=errors.append,
                                 enqueue=lambda **_k: 1)
    handle.drain_thread.join(timeout=5)
    assert errors == ["Account is suspended"]


def test_run_path_asks_for_reasoning_events_and_treats_them_as_busy(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("OPENCODE_FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    events = [
        {"type": "tool_use", "sessionID": "ses_x", "part": {
            "tool": "bash", "state": {"status": "completed", "input": {"command": "ls"}}}},
        {"type": "reasoning", "sessionID": "ses_x", "part": {"text": "hidden thought"}},
        {"type": "text", "sessionID": "ses_x", "part": {"text": "Done."}},
    ]
    _install_fake_opencode(bin_dir, "".join(json.dumps(e) + "\n" for e in events))
    states: list[str] = []
    monkeypatch.setattr(OPENCODE, "_transition",
                        lambda _agent_id, event, _detail: states.append(event))
    monkeypatch.setattr(agents_db, "get_by_agent_id", lambda _id: None)
    monkeypatch.setattr(agents_db, "latest_turn_synthesize_audio", lambda _id: False)
    results: list[dict] = []
    handle = OPENCODE.start_turn(text="hi", cwd=tmp_path, agent_id="a1", session="s",
                                 on_session_init=lambda _sid: True,
                                 on_result=results.append, enqueue=lambda **_k: 1)
    handle.drain_thread.join(timeout=5)
    assert "--thinking" in json.loads((tmp_path / "argv.json").read_text())
    after_tool = states[states.index(turn_lifecycle.TurnEvent.TOOL_STARTED) + 1]
    assert turn_lifecycle.target(after_tool) == AgentState.THINKING
    assert results[0]["last_agent_message"] == "Done."


def test_text_is_responding_and_a_finished_tool_reports_its_call(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    events = [
        {"type": "step_start", "sessionID": "ses_x", "part": {"type": "step-start"}},
        {"type": "text", "sessionID": "ses_x", "part": {"type": "text", "text": "Looking."}},
        {"type": "tool_use", "sessionID": "ses_x", "part": {
            "id": "prt_1", "callID": "call_1", "tool": "read",
            "state": {"status": "completed", "input": {"filePath": "/a.py"}}}},
        {"type": "step_finish", "sessionID": "ses_x", "part": {"type": "step-finish"}},
    ]
    _install_fake_opencode(bin_dir, "".join(json.dumps(row) + "\n" for row in events))
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(OPENCODE, "_transition",
                        lambda _agent_id, event, detail: seen.append((event, detail)))
    monkeypatch.setattr(agents_db, "get_by_agent_id", lambda _id: None)
    monkeypatch.setattr(agents_db, "latest_turn_synthesize_audio", lambda _id: False)
    handle = OPENCODE.start_turn(
        text="hello", cwd=tmp_path, agent_id="a1", session="s",
        on_session_init=lambda _sid: True, enqueue=lambda **_k: 1)
    handle.drain_thread.join(timeout=5)
    phases = [d.get("phase") for e, d in seen if e == turn_lifecycle.TurnEvent.TEXT_STREAMED]
    assert "responding" in phases
    finished = [d for e, d in seen if e == turn_lifecycle.TurnEvent.TOOL_FINISHED]
    assert [(d["call_id"], d["status"]) for d in finished] == [("call_1", "ok")]
