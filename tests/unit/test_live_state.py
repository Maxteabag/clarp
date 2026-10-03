"""The agent's live state says what is happening now (docs/live-items.md §1.3):
a finished tool goes back to thinking, streaming text is `responding`, Codex
tools finish and Codex compaction shows, and agent-activity names the tool
call with its start time."""
from __future__ import annotations

import threading

from lib import agents as agents_db
from lib import codex_app_server, turn_lifecycle
from lib.activity import state_activity_event
from lib.backend.codex import TurnState as _TurnState
from lib.protocol import AgentState
from lib.turn_lifecycle import TurnEvent


class _Handle:
    def __init__(self):
        self._done = threading.Event()


def _agent(session="nova", backend="codex"):
    agent_id = agents_db.create_agent(
        persona=session.title(), voice_id="v", cwd="/tmp", session=session,
        backend=backend)
    agents_db.start_runtime(agent_id, session)
    return agent_id


def _codex_client(agent_id, session="nova"):
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-1")
    turn_lifecycle.transition(agent_id, TurnEvent.SPAWN_STARTED)
    client = object.__new__(codex_app_server._Client)
    client.agent_id = agent_id
    client.active = codex_app_server._ActiveTurn(
        turn_id="turn-1", thread_id="thread-1", agent_id=agent_id,
        session=session, trace_id="trace-1",
        state=_TurnState(live_backend_session_id="thread-1"),
        handle=_Handle(), on_result=None, on_error=None, stream=None,
        enqueue=lambda **_kwargs: 0)
    return client


def _activity(agent_id):
    latest = agents_db.latest_state(agent_id)
    return state_activity_event(
        agent_id=agent_id, session="nova", persona="Nova",
        kind=latest["kind"], ts=latest["ts"], detail=latest["detail"])


def _state_rows(agent_id):
    return agents_db.conn().execute(
        "SELECT COUNT(*) AS n FROM state_log WHERE agent_id = ?",
        (agent_id,)).fetchone()["n"]


def test_a_finished_tool_returns_the_agent_to_thinking():
    agent_id = _agent(backend="claude")
    turn_lifecycle.transition(agent_id, TurnEvent.PROMPT_ADMITTED)
    turn_lifecycle.transition(agent_id, TurnEvent.TOOL_STARTED, {
        "phase": "tool_started", "tool": "Bash", "call_id": "toolu_1",
        "input": {"command": "ls"}, "status": "running"})
    assert _activity(agent_id)["state"] == "tool"
    turn_lifecycle.transition(agent_id, TurnEvent.TOOL_FINISHED, {
        "phase": "tool_finished", "tool": "Bash", "call_id": "toolu_1",
        "input": {"command": "ls"}, "status": "ok"})
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.THINKING
    activity = _activity(agent_id)
    assert activity["state"] == "thinking"
    assert activity["kind"] == AgentState.THINKING
    # Older clients still see which tool settled and how.
    assert (activity["tool"], activity["call_id"], activity["status"]) == ("Bash", "toolu_1", "ok")


def test_agent_activity_names_the_tool_call_and_when_it_started():
    agent_id = _agent(backend="claude")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="t-9")
    turn_lifecycle.transition(agent_id, TurnEvent.PROMPT_ADMITTED)
    started = turn_lifecycle.transition(agent_id, TurnEvent.TOOL_STARTED, {
        "phase": "tool_started", "tool": "Read", "call_id": "toolu_7",
        "input": {"file_path": "/x/src/a.py"}, "status": "running"})
    activity = _activity(agent_id)
    assert activity["state"] == "tool"
    assert activity["call_id"] == "toolu_7"
    assert activity["started_at_ms"] == started.ts
    turn = agents_db.conn().execute(
        "SELECT started_at FROM turns WHERE agent_id = ?", (agent_id,)).fetchone()
    assert activity["turn_started_ms"] == turn["started_at"]


def test_streaming_text_is_responding_and_repeated_deltas_write_one_row():
    agent_id = _agent()
    client = _codex_client(agent_id)
    client._notification("item/agentMessage/delta", {"delta": "Hel"})
    rows = _state_rows(agent_id)
    client._notification("item/agentMessage/delta", {"delta": "lo"})
    client._notification("item/agentMessage/delta", {"delta": " there"})
    assert _state_rows(agent_id) == rows
    activity = _activity(agent_id)
    assert activity["state"] == "responding"
    # agent-state.kind keeps an old client busy.
    assert activity["kind"] == AgentState.THINKING


def test_codex_reasoning_is_thinking_not_responding():
    agent_id = _agent()
    client = _codex_client(agent_id)
    client._notification("item/agentMessage/delta", {"delta": "Hi"})
    client._notification("item/started", {"item": {"type": "reasoning", "id": "rs_1"}})
    assert _activity(agent_id)["state"] == "thinking"


def test_codex_command_finishes_and_the_agent_thinks_again():
    agent_id = _agent()
    client = _codex_client(agent_id)
    client._notification("item/started", {"item": {
        "type": "commandExecution", "id": "call_9", "command": "npm test", "status": "inProgress"}})
    activity = _activity(agent_id)
    assert (activity["state"], activity["tool"], activity["call_id"]) == ("tool", "npm test", "call_9")
    client._notification("item/completed", {"item": {
        "type": "commandExecution", "id": "call_9", "command": "npm test",
        "status": "failed", "exitCode": 1}})
    activity = _activity(agent_id)
    assert activity["state"] == "thinking"
    assert (activity["call_id"], activity["status"]) == ("call_9", "error")


def test_codex_compaction_shows_while_it_runs():
    agent_id = _agent()
    client = _codex_client(agent_id)
    client._notification("item/started", {"item": {"type": "contextCompaction", "id": "cc_1"}})
    assert _activity(agent_id)["state"] == "compacting"
    client._notification("item/completed", {"item": {"type": "contextCompaction", "id": "cc_1"}})
    assert _activity(agent_id)["state"] == "thinking"
