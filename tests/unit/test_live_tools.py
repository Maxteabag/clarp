"""Tool items name what they do (docs/live-items.md §1.1 tool): a category
that drives Exploring/Explored grouping and a short label, from any
provider's tool name and input; and the turn's state transitions feed the
live hub for every backend."""
from __future__ import annotations

from lib import agents as agents_db
from lib import live_hub, turn_lifecycle
from lib.live_hub import LiveHub
from lib.live_tools import classify_tool
from lib.turn_lifecycle import TurnEvent


def test_tools_are_classified_and_labelled():
    assert classify_tool("Read", {"file_path": "/home/p/proj/src/parser.ts"})[:2] == ("read", "src/parser.ts")
    assert classify_tool("Grep", {"pattern": "tokenize", "path": "src"})[:2] == ("search", "tokenize")
    assert classify_tool("Glob", {"pattern": "**/*.py"})[:2] == ("search", "**/*.py")
    assert classify_tool("Bash", {"command": "rg tokenize src/lib"})[:2] == ("search", "tokenize in src/lib")
    assert classify_tool("Bash", {"command": "sed -n 1,40p src/a.py"})[:2] == ("read", "src/a.py")
    assert classify_tool("Bash", {"command": "ls src"})[:2] == ("list", "src")
    category, label, command = classify_tool("Bash", {"command": "npm test -- --watch=false"})
    assert (category, label, command) == ("exec", "npm test -- --watch=false", "npm test -- --watch=false")
    assert classify_tool("bash -lc 'cat README.md'", {})[:2] == ("read", "README.md")
    assert classify_tool("Edit", {"file_path": "/x/src/a.ts"})[:2] == ("edit", "src/a.ts")
    assert classify_tool("Write", {"file_path": "/x/b.md"})[:2] == ("write", "x/b.md")
    assert classify_tool("WebFetch", {"url": "https://example.com/a"})[:2] == ("fetch", "https://example.com/a")
    assert classify_tool("mcp__teams__send", {"text": "hi"})[0] == "mcp"
    assert classify_tool("TodoWrite", {"todos": []})[0] == "todo"


def test_state_transitions_feed_the_hub_for_any_backend():
    events = []
    hub = LiveHub(sink=events.append)
    live_hub.install(hub)
    try:
        agent_id = agents_db.create_agent(
            persona="Caleb", voice_id="v", cwd="/tmp", session="caleb", backend="codex")
        agents_db.start_runtime(agent_id, "caleb")
        agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="tr-9")
        turn_lifecycle.transition(agent_id, TurnEvent.SPAWN_STARTED, {"trace_id": "tr-9"})
        turn_lifecycle.transition(agent_id, TurnEvent.TOOL_STARTED, {
            "tool": "bash -lc 'rg parse src'", "call_id": "call_1", "status": "running"})
        snapshot = hub.snapshot(session="caleb")
        assert snapshot["turn"]["turn_id"] == "tr-9"
        [tool] = snapshot["items"]
        assert (tool["id"], tool["status"]) == ("cx:call_1", "running")
        assert (tool["tool"]["category"], tool["tool"]["group"]) == ("search", "explore:cx:call_1")
        assert snapshot["activity"]["state"] == "tool"
        turn_lifecycle.transition(agent_id, TurnEvent.TOOL_FINISHED, {
            "tool": "bash -lc 'rg parse src'", "call_id": "call_1", "status": "error"})
        assert hub.snapshot(session="caleb")["items"][0]["status"] == "failed"
        turn_lifecycle.transition(agent_id, TurnEvent.STOP_REQUESTED)
        snapshot = hub.snapshot(session="caleb")
        assert snapshot["turn"]["status"] == "interrupted"
        assert snapshot["activity"]["state"] == "interrupted"
    finally:
        live_hub.install(None)


def test_a_codex_turn_streams_reasoning_command_output_diffs_and_messages():
    import threading

    from lib import codex_app_server
    from lib.backend.codex import TurnState

    class _Handle:
        def __init__(self):
            self._done = threading.Event()

    hub = LiveHub(sink=lambda _e: None)
    live_hub.install(hub)
    try:
        agent_id = agents_db.create_agent(
            persona="Caleb", voice_id="v", cwd="/tmp", session="caleb", backend="codex")
        agents_db.start_runtime(agent_id, "caleb")
        agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="tr-1")
        turn_lifecycle.transition(agent_id, TurnEvent.SPAWN_STARTED, {"trace_id": "tr-1"})
        client = object.__new__(codex_app_server._Client)
        client.agent_id = agent_id
        client.active = codex_app_server._ActiveTurn(
            turn_id="turn-1", thread_id="thread-1", agent_id=agent_id, session="caleb",
            trace_id="tr-1", state=TurnState(live_backend_session_id="thread-1"),
            handle=_Handle(), on_result=None, on_error=None, stream=None,
            enqueue=lambda **_kwargs: 0)
        note = client._notification
        note("item/started", {"item": {"type": "reasoning", "id": "rs_1"}})
        note("item/reasoning/summaryTextDelta", {"itemId": "rs_1", "delta": "**Plan the fix**\n\nCheck"})
        note("item/reasoning/summaryTextDelta", {"itemId": "rs_1", "delta": " the tests."})
        note("item/completed", {"item": {"type": "reasoning", "id": "rs_1"}})
        note("item/started", {"item": {"type": "commandExecution", "id": "call_7",
                                       "command": "npm test", "status": "inProgress"}})
        note("item/commandExecution/outputDelta", {"itemId": "call_7", "delta": "line 1\nline "})
        note("item/commandExecution/outputDelta", {"itemId": "call_7", "delta": "2\nline 3\n"})
        note("item/completed", {"item": {"type": "commandExecution", "id": "call_7",
                                         "command": "npm test", "status": "failed", "exitCode": 1,
                                         "aggregatedOutput": "line 1\nline 2\nline 3\n"}})
        note("item/started", {"item": {"type": "fileChange", "id": "call_8", "status": "inProgress",
                                       "changes": [{"path": "src/a.ts", "kind": "update",
                                                    "diff": "@@ -1 +1,2 @@\n-a\n+b\n+c"}]}})
        note("item/completed", {"item": {"type": "fileChange", "id": "call_8", "status": "completed",
                                         "changes": [{"path": "src/a.ts", "kind": "update",
                                                      "diff": "@@ -1 +1,2 @@\n-a\n+b\n+c"}]}})
        note("item/agentMessage/delta", {"itemId": "msg_1", "delta": "Fixed."})
        note("item/completed", {"item": {"type": "agentMessage", "id": "msg_1", "text": "Fixed.",
                                         "phase": "final_answer"}})
        items = {item["id"]: item for item in hub.snapshot(session="caleb")["items"]}
        reasoning = items["cx:rs_1"]
        assert (reasoning["status"], reasoning["title"]) == ("completed", "Plan the fix")
        assert reasoning["text"].endswith("Check the tests.")
        command = items["cx:call_7"]
        assert command["status"] == "failed"
        assert command["tool"]["output"] == {"tail": ["line 1", "line 2", "line 3"], "total_lines": 3,
                                             "truncated": False, "exit_code": 1}
        change = items["cx:call_8"]
        assert change["tool"]["category"] == "edit"
        assert (change["tool"]["diff"]["added"], change["tool"]["diff"]["removed"]) == (2, 1)
        assert change["tool"]["diff"]["files"] == [{"path": "src/a.ts", "added": 2, "removed": 1}]
        message = items["cx:msg_1"]
        assert (message["text"], message["phase"], message["status"]) == ("Fixed.", "final", "completed")
    finally:
        live_hub.install(None)
