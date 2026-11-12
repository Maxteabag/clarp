"""A turn parked for an account is the agent's turn until its slot says otherwise.

elitebook, 2026-11-12. Every Claude account was empty from 07:31 to 08:40.
At 08:03:32 a voice message for an idle agent arrived while the pool was in
recovery, and was parked before it started. From 08:10 six more voice
messages for that agent arrived and queued behind it. POST /transcribe writes
the agent's trace marker to the new message's trace, also when the message
is only queued; the account coordinator took the moved marker to mean that
the parked turn had been replaced, and dropped it. Nothing had replaced it:
the slot still held it. The park's expiry at 08:32 released ten turns and the
recovery at 08:40 resumed six, neither this one. Its agent showed "Waiting
for a claude account with available usage" until someone stopped it at
11:52, and every message sent to it queued behind a turn nobody would start.
"""
from __future__ import annotations

import json

import pytest

from lib import agents as agents_db
from lib import db
from lib.account_failover import MAX_PARK_SECONDS, RECHECK_SECONDS
from lib.protocol import AgentState
import lib.turn_dispatch as _td
from tests.unit.test_account_plan_hostage import (  # noqa: F401
    BACKENDS, Fleet, USAGE_LIMIT, _dispatch, _local_dispatch_runtime)


def _spawns(fleet, trace):
    return [call for _, call in fleet.backends.spawned if call["trace_id"] == trace]


def _voice_message(fleet, session, trace, text):
    """What POST /transcribe does: the agent's trace marker moves to the new
    message's trace, then the message is dispatched."""
    agents_db.set_trace_for_session(session, trace)
    return fleet.service.dispatch(
        text=text, requested_session=session, forced_session=session, trace_id=trace,
        synthesize_audio=False)


def _the_morning(tmp_path, monkeypatch, backend):
    """No account has usage; a park expires and a second recovery begins; a
    voice message for an idle agent parks; more voice messages queue behind it."""
    fleet = Fleet(tmp_path, monkeypatch, backend, ["sonnet"], unserved=("sonnet",))
    start = fleet.clock[0]
    fleet.limit(0)
    fleet.run_until(start + MAX_PARK_SECONDS + RECHECK_SECONDS)       # 08:01: released
    assert len(fleet.spawns(0)) == 2 and not fleet.coordinator.recovering
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    assert fleet.kinds() == [AgentState.INTERRUPTED]

    _other, spawned = _dispatch(fleet, "worker", "sonnet")              # the next turn of the pool
    spawned[-1]["on_error"](USAGE_LIMIT)
    fleet.run_until(fleet.clock[0] + 5)
    assert fleet.coordinator.recovering                                # 08:02: a recovery again

    agent = agents_db.create_agent(persona="android", voice_id="V", cwd=str(tmp_path),
                                   session="android", backend=backend, model="sonnet")
    agents_db.start_runtime(agent, "android")
    _voice_message(fleet, "android", "voice-1", "the first voice message")   # 08:03:32
    assert _spawns(fleet, "voice-1") == []
    assert agents_db.latest_state(agent)["detail"]["account_recovery"] == "waiting"
    fleet.run_until(fleet.clock[0] + 400)
    for number in (2, 3, 4):                                           # 08:10 and after
        _voice_message(fleet, "android", f"voice-{number}", f"voice message {number}")
        fleet.run_until(fleet.clock[0] + 30)
    assert agents_db.get_trace(agent) == "voice-4"                     # the marker moved on
    assert _td._INFLIGHT.get(agent) == "voice-1"                       # the slot did not
    return fleet, agent


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_parked_turn_is_resumed_though_later_voice_messages_moved_the_trace_marker(
        tmp_path, monkeypatch, backend):
    fleet, agent = _the_morning(tmp_path, monkeypatch, backend)
    assert fleet.coordinator.attempts[agent].owned(), "the coordinator let go of a parked turn"

    fleet.no_account_offers()                                          # 08:40: accounts are back
    fleet.run_until(fleet.clock[0] + 2 * RECHECK_SECONDS)

    assert not fleet.coordinator.recovering
    started = _spawns(fleet, "voice-1")
    assert len(started) == 1, "the parked turn was never started"
    assert started[0]["text"].endswith("the first voice message")      # its own request, once
    state = agents_db.latest_state(agent)
    assert state["kind"] == AgentState.THINKING and "account_recovery" not in state["detail"]
    # It is the agent's live turn again, so what it reports is its own.
    assert agents_db.get_trace(agent) == "voice-1"
    assert started[0]["on_session_init"](started[0]["backend_session_id"] or "native-voice") is True
    started[0]["on_result"]({"duration_ms": 5})

    # And the messages queued behind it follow, in order.
    following = _spawns(fleet, "voice-2")
    assert len(following) == 1 and following[0]["text"].endswith("voice message 2")
    assert _td._INFLIGHT.get(agent) == "voice-2"
    assert [len(_spawns(fleet, f"voice-{number}")) for number in (3, 4)] == [0, 0]


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_parked_turn_whose_marker_moved_is_released_at_the_parks_expiry_like_the_rest(
        tmp_path, monkeypatch, backend):
    """No account comes back. Half an hour after the second recovery began
    the park expires, and this turn runs and fails visibly with the others
    instead of waiting for ever."""
    fleet, agent = _the_morning(tmp_path, monkeypatch, backend)

    fleet.run_until(fleet.clock[0] + MAX_PARK_SECONDS + RECHECK_SECONDS)

    started = _spawns(fleet, "voice-1")
    assert len(started) == 1
    started[0]["on_error"](USAGE_LIMIT)
    # It ended visibly, and the agent moved on to the message queued behind it.
    ended = [(row["kind"], json.loads(row["detail"]).get("reason")) for row in db.conn().execute(
        "SELECT kind, detail FROM state_log WHERE agent_id=? ORDER BY state_id", (agent,))]
    assert (AgentState.INTERRUPTED, "usage_limit") in ended
    assert _td._INFLIGHT.get(agent) == "voice-2" and len(_spawns(fleet, "voice-2")) == 1


def test_a_stop_still_takes_a_parked_turn_away(tmp_path, monkeypatch):
    """Ownership follows the slot: once a Stop has released it, the
    coordinator forgets the turn and never starts it."""
    fleet, agent = _the_morning(tmp_path, monkeypatch, "claude")
    attempt = fleet.coordinator.attempts[agent]

    assert _td._SLOTS.release(agent, "voice-1")
    assert not attempt.owned()

    fleet.no_account_offers()
    fleet.run_until(fleet.clock[0] + 2 * RECHECK_SECONDS)
    assert _spawns(fleet, "voice-1") == [] and agent not in fleet.coordinator.attempts
