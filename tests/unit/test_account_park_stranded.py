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

from dataclasses import replace

from lib import agents as agents_db
from lib import db, janitors, turn_queue
from lib.account_failover import MAX_PARK_SECONDS, PLAN_CHECKS, RECHECK_SECONDS
from lib.protocol import AgentState
import lib.turn_dispatch as _td
from tests.patching import patch_shared
from tests.unit.test_account_plan_hostage import (  # noqa: F401
    BACKENDS, BAD, NOT_OFFERED, Fleet, USAGE_LIMIT, _dispatch, _local_dispatch_runtime,
    _visible_failure)


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


def _the_hosts_stop(fleet, agent):
    """A Host that runs its own turns (server.py's Stop)."""
    snapshot = _td.snapshot_stop_state(agent)
    _td.clear_for_agent(agent, preserve_queue=True, pause_queue=True)
    return snapshot


def _the_runtimes_stop(fleet, agent):
    """The runtime's side of the Host's Stop (runtime_rpc_server), to the end."""
    snapshot, _dropped, _was_paused = _td.begin_stop(agent, user_stop=True)
    _td.fence_user_stop(agent)
    _td.complete_stop(fleet.service.ctx, agent, snapshot, set(),
                      backend_registry=fleet.backends)
    return snapshot


@pytest.mark.parametrize("stop", [_the_hosts_stop, _the_runtimes_stop])
def test_a_stop_still_takes_a_parked_turn_away(tmp_path, monkeypatch, stop):
    """Ownership follows the slot: the real Stop takes it, and from then on
    the coordinator forgets the turn and never starts it. The messages queued
    behind it stay queued, paused, and none of them starts either."""
    fleet, agent = _the_morning(tmp_path, monkeypatch, "claude")
    attempt = fleet.coordinator.attempts[agent]
    assert attempt.owned()

    snapshot = stop(fleet, agent)

    # Stop knew the turn was parked, so a process that answers no signal is no failure.
    assert snapshot["trace_id"] == "voice-1" and snapshot["account_recovery_parked"]
    assert not attempt.owned() and _td._INFLIGHT.get(agent) != "voice-1"
    assert turn_queue.is_paused(agent)

    fleet.no_account_offers()
    fleet.run_until(fleet.clock[0] + 2 * RECHECK_SECONDS)
    assert agent not in fleet.coordinator.attempts
    assert [len(_spawns(fleet, f"voice-{number}")) for number in (1, 2, 3, 4)] == [0, 0, 0, 0]


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_parked_turn_is_resumed_though_the_speech_worker_moved_the_trace_marker(
        tmp_path, monkeypatch, backend):
    """The other writer that is not the turn's owner: the speech worker sets
    the marker to the trace of the clip it is about to speak, which is an
    earlier turn's when that reply is still being read out. Nothing is queued
    behind the parked turn here; it is resumed all the same and its process's
    callbacks are accepted, because the resume takes the marker back."""
    fleet = Fleet(tmp_path, monkeypatch, backend, ["sonnet"], unserved=("sonnet",))
    agent, start = fleet.agents[0], fleet.clock[0]
    fleet.limit(0)
    fleet.run_until(start + 120)
    assert fleet.waiting() == ["waiting"]

    agents_db.set_trace(agent, "the-turn-before")                      # tts_worker
    assert fleet.coordinator.attempts[agent].owned()

    fleet.no_account_offers()
    fleet.run_until(start + 300)
    assert len(fleet.spawns(0)) == 2 and fleet.recovery_messages(0) == 1
    assert fleet.kinds() == [AgentState.THINKING] and fleet.waiting() == [None]
    assert agents_db.get_trace(agent) == "trace-0"
    resumed = fleet.spawns(0)[-1]
    assert resumed["on_session_init"](resumed["backend_session_id"] or "native-0") is True
    resumed["on_result"]({"duration_ms": 5})
    assert fleet.kinds() == [AgentState.DONE]


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_turn_parked_for_its_plan_ends_with_the_providers_sentence_though_the_marker_moved(
        tmp_path, monkeypatch, backend):
    """The other way a parked turn ends without being resumed: its account
    does not offer its model and three answers find none that does. A voice
    message queued behind it has moved the marker by then. The turn ends as
    its own turn, under its own trace, and the voice message follows (read
    by the marker it never ended: the selector was not asked again and the
    agent showed the wait for four hours)."""
    fleet = Fleet(tmp_path, monkeypatch, backend, [BAD])
    agent = fleet.agents[0]
    fleet.spawns(0)[-1]["on_error"](NOT_OFFERED)
    fleet.run_scheduled()
    assert fleet.waiting() == ["waiting"]

    _voice_message(fleet, "agent-0", "voice-1", "a voice message behind it")
    assert agents_db.get_trace(agent) == "voice-1" and _td._INFLIGHT.get(agent) == "trace-0"
    seen = db.conn().execute("SELECT MAX(state_id) FROM state_log").fetchone()[0]
    marker_at_the_end = []
    mark = fleet.service._mark_interrupted
    monkeypatch.setattr(fleet.service, "_mark_interrupted", lambda spec, *a, **kw: (
        marker_at_the_end.append(agents_db.get_trace(agent)), mark(spec, *a, **kw))[1])

    fleet.run_scheduled(advance=RECHECK_SECONDS + 1)
    fleet.run_scheduled(advance=RECHECK_SECONDS + 1)

    assert fleet.selector_calls() == [[BAD]] * PLAN_CHECKS
    assert len(fleet.spawns(0)) == 1                                   # never resumed
    assert marker_at_the_end == ["trace-0"]                            # ended as its own turn
    ended = [dict(row) for row in db.conn().execute(
        "SELECT kind, detail FROM state_log WHERE agent_id=? AND state_id>? ORDER BY state_id",
        (agent, seen))]
    failure = next(row for row in ended if row["kind"] == AgentState.INTERRUPTED)
    _visible_failure({"kind": failure["kind"], "detail": json.loads(failure["detail"])},
                     category="account_plan", says="'gpt-6' model is not supported")
    # The message queued behind it is the agent's turn now.
    assert len(_spawns(fleet, "voice-1")) == 1 and _td._INFLIGHT.get(agent) == "voice-1"


