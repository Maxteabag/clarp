"""Table-driven decisions for heartbeat._skip_reason over a gathered snapshot."""
import pytest

from lib import heartbeat
from lib.heartbeat import _AgentSnapshot, _HeartbeatState, _skip_reason
from lib.protocol import AgentState

NOW = 1_000_000.0
USER_STOP = {"kind": AgentState.INTERRUPTED, "ts": NOW * 1000, "detail": {"source": "user_stop"}}


def interrupted(age_sec):
    return {"kind": AgentState.INTERRUPTED, "ts": (NOW - age_sec) * 1000,
            "detail": {"source": "turn_error"}}


@pytest.mark.parametrize("snapshot,expected", [
    (_AgentSnapshot(latest={"kind": AgentState.WAITING}, busy=True), str(AgentState.WAITING)),
    (_AgentSnapshot(latest=USER_STOP, busy=True), str(AgentState.INTERRUPTED)),
    (_AgentSnapshot(busy=True, active=True), "busy"),
    (_AgentSnapshot(active=True, compacting=True), "active"),
    (_AgentSnapshot(compacting=True, recent_activity="recent-activity:5s"), "compacting"),
    (_AgentSnapshot(recent_activity="recent-activity:5s"), "recent-activity:5s"),
    (_AgentSnapshot(recent_activity="activity-unknown"), "activity-unknown"),
    (_AgentSnapshot(latest=interrupted(heartbeat.MAX_INTERRUPTED_RETRY_SEC)), "dormant"),
    (_AgentSnapshot(latest=interrupted(5)), "min-spacing"),
    (_AgentSnapshot(), ""),
])
def test_snapshot_ordering(snapshot, expected):
    assert _skip_reason(snapshot=snapshot, state=_HeartbeatState(), now=NOW) == expected


def test_schedule_state_decides_when_the_snapshot_is_idle():
    idle = _AgentSnapshot()
    assert _skip_reason(snapshot=idle, state=_HeartbeatState(last_started=NOW - 5), now=NOW) \
        == "min-spacing"
    interval = heartbeat._effective_interval_sec(_HeartbeatState(), is_interrupted=False)
    not_due = _HeartbeatState(last_started=NOW - max(heartbeat.MIN_WAKE_SPACING_SEC, interval - 1))
    if interval > heartbeat.MIN_WAKE_SPACING_SEC:
        assert _skip_reason(snapshot=idle, state=not_due, now=NOW) == "not-due"
    flooded = _HeartbeatState(recent_starts=[NOW - 1] * heartbeat.FLOOD_THRESHOLD)
    assert _skip_reason(snapshot=idle, state=flooded, now=NOW) == "flood"
    stale = _HeartbeatState(recent_starts=[NOW - heartbeat.FLOOD_WINDOW_SEC - 1] * 9)
    assert _skip_reason(snapshot=idle, state=stale, now=NOW) == ""
    assert _skip_reason(snapshot=idle, state=_HeartbeatState(dormant=True), now=NOW) == "dormant"


def test_decision_does_not_mutate_state():
    state = _HeartbeatState(recent_starts=[NOW - 999, NOW - 1])
    _skip_reason(snapshot=_AgentSnapshot(latest=interrupted(heartbeat.MAX_INTERRUPTED_RETRY_SEC)),
                 state=state, now=NOW)
    _skip_reason(snapshot=_AgentSnapshot(), state=state, now=NOW)
    assert state.dormant is False and state.recent_starts == [NOW - 999, NOW - 1]
