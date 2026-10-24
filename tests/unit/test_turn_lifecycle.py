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
    # An ordinary turn end stays illegal from SPAWNED; only a call opening's unconfirmed end
    # leaves it, and that event never overwrites a turn, Stop or successor that moved on.
    (AgentState.SPAWNED, TurnEvent.PROCESS_EXITED_FAILED),
    (AgentState.THINKING, TurnEvent.CALL_OPENING_UNCONFIRMED),
    (AgentState.INTERRUPTED, TurnEvent.CALL_OPENING_UNCONFIRMED),
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
        agent_id, TurnEvent.TOOL_FINISHED).to_state == AgentState.THINKING


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
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"record_state", "record"}
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in {"agents", "agents_db", "turn_lifecycle"}):
                    callers.append(f"{path.relative_to(server)}:{func.name}")
    # The fixture wrapper itself delegates to turn_lifecycle.record.
    assert callers == ["lib/agents.py:record_state"], callers


def test_audit_note_repeats_the_current_state(tmp_path):
    aid = _agent(tmp_path, "audit")
    _put(aid, AgentState.DONE)
    result = turn_lifecycle.transition(aid, TurnEvent.AUDIT_NOTED, {"event": "handoff"})
    assert (result.from_state, result.to_state) == (AgentState.DONE, AgentState.DONE)
    _put(aid, NONE)
    with pytest.raises(IllegalTransition):
        turn_lifecycle.transition(aid, TurnEvent.AUDIT_NOTED)


# ---- the conditional transition (call-open end): written only while nothing newer happened ----
# expect_trace + anchor + s_open select it. Its conditions run inside one BEGIN IMMEDIATE before the
# INSERT; a refusal writes nothing, settles nothing and observes nothing.

import sqlite3


def _opening(tmp_path):
    """An agent whose live trace T owns an open turn, after its dispatcher's SPAWN_STARTED; s_open
    is the generation read before set_trace, as the dispatcher reads it."""
    agent_id = _agent(tmp_path)
    s_open = turn_lifecycle.state_generation(agent_id)
    agents_db.set_trace(agent_id, "T")
    turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="T")
    turn_lifecycle.transition(agent_id, TurnEvent.SPAWN_STARTED,
                              {"trace_id": "T", "source": "pwa", "dispatch": "claude"})
    return agent_id, s_open


def _end(agent_id, s_open, anchor, event=TurnEvent.PROCESS_EXITED_FAILED):
    return turn_lifecycle.transition(agent_id, event, {"trace_id": "T", "summary": "end"},
                                     expect_trace="T", anchor=anchor, s_open=s_open)


def _settled(agent_id, trace="T"):
    return db.conn().execute("SELECT outcome FROM turns WHERE agent_id = ? AND trace_id = ?",
                             (agent_id, trace)).fetchone()["outcome"]


@pytest.fixture
def observed(monkeypatch):
    from lib import live_hub
    calls = []
    monkeypatch.setattr(live_hub, "observe_transition", lambda *a, **k: calls.append(a))
    return calls


def test_conditional_end_writes_and_settles_its_turn_and_receipt(tmp_path, observed):
    from lib import turn_queue
    agent_id, s_open = _opening(tmp_path)
    turn_queue.enqueue(queue_id="q-T", agent_id=agent_id, session="mike", text="x", trace_id="T",
                       client_msg_id="q-T", synthesize_audio=False, origin="user",
                       sender_agent_id="")
    turn_queue.mark_started("q-T")
    baseline = len(observed)                                  # after setup, before the end
    moved = _end(agent_id, s_open, turn_lifecycle.state_generation(agent_id))
    assert moved.to_state == AgentState.INTERRUPTED
    assert turn_lifecycle.current_kind(agent_id) == AgentState.INTERRUPTED
    assert _settled(agent_id) == "failed"
    assert turn_queue.status("q-T") == "failed"
    [(seen_agent, event, kind, detail)] = observed[baseline:]  # exactly the accepted end
    assert (seen_agent, event, kind) == (agent_id, TurnEvent.PROCESS_EXITED_FAILED,
                                         AgentState.INTERRUPTED)
    assert detail["trace_id"] == "T" and detail["summary"] == "end"
    assert detail["_state_id"] == moved.state_id


@pytest.mark.parametrize("change,reason", [
    ("newer_row", "g_anchor"), ("other_trace", "p1_trace"), ("settled", "p2_settled"),
])
def test_conditional_end_refuses_and_writes_nothing(tmp_path, observed, change, reason):
    agent_id, s_open = _opening(tmp_path)
    anchor = turn_lifecycle.state_generation(agent_id)
    if change == "newer_row":
        turn_lifecycle.transition(agent_id, TurnEvent.TEXT_STREAMED,
                                  {"trace_id": "T", "phase": "responding"})
    elif change == "other_trace":
        agents_db.set_trace(agent_id, "T2")
    else:
        turn_lifecycle.transition(agent_id, TurnEvent.STOP_REQUESTED, {"trace_id": "T"})
        anchor = turn_lifecycle.state_generation(agent_id)
    before, settled, seen = _rows(agent_id), _settled(agent_id), len(observed)
    with pytest.raises(turn_lifecycle.TransitionNotWritten) as refused:
        _end(agent_id, s_open, anchor)
    assert refused.value.reason == reason
    assert _rows(agent_id) == before and _settled(agent_id) == settled
    assert len(observed) == seen


