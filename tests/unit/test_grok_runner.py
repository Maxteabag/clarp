"""Grok Build headless runner tests.

Event fixtures mirror ``grok 1.0.30 --output-format streaming-json`` (one ACP
session update per line): ``thought``/``text`` deltas in ``data``,
``tool_call`` / ``tool_call_update`` for tools, ``usage`` and ``end``.
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
import sys
import time

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import agents as agents_db  # noqa: E402
from lib import grok_runner, runner_common  # noqa: E402
from lib import grok_transcript  # noqa: E402
from lib.protocol import AgentState, SSEType  # noqa: E402


SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CALL = "call-4285fff4-88be-46a9-a564-c3f94ae2456e-0"


def _stream_events() -> list[dict]:
    """A real-shaped turn: think, speak, run one command, speak again."""
    return [
        {"type": "available_commands", "tools": ["run_terminal_command"],
         "commands": ["compact"]},
        {"type": "thought", "data": "The user"},
        {"type": "thought", "data": " wants a listing."},
        {"type": "text", "data": "<speak>Hi from Grok.</speak> I'll"},
        {"type": "text", "data": " run it now."},
        {"type": "usage", "usage": {"input_tokens": 100, "output_tokens": 5}},
        {"type": "tool_call", "toolCallId": CALL, "title": "run_terminal_command",
         "kind": "execute", "status": "pending", "toolName": "run_terminal_command",
         "rawInput": {"command": "ls -la", "description": "List files"},
         "content": [], "locations": []},
        {"type": "tool_call_update", "toolCallId": CALL, "status": None,
         "content": [{"type": "content", "content": {"type": "text", "text": "List files"}}],
         "rawOutput": None, "locations": []},
        {"type": "tool_call_update", "toolCallId": CALL, "status": "in_progress",
         "content": [], "rawOutput": {"type": "Bash", "output": []}, "locations": []},
        {"type": "tool_call_update", "toolCallId": CALL, "status": "completed",
         "content": [{"type": "content", "content": {"type": "text", "text": "a.txt\n"}}],
         "rawOutput": {"type": "Bash", "exit_code": 0}, "locations": []},
        {"type": "text", "data": "The directory holds"},
        {"type": "text", "data": " one file."},
        {"type": "end", "stopReason": "end_turn", "sessionId": SID,
         "requestId": "req-1",
         "usage": {"input_tokens": 240, "output_tokens": 30}, "num_turns": 2},
    ]


class _Stream:
    """Same shape as lib.runtime_events / audio_stream: one event dict."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def broadcast(self, event: dict) -> None:
        assert isinstance(event, dict)
        self.events.append(event)


def _run_events(events, *, agent_id="agent-1", session="sess-1",
                trace_id="trace-1", stream=None, enqueue=None,
                on_error=None):
    st = grok_runner._TurnState(session_id=SID)
    for ev in events:
        grok_runner._handle_event(
            ev, st, agent_id=agent_id, session=session, trace_id=trace_id,
            on_error=on_error, stream=stream,
            enqueue=enqueue or (lambda **_k: 1))
    return st


def test_build_cmd_fresh_and_resume():
    fresh = grok_runner.build_cmd("sid-1", is_new_session=True, model="grok-4.6",
                                  effort="high")
    assert fresh[0] == "grok"
    assert fresh[fresh.index("--output-format") + 1] == "streaming-json"
    assert fresh[fresh.index("--session-id") + 1] == "sid-1"
    assert fresh[fresh.index("--model") + 1] == "grok-4.6"
    assert fresh[fresh.index("--reasoning-effort") + 1] == "high"
    resume = grok_runner.build_cmd("sid-1")
    assert resume[resume.index("--resume") + 1] == "sid-1"
    assert "--session-id" not in resume