def test_a_plan_end_that_arrives_after_a_stop_ends_nothing(tmp_path, monkeypatch):
    """A recovery pass chose to end the plan-parked turn, and a Stop took the
    turn before the pass got to it. The late end records nothing and leaves
    the marker where the Stop left it."""
    fleet = Fleet(tmp_path, monkeypatch, "claude", [BAD])
    agent = fleet.agents[0]
    fleet.spawns(0)[-1]["on_error"](NOT_OFFERED)
    fleet.run_scheduled()
    attempt = fleet.coordinator.attempts[agent]
    assert fleet.waiting() == ["waiting"] and attempt.fail is not None

    _the_hosts_stop(fleet, agent)
    agents_db.set_trace(agent, "what-came-next")
    states = db.conn().execute("SELECT COUNT(*) FROM state_log WHERE agent_id=?",
                               (agent,)).fetchone()[0]

    attempt.fail()

    assert db.conn().execute("SELECT COUNT(*) FROM state_log WHERE agent_id=?",
                             (agent,)).fetchone()[0] == states
    assert agents_db.get_trace(agent) == "what-came-next"


def _parked_spec(fleet):
    """The parked turn's spec, as the dispatcher holds it, as a maintenance run's."""
    spec = fleet.coordinator.attempts[fleet.agents[0]].work[0]
    assert (spec.agent_id, spec.trace_id) == (fleet.agents[0], "trace-0")
    return replace(spec, janitor_run_id="run-1")


def test_a_parked_maintenance_turn_is_the_agents_turn_only_while_its_run_is(
        tmp_path, monkeypatch):
    """A maintenance run that was cancelled or replaced while its turn waited
    for an account is not resumed, though the slot still holds its trace."""
    fleet = Fleet(tmp_path, monkeypatch, "claude", ["sonnet"], unserved=("sonnet",))
    fleet.limit(0)
    fleet.run_until(fleet.clock[0] + 120)
    assert fleet.waiting() == ["waiting"]
    spec = _parked_spec(fleet)
    asked = []

    def validate(session, run_id, trace_id, answer=[True]):
        asked.append((session, run_id, trace_id))
        if isinstance(answer[0], Exception):
            raise answer[0]
        return answer[0]

    monkeypatch.setattr(janitors, "validate_dispatch", validate)
    assert fleet.service._holds_slot(spec) is True
    assert asked == [("agent-0", "run-1", "trace-0")]

    validate.__defaults__[0][0] = False                                # the run is no longer it
    assert fleet.service._holds_slot(spec) is False
    assert fleet.service._holds_slot(replace(spec, janitor_run_id="")) is True   # an ordinary turn

    logged = []
    patch_shared(monkeypatch, _td, "log_exception", lambda event, exc, **kw: logged.append(event))
    validate.__defaults__[0][0] = RuntimeError("the store is locked")
    assert fleet.service._holds_slot(spec) is False                    # unprovable: not started
    assert logged == ["parkedTurnOwnerCheckFail"]