@pytest.mark.parametrize("source", ["pwa", "local", "agy_hook"])
def test_conditional_end_refuses_after_any_hook_prompt_admission(tmp_path, observed, source):
    agent_id, s_open = _opening(tmp_path)
    turn_lifecycle.hook_transition(agent_id, TurnEvent.PROMPT_ADMITTED,
                                   {"source": source, "backend_session_id": "b"})
    before = _rows(agent_id)
    with pytest.raises(turn_lifecycle.TransitionNotWritten) as refused:
        _end(agent_id, s_open, turn_lifecycle.state_generation(agent_id))
    assert refused.value.reason == "p4s_prompt_admitted" and _rows(agent_id) == before


def test_conditional_end_without_an_admission_mark_is_refused(tmp_path, observed):
    agent_id, _s_open = _opening(tmp_path)
    anchor, baseline = turn_lifecycle.state_generation(agent_id), len(observed)
    with pytest.raises(turn_lifecycle.TransitionNotWritten) as refused:
        _end(agent_id, None, anchor)
    assert refused.value.reason == "p4_no_admission_mark" and observed[baseline:] == []


@pytest.mark.parametrize("finish", ["COMMIT", "ROLLBACK"])
def test_conditional_end_inside_a_callers_transaction_issues_no_sql(tmp_path, observed, finish):
    """Refused before any statement (no BEGIN, no PRAGMA): the caller's work and transaction
    are untouched, whichever way the caller ends it."""
    agent_id, s_open = _opening(tmp_path)
    anchor, before = turn_lifecycle.state_generation(agent_id), _rows(agent_id)
    baseline = len(observed)
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    con.execute("INSERT INTO settings (key, value, updated_at) VALUES ('probe', '1', 1)")
    statements = []
    con.set_trace_callback(statements.append)
    try:
        with pytest.raises(turn_lifecycle.TransitionNotWritten) as refused:
            _end(agent_id, s_open, anchor)
    finally:
        con.set_trace_callback(None)
    assert refused.value.reason == "in_transaction"
    assert statements == [] and con.in_transaction
    con.execute(finish)
    probe = con.execute("SELECT value FROM settings WHERE key = 'probe'").fetchone()
    assert (probe is not None) == (finish == "COMMIT")
    assert _rows(agent_id) == before and observed[baseline:] == []


def test_conditional_end_is_observed_only_after_its_commit(tmp_path, monkeypatch):
    from lib import live_hub
    agent_id, s_open = _opening(tmp_path)
    seen = []

    def observe(observed_agent, event, kind, detail):
        other = sqlite3.connect(str(db.DB_PATH))           # another connection: committed rows only
        try:
            seen.append(other.execute("SELECT COUNT(*) FROM state_log WHERE state_id = ?",
                                      (detail["_state_id"],)).fetchone()[0])
        finally:
            other.close()
    monkeypatch.setattr(live_hub, "observe_transition", observe)
    _end(agent_id, s_open, turn_lifecycle.state_generation(agent_id))
    assert seen == [1]


def test_conditional_end_refuses_when_the_write_lock_stays_busy(tmp_path, observed, monkeypatch):
    agent_id, s_open = _opening(tmp_path)
    anchor, before = turn_lifecycle.state_generation(agent_id), _rows(agent_id)
    baseline = len(observed)
    monkeypatch.setattr(turn_lifecycle, "SQLITE_CRITICAL_BUSY_TIMEOUT_MS", 50)
    db.conn().execute("PRAGMA busy_timeout = 0")
    blocker = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(turn_lifecycle.TransitionNotWritten) as refused:
            _end(agent_id, s_open, anchor)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert refused.value.reason == "write_busy"
    assert _rows(agent_id) == before and observed[baseline:] == []


def test_conditional_end_whose_transaction_was_lost_is_not_written(tmp_path, observed, monkeypatch):
    """An inner error that auto-rolls back (SQLITE_FULL, IOERR) is swallowed by _settle_turn; the
    write is then reported lost, never observed."""
    agent_id, s_open = _opening(tmp_path)
    anchor, before = turn_lifecycle.state_generation(agent_id), _rows(agent_id)
    baseline = len(observed)
    monkeypatch.setattr(turn_lifecycle, "_settle_turn",
                        lambda *a, **k: db.conn().execute("ROLLBACK"))
    with pytest.raises(turn_lifecycle.TransitionNotWritten) as refused:
        _end(agent_id, s_open, anchor)
    assert refused.value.reason == "write_lost"
    assert _rows(agent_id) == before and observed[baseline:] == []


def test_conditional_end_checks_legality_inside_its_transaction(tmp_path, observed):
    agent_id, s_open = _opening(tmp_path)                    # THINKING: not SPAWNED
    anchor, before, baseline = turn_lifecycle.state_generation(agent_id), _rows(agent_id), len(observed)
    with pytest.raises(IllegalTransition) as refused:
        _end(agent_id, s_open, anchor, event=TurnEvent.CALL_OPENING_UNCONFIRMED)
    assert not isinstance(refused.value, turn_lifecycle.TransitionNotWritten)
    assert _rows(agent_id) == before and observed[baseline:] == []


def test_the_generation_read_seeks_the_per_agent_index(tmp_path):
    plan = " ".join(r[3] for r in db.conn().execute(
        "EXPLAIN QUERY PLAN SELECT MAX(state_id) FROM state_log WHERE agent_id = ?", ("a",)))
    assert "idx_state_log_agent_state" in plan, plan
