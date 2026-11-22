"""Issue 360 R50 (issue 396): a heartbeat whose turn fails every time was retried
for ever, because the 11-hour window restarted at each new interrupted state.
It now counts from the first failure of the run; a completed turn ends the run."""
from __future__ import annotations

from lib import agents as agents_db
from lib import db, heartbeat
from lib.protocol import AgentState

from test_heartbeat import _agent, _heartbeat_test_env

HOUR = 3600
T0 = 10_000.0


def _state_at(aid, kind, at_sec, detail=None):
    agents_db.record_state(aid, kind, detail or {"reason": "runner_exit", "message": "exit"})
    db.conn().execute(
        "UPDATE state_log SET ts = ? WHERE state_id = (SELECT MAX(state_id) FROM state_log"
        " WHERE agent_id = ?)", (int(at_sec * 1000), aid))


def _failing_run(aid, starts):
    db.conn().execute("UPDATE state_log SET ts = ? WHERE agent_id = ? AND ts > ?",
                      (int((T0 - 2 * HOUR) * 1000), aid, int(T0 * 1000) + 12 * HOUR * 1000))
    for at in starts:                       # each failed heartbeat: it runs, then is cut short
        _state_at(aid, AgentState.THINKING, at - 60, {})
        _state_at(aid, AgentState.INTERRUPTED, at)


def test_a_run_of_failed_heartbeats_goes_dormant_11_hours_after_its_first_failure(monkeypatch):
    _heartbeat_test_env(monkeypatch, base=40, cap=160)
    aid = _agent("failing_run")
    _failing_run(aid, [T0, T0 + 4 * HOUR, T0 + 8 * HOUR, T0 + 10 * HOUR])
    now = T0 + 11 * HOUR + 10              # 1 h after the latest failure
    heartbeat.pending_heartbeat_agents(now=now)
    assert heartbeat._state_for(aid).dormant is True  # noqa: SLF001
    agent_row = agents_db.get_by_agent_id(aid)
    assert heartbeat.agent_schedule(agent_row, now=now)["next_heartbeat_at"] is None


def test_a_completed_turn_ends_the_run(monkeypatch):
    _heartbeat_test_env(monkeypatch, base=40, cap=160)
    aid = _agent("recovered_run")
    _failing_run(aid, [T0, T0 + 4 * HOUR])
    _state_at(aid, AgentState.DONE, T0 + 6 * HOUR, {})
    _failing_run(aid, [T0 + 8 * HOUR, T0 + 10 * HOUR])
    heartbeat.pending_heartbeat_agents(now=T0 + 11 * HOUR + 10)   # 3 h into the new run
    assert heartbeat._state_for(aid).dormant is False  # noqa: SLF001