def test_text_deltas_stream_into_live_row_and_speak(monkeypatch):
    live_writes: list[str] = []
    states: list[tuple[str, str]] = []
    spoken: list[str] = []

    def fake_upsert(*, agent_id, backend_session_id, trace_id, text):
        live_writes.append(text)
        return {"changed": True}

    monkeypatch.setattr(agents_db, "upsert_live_assistant_message", fake_upsert)
    monkeypatch.setattr(agents_db, "record_state",
                        lambda agent_id, kind, detail=None: states.append((kind, (detail or {}).get("tool", ""))))
    monkeypatch.setattr(agents_db, "latest_turn_synthesize_audio", lambda agent_id: True)
    monkeypatch.setattr(agents_db, "get_by_agent_id",
                        lambda agent_id: {"persona": "Margrok", "voice_id": "v1"})
    monkeypatch.setattr(agents_db, "get_focus", lambda: "agent-1")
    monkeypatch.setattr(agents_db, "get_trace", lambda agent_id: "")
    monkeypatch.setattr(grok_runner, "LIVE_TEXT_INTERVAL_SEC", 0.0)
    stream = _Stream()
    st = _run_events(
        _stream_events(), stream=stream,
        enqueue=lambda **kw: spoken.append(kw["text"]) or 1)

    # Text accumulates per model call; the tool call starts a new segment so
    # the durable assistant row imported from chat_history covers the first.
    assert live_writes[0] == "<speak>Hi from Grok.</speak> I'll"
    assert "<speak>Hi from Grok.</speak> I'll run it now." in live_writes
    assert live_writes[-1] == "The directory holds one file."
    assert st.last_agent_message == "The directory holds one file."
    assert st.turn_text.endswith("one file.")
    # Only the completed <speak> block is voiced, once, even though the
    # segment is re-scanned on every delta; untagged prose stays silent.
    assert spoken == ["Hi from Grok."]
    assert (st.tokens_in, st.tokens_out) == (240, 30)
    # Every SSE event is a single dict with a type, like the other runners.
    assert stream.events
    assert all(ev["type"] == SSEType.TRANSCRIPT_UPDATED for ev in stream.events)
    assert stream.events[0]["agent_id"] == "agent-1"
    assert stream.events[0]["session"] == "sess-1"
    assert (AgentState.TOOL, "run_terminal_command") in states
    assert states[0][0] == AgentState.THINKING


def test_tool_call_lifecycle_refreshes_pane(monkeypatch):
    monkeypatch.setattr(agents_db, "upsert_live_assistant_message",
                        lambda **_k: {"changed": False})
    states: list[str] = []
    monkeypatch.setattr(agents_db, "record_state",
                        lambda agent_id, kind, detail=None: states.append(kind))
    stream = _Stream()
    events = [ev for ev in _stream_events()
              if ev["type"] in {"tool_call", "tool_call_update"}]
    _run_events(events, stream=stream)
    # tool_call and the completed update each refresh the history pane;
    # the null / in_progress updates do not.
    assert len(stream.events) == 2
    assert states == [AgentState.TOOL, AgentState.THINKING]


def test_live_text_cadence_is_bounded(monkeypatch):
    live_writes: list[str] = []
    monkeypatch.setattr(agents_db, "upsert_live_assistant_message",
                        lambda **kw: live_writes.append(kw["text"]) or {"changed": True})
    monkeypatch.setattr(agents_db, "record_state", lambda *a, **k: None)
    monkeypatch.setattr(grok_runner, "LIVE_TEXT_INTERVAL_SEC", 60.0)
    st = _run_events([{"type": "text", "data": f"w{i} "} for i in range(20)])
    assert live_writes == ["w0 "]
    grok_runner._persist_live_text(
        st, agent_id="agent-1", session="sess-1", trace_id="trace-1",
        stream=None, force=True)
    assert live_writes[-1] == st.live_text


def test_error_event_fails_turn(monkeypatch):
    monkeypatch.setattr(agents_db, "record_state", lambda *a, **k: None)
    errors: list[str] = []
    st = _run_events([{"type": "error", "error": {"message": "rate limited"}}],
                     on_error=errors.append)
    assert st.failed_error == "rate limited"
    assert errors == ["rate limited"]


def test_broadcast_failure_is_logged_not_raised(monkeypatch):
    class Broken:
        def broadcast(self, event):
            raise RuntimeError("boom")

    logged: list[str] = []
    monkeypatch.setattr(runner_common, "log_exception",
                        lambda name, *a, **k: logged.append(name))
    grok_runner._broadcast(Broken(), "agent-1", "sess-1")
    assert logged == ["grokBroadcastFail"]


def test_routing_text_concatenates_real_stream():
    stdout = "".join(json.dumps(ev) + "\n" for ev in _stream_events())
    assert grok_runner.routing_text(stdout) == (
        "<speak>Hi from Grok.</speak> I'll run it now.The directory holds one file.")


def _install_fake_grok(tmp_bin: pathlib.Path, stdout: str, *, rc: int = 0) -> None:
    fake = tmp_bin / "grok"
    payload = json.dumps(stdout)
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "out = os.environ.get('GROK_FAKE_ARGV_OUT')\n"
        "if out:\n"
        "    json.dump(sys.argv, open(out, 'w'))\n"
        f"sys.stdout.write({payload})\n"
        f"raise SystemExit({rc})\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


