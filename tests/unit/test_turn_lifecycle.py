"""TurnStateMachine: the transition table, refusals, force, and the writers."""
import pathlib

import pytest

from lib import agents as agents_db
from lib import db, reconcile, turn_lifecycle
from lib.protocol import AgentState
from lib.turn_lifecycle import (
    BUSY, NONE, TERMINAL, TRANSITIONS, IllegalTransition, TurnEvent,
)


@pytest.fixture(autouse=True)
def _fresh_counters():
    turn_lifecycle.reset_for_tests()
    yield
    turn_lifecycle.reset_for_tests()


def _agent(tmp_path, session="mike"):
    agent_id = agents_db.create_agent(
        persona=session.title(), voice_id="V", cwd=str(tmp_path),
        session=session, backend="claude")
    agents_db.start_runtime(agent_id, session)
    return agent_id


def _put(agent_id, kind):
    """Seed the current state through the compatibility edge."""
    if kind == NONE:
        db.conn().execute("DELETE FROM state_log WHERE agent_id = ?", (agent_id,))
    else:
        turn_lifecycle.record(agent_id, kind)
    assert turn_lifecycle.current_kind(agent_id) == kind


def _rows(agent_id):
    return db.conn().execute(
        "SELECT COUNT(*) AS n FROM state_log WHERE agent_id = ?",
        (agent_id,)).fetchone()["n"]


def test_busy_and_terminal_are_the_single_source():
    assert BUSY == frozenset(AgentState.busy_states())
    assert not BUSY & TERMINAL
    assert reconcile._PROCESS_BUSY_KINDS is BUSY
    assert TurnEvent.all() == frozenset(TRANSITIONS) | {TurnEvent.RECORDED, TurnEvent.AUDIT_NOTED}
    for target, origins in TRANSITIONS.values():
        assert AgentState.is_valid(target)
        assert origins <= turn_lifecycle.ALL


@pytest.mark.parametrize(
    "origin,event,target", sorted(turn_lifecycle.MACHINE.legal_edges()))
def test_every_legal_edge_writes_its_target(tmp_path, origin, event, target):
    agent_id = _agent(tmp_path)
    _put(agent_id, origin)
    result = turn_lifecycle.transition(agent_id, event, {"probe": event})
    assert (result.from_state, result.to_state) == (origin, target)
    latest = agents_db.latest_state(agent_id)
    assert latest["kind"] == target
    assert latest["detail"]["probe"] == event
    assert turn_lifecycle.illegal_counts() == {}


@pytest.mark.parametrize("origin,event", [
    (AgentState.DONE, TurnEvent.TOOL_STARTED),
    (AgentState.STOPPED, TurnEvent.COMPACTION_STARTED),
    (AgentState.SPAWNED, TurnEvent.HOOK_STOP),
    (AgentState.SPAWNED, TurnEvent.PROCESS_EXITED_OK),
    (NONE, TurnEvent.RETRY_SCHEDULED),
    (AgentState.IDLE, TurnEvent.RECONCILE_REPAIR),
    (AgentState.DONE, TurnEvent.RESTART_INTERRUPTED),
])
def test_illegal_transition_is_refused_logged_and_counted(tmp_path, origin, event):
    agent_id = _agent(tmp_path)
    _put(agent_id, origin)
    before = _rows(agent_id)
    with pytest.raises(IllegalTransition) as refused:
        turn_lifecycle.transition(agent_id, event)
    assert refused.value.from_state == origin
    assert _rows(agent_id) == before
    assert turn_lifecycle.current_kind(agent_id) == origin
    assert turn_lifecycle.illegal_counts() == {(origin or "none", event): 1}
    [recent] = turn_lifecycle.recent_refusals()
    assert (recent["agent_id"], recent["event"]) == (agent_id, event)


def test_try_transition_swallows_only_the_exception(tmp_path):
    agent_id = _agent(tmp_path)
    _put(agent_id, AgentState.DONE)
    assert turn_lifecycle.try_transition(agent_id, TurnEvent.TOOL_STARTED) is None
    assert turn_lifecycle.illegal_counts() == {
        (AgentState.DONE, TurnEvent.TOOL_STARTED): 1}


