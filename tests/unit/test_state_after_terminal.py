"""TP-N2: a non-terminal state row of a trace is never committed after a
terminal row of the same trace, from whichever process writes it.

Found by rf-harness in a G2 run: a resumed queued turn's spawn-side
'thinking' row was produced before the trace's 'done' but committed ~170 ms
after it, so the agent's last state was 'thinking' after the turn finished.
The race is made deterministic here: the late write is paused where it waits
for the writer lock, and another connection (another thread, as another
process would) commits the trace's 'done' meanwhile."""
import contextlib
import threading
import time

import pytest

from lib import agents as agents_db
from lib import db, turn_lifecycle
from lib.turn_lifecycle import TransitionNotWritten, TurnEvent


@pytest.fixture(autouse=True)
def _fresh_counters():
    turn_lifecycle.reset_for_tests()
    yield
    turn_lifecycle.reset_for_tests()


def _agent(tmp_path, session="nadia"):
    agent_id = agents_db.create_agent(persona=session.title(), voice_id="V", cwd=str(tmp_path),
                                      session=session, backend="claude")
    agents_db.start_runtime(agent_id, session)
    return agent_id


def _kinds(agent_id):
    return [row["kind"] for row in db.conn().execute(
        "SELECT kind FROM state_log WHERE agent_id = ? ORDER BY state_id", (agent_id,))]


def _started(agent_id, trace):
    agents_db.open_turn(agent_id=agent_id, source="local", trace_id=trace, synthesize_audio=False)
    turn_lifecycle.transition(agent_id, TurnEvent.SPAWN_STARTED, {"trace_id": trace})


def _commit_elsewhere_while_waiting(monkeypatch, write):
    """Run ``write`` on another connection the first time a state write
    waits for the writer lock, and let the paused write go on after it."""
    original, fired = turn_lifecycle._critical_write, []

    def critical_write():
        if not fired:
            fired.append(True)
            other = threading.Thread(target=write)
            other.start()
            other.join(10)
            assert not other.is_alive()
        return original()

    monkeypatch.setattr(turn_lifecycle, "_critical_write", critical_write)
    return fired


@pytest.mark.parametrize("event, detail, raises", [
    # The spawn-side start: refused like TP-N1's stopped start.
    (TurnEvent.SPAWN_STARTED, {"source": "resume", "backend_session_id": "native-1"}, True),
    # Backend activity (reader threads): dropped without raising.
    (TurnEvent.TOOL_STARTED, {"tool": "Bash"}, False),
])
def test_a_late_write_of_a_finished_trace_is_not_committed_after_its_end(tmp_path, monkeypatch,
                                                                       event, detail, raises):
    agent = _agent(tmp_path)
    _started(agent, "trace-1")
    fired = _commit_elsewhere_while_waiting(monkeypatch, lambda: turn_lifecycle.transition(
        agent, TurnEvent.PROCESS_EXITED_OK, {"trace_id": "trace-1"}))
    with (pytest.raises(TransitionNotWritten) if raises else contextlib.nullcontext()):
        result = turn_lifecycle.transition(agent, event, {"trace_id": "trace-1", **detail})
        assert result.state_id == 0, "a dropped late write reports no row"
    assert fired, "control: the trace's 'done' committed while the late write waited"
    assert _kinds(agent)[-1] == "done", "a late non-terminal row made the agent busy after its turn ended"
    assert turn_lifecycle.current_kind(agent) == "done"


def test_the_normal_order_is_unchanged(tmp_path):
    agent = _agent(tmp_path)
    _started(agent, "trace-1")
    turn_lifecycle.transition(agent, TurnEvent.SPAWN_STARTED,
                              {"trace_id": "trace-1", "source": "resume", "backend_session_id": "native-1"})
    turn_lifecycle.transition(agent, TurnEvent.TOOL_STARTED, {"trace_id": "trace-1", "tool": "Bash"})
    turn_lifecycle.transition(agent, TurnEvent.PROCESS_EXITED_OK, {"trace_id": "trace-1"})
    assert _kinds(agent)[-4:] == ["thinking", "thinking", "tool", "done"]


def test_a_new_attempt_on_the_same_trace_after_its_end_still_starts(tmp_path):
    """An account-failover retry keeps the trace: a write that begins after
    the terminal row is a new attempt, not a late one."""
    agent = _agent(tmp_path)
    _started(agent, "trace-1")
    turn_lifecycle.transition(agent, TurnEvent.PROCESS_EXITED_FAILED, {"trace_id": "trace-1"})
    # A real retry comes seconds later. In the same millisecond TP-N1's start
    # guard reads the failure as a Stop of the new turn (a clock tie is no
    # order), which is TP-N1's rule, not this fence.
    time.sleep(0.005)
    _started(agent, "trace-1")
    assert _kinds(agent)[-1] == "thinking"


def test_another_traces_terminal_row_does_not_fence(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    _started(agent, "trace-1")
    _commit_elsewhere_while_waiting(monkeypatch, lambda: turn_lifecycle.record(
        agent, "done", {"trace_id": "trace-other"}))
    turn_lifecycle.transition(agent, TurnEvent.TOOL_STARTED, {"trace_id": "trace-1", "tool": "Bash"})
    assert _kinds(agent)[-1] == "tool"