def test_spawn_turn_binds_session_and_results(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("GROK_FAKE_ARGV_OUT", str(tmp_path / "argv.json"))
    _install_fake_grok(bin_dir, "".join(json.dumps(row) + "\n" for row in _stream_events()))
    results: list[dict] = []
    sessions: list[str] = []
    handle = grok_runner.spawn_turn(
        text="hello", cwd=tmp_path, backend_session_id=SID,
        on_session_init=lambda sid: sessions.append(sid) or True,
        on_result=results.append, enqueue=lambda **_k: 1,
    )
    handle.drain_thread.join(timeout=5)
    assert results
    assert results[0]["last_agent_message"] == "The directory holds one file."
    assert results[0]["usage"] == {"input_tokens": 240, "output_tokens": 30}
    assert sessions == [SID]
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[0].endswith("grok")
    assert argv[argv.index("--resume") + 1] == SID
    assert "-p" in argv


def test_missing_grok_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    try:
        grok_runner.spawn_turn(text="hi", cwd=tmp_path)
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError as error:
        assert "grok" in str(error)


def _chat_history_rows() -> list[dict]:
    """Rows as grok 1.0.30 writes them to chat_history.jsonl."""
    return [
        {"type": "system", "content": "You are Grok 4.6 released by xAI."},
        {"type": "user", "content": [{"type": "text", "text": "<user_info>\nOS Version: linux\n</user_info>"}]},
        {"type": "user", "content": [{"type": "text", "text": "<system-reminder>\nskills…\n</system-reminder>"}],
         "synthetic_reason": "system_reminder"},
        {"type": "user", "content": [{"type": "text", "text": "<user_query>\nplease fix it\n</user_query>"}],
         "prompt_index": 0},
        {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "thinking"}],
         "status": "completed"},
        {"type": "assistant", "content": "I'll look at the file.",
         "tool_calls": [{"id": CALL, "name": "run_terminal_command",
                         "arguments": json.dumps({"command": "cat a.txt",
                                                  "description": "Read a.txt"})}],
         "model_id": "grok-4.6-build"},
        {"type": "tool_result", "tool_call_id": CALL, "content": "exit: 0\nhello\n"},
        {"type": "assistant", "content": "",
         "tool_calls": [{"id": "call-2", "name": "search_replace",
                         "arguments": json.dumps({"file_path": "a.txt", "old_string": "hello",
                                                  "new_string": "bye", "replace_all": False})}]},
        {"type": "tool_result", "tool_call_id": "call-2", "content": "exit: 1\nno match"},
        {"type": "assistant", "content": "done", "model_id": "grok-4.6-build"},
        {"type": "user", "content": [{"type": "text", "text": "<system-reminder>\nBackground task finished\n</system-reminder>"}],
         "synthetic_reason": "system_reminder"},
    ]


def test_parse_turns_keeps_only_the_conversation(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in _chat_history_rows()))
    turns = grok_transcript.parse_turns(path)
    assert [t["role"] for t in turns] == ["user", "assistant", "assistant", "assistant"]
    assert turns[0]["text"] == "please fix it"
    bash = turns[1]["tools"][0]
    assert bash["name"] == "Bash"
    assert bash["id"] == CALL
    assert bash["command"] == "cat a.txt"
    assert bash["summary"] == "Read a.txt"
    assert bash["status"] == "ok"
    assert bash["result"] == "hello\n"
    edit = turns[2]["tools"][0]
    assert edit["name"] == "Edit"
    assert edit["file_path"] == "a.txt"
    assert (edit["old"], edit["new"]) == ("hello", "bye")
    assert edit["status"] == "error"
    assert edit["result"] == "no match"
    assert turns[3]["text"] == "done"


def test_parse_turns_unknown_tool_keeps_dict_input(tmp_path):
    path = tmp_path / "chat_history.jsonl"
    path.write_text(json.dumps({
        "type": "assistant", "content": "",
        "tool_calls": [{"id": "c", "name": "spawn_subagent",
                        "arguments": json.dumps({"prompt": "go", "background": True})}],
    }) + "\n")
    tool = grok_transcript.parse_turns(path)[0]["tools"][0]
    assert tool["name"] == "spawn_subagent"
    # Native decodes `input` as a string map; a JSON string here broke it.
    assert isinstance(tool["input"], dict)
    assert tool["input"]["prompt"] == "go"
    assert tool["status"] == "pending"


def test_parse_turns_and_list_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PWA_GROK_HOME", str(tmp_path))
    cwd = "/tmp/proj"
    encoded = grok_transcript._encode_cwd(cwd)
    session_dir = tmp_path / "sessions" / encoded / "sess-1"
    session_dir.mkdir(parents=True)
    (session_dir / "chat_history.jsonl").write_text(
        json.dumps({"type": "user", "content": "please fix it"}) + "\n"
        + json.dumps({"type": "assistant", "content": "done",
                      "tool_calls": [{"name": "Edit", "arguments": {}}]}) + "\n"
    )
    (session_dir / "summary.json").write_text(json.dumps({
        "generated_title": "Fix it",
    }))
    turns = grok_transcript.parse_turns(session_dir / "chat_history.jsonl")
    assert turns[0]["role"] == "user"
    assert turns[1]["role"] == "assistant"
    assert turns[1]["tools"][0]["name"] == "Edit"
    listed = grok_transcript.list_sessions(cwd, home=tmp_path)
    assert listed[0]["id"] == "sess-1"
    assert listed[0]["title"] == "Fix it"
    assert listed[0]["preview"] == "please fix it"
    assert grok_transcript.find_latest_jsonl("sess-1", home=tmp_path)