def test_force_is_the_documented_repair_path(tmp_path):
    agent_id = _agent(tmp_path)
    _put(agent_id, AgentState.IDLE)
    result = turn_lifecycle.transition(
        agent_id, TurnEvent.RECONCILE_REPAIR, {"reason": "reconcile"}, force=True)
    assert result.forced and result.to_state == AgentState.IDLE
    assert turn_lifecycle.illegal_counts() == {}


def test_unknown_event_and_invalid_recorded_kind_are_errors(tmp_path):
    agent_id = _agent(tmp_path)
    with pytest.raises(ValueError):
        turn_lifecycle.transition(agent_id, "made_up")
    with pytest.raises(ValueError):
        agents_db.record_state(agent_id, "not-a-state")


def test_record_state_wrapper_stays_legal_from_every_state(tmp_path):
    agent_id = _agent(tmp_path)
    _put(agent_id, AgentState.STOPPED)
    agents_db.record_state(agent_id, AgentState.TOOL, {"tool": "Bash"})
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.TOOL
    assert agents_db.is_busy(agent_id)


def test_hook_transition_never_raises(tmp_path):
    agent_id = _agent(tmp_path)
    _put(agent_id, AgentState.DONE)
    assert turn_lifecycle.hook_transition(agent_id, TurnEvent.TOOL_FINISHED) is None
    assert turn_lifecycle.hook_transition(
        agent_id, TurnEvent.PROMPT_ADMITTED).to_state == AgentState.THINKING
    assert turn_lifecycle.hook_transition(
        agent_id, TurnEvent.TOOL_FINISHED).to_state == AgentState.TOOL


def test_turn_rows_open_close_and_open_turns(tmp_path):
    agent_id = _agent(tmp_path)
    first = turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="t-1")
    second = turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="t-2")
    assert [row["trace_id"] for row in turn_lifecycle.open_turns()] == ["t-2"]
    turn_lifecycle.close_turn(second)
    assert [row["trace_id"] for row in turn_lifecycle.open_turns()] == ["t-1"]
    assert turn_lifecycle.close_turns_for_trace(agent_id, "t-1") == 1
    assert turn_lifecycle.open_turns() == []
    assert first != second
    with pytest.raises(ValueError):
        turn_lifecycle.open_turn(agent_id=agent_id, source="nope", trace_id="t")


def test_unlaunched_receipt_is_back_dated_before_newer_state(tmp_path):
    agent_id = _agent(tmp_path)
    agents_db.record_user_message(
        agent_id=agent_id, backend_session_id="b", text="hi",
        client_msg_id="c-1", origin="user", trace_id="t-old")
    turn_lifecycle.transition(agent_id, TurnEvent.SPAWN_STARTED, {"trace_id": "t-new"})
    turn_lifecycle.record_unlaunched(agent_id, "t-old")
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.THINKING


def test_no_server_module_writes_state_without_an_event():
    """Rule 4: production writers name an event; record_state is for fixtures."""
    import ast
    server = pathlib.Path(__file__).resolve().parents[2] / "server"
    callers = []
    for path in sorted(server.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"record_state", "record"}
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"agents", "agents_db", "turn_lifecycle"}):
                callers.append(f"{path.relative_to(server)}:{node.lineno}")
    # The fixture wrapper itself delegates to turn_lifecycle.record.
    assert callers == ["lib/agents.py:462"], callers


def test_audit_note_repeats_the_current_state(tmp_path):
    aid = _agent(tmp_path, "audit")
    _put(aid, AgentState.DONE)
    result = turn_lifecycle.transition(aid, TurnEvent.AUDIT_NOTED, {"event": "handoff"})
    assert (result.from_state, result.to_state) == (AgentState.DONE, AgentState.DONE)
    _put(aid, NONE)
    with pytest.raises(IllegalTransition):
        turn_lifecycle.transition(aid, TurnEvent.AUDIT_NOTED)
