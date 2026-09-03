"""OpenCode runner and transcript tests."""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import stat
import sys

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import opencode_runner  # noqa: E402
from lib import opencode_transcript  # noqa: E402


def test_build_cmd_fresh_and_resume():
    fresh = opencode_runner.build_cmd("", is_new_session=True,
                                      model="anthropic/claude-sonnet-4-5",
                                      effort="high")
    assert fresh[:4] == ["opencode", "run", "--format", "json"]
    assert "--auto" in fresh
    assert fresh[fresh.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
    assert fresh[fresh.index("--variant") + 1] == "high"
    resume = opencode_runner.build_cmd("ses_1")
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
    handle = opencode_runner.spawn_turn(
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
        opencode_runner.spawn_turn(text="hi", cwd=tmp_path)
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


class _Recorder:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return len(self.calls)


def _voice_agent(monkeypatch, *, focused="someone-else"):
    monkeypatch.setattr(opencode_runner.agents_db,
                        "latest_turn_synthesize_audio", lambda _agent_id: True)
    monkeypatch.setattr(opencode_runner.agents_db, "get_by_agent_id",
                        lambda _agent_id: {"persona": "Mike", "voice_id": "voice-1"})
    monkeypatch.setattr(opencode_runner.agents_db, "get_focus", lambda: focused)
    monkeypatch.setattr(opencode_runner.agents_db, "get_trace", lambda _agent_id: "trace-db")


def test_speak_enqueues_with_voice_identity(monkeypatch):
    _voice_agent(monkeypatch)
    enqueue = _Recorder()
    st = opencode_runner._TurnState()
    text = "<speak>Pong.</speak>\n\npong"
    opencode_runner._speak(text, st, agent_id="a1", session="mike-1",
                           trace_id="trace-arg", enqueue=enqueue)
    # Same block again in a later event: spoken once.
    opencode_runner._speak(text, st, agent_id="a1", session="mike-1",
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
    st = opencode_runner._TurnState()
    opencode_runner._speak("plain prose only", st, agent_id="a1",
                           session="s", trace_id="", enqueue=enqueue)
    assert enqueue.calls == []
    opencode_runner._speak("<speak>Hi.</speak>", st, agent_id="a1",
                           session="s", trace_id="", enqueue=enqueue)
    assert [c["text"] for c in enqueue.calls] == ["Hi."]


def test_speak_is_silent_without_voice_turn(monkeypatch):
    monkeypatch.setattr(opencode_runner.agents_db,
                        "latest_turn_synthesize_audio", lambda _agent_id: False)
    enqueue = _Recorder()
    opencode_runner._speak("<speak>Hi.</speak>", opencode_runner._TurnState(),
                           agent_id="a1", session="s", trace_id="", enqueue=enqueue)
    assert enqueue.calls == []


def test_broadcast_sends_one_event_dict():
    class Stream:
        def __init__(self):
            self.events = []

        def broadcast(self, event_dict):
            self.events.append(event_dict)

    stream = Stream()
    opencode_runner._broadcast(stream, "a1", "mike-1")
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
    monkeypatch.setattr(opencode_runner, "_record_state",
                        lambda _agent_id, kind, _detail: states.append(kind))
    monkeypatch.setattr(opencode_runner.agents_db, "get_by_agent_id", lambda _id: None)
    monkeypatch.setattr(opencode_runner.agents_db,
                        "latest_turn_synthesize_audio", lambda _id: False)
    handle = opencode_runner.spawn_turn(
        text="hello", cwd=tmp_path, agent_id="a1", session="s",
        on_session_init=lambda _sid: True, enqueue=lambda **_k: 1,
    )
    handle.drain_thread.join(timeout=5)
    assert "thinking" in states
    assert "tool" not in states
