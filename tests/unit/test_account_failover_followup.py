"""From the second review of PR 269: three things the park for a model left open.

1. When the model was back, turns for it kept waiting half an hour each: a
   new one parks behind those already parked, and nothing noticed that a turn
   released at its cap then ran fine. Measured by the reviewer: Fable's quota
   back at minute 0, Sonnet never idle, a Fable turn from another agent every
   25 minutes; all six waited the full 30 minutes.
2. A hand-over in the minute after the last working turn finished carried
   nothing, so the next runtime started a parked turn that had never run on
   the active account without asking the selector.
3. A turn parked beside working ones whose cap falls inside a later
   recovery is released by that recovery's pass (pinned here; a mutant that
   left it to the idle check survived).
"""
from __future__ import annotations

import threading

import pytest

from lib import agents as agents_db
from lib.account_failover import MAX_PARK_SECONDS, RECHECK_SECONDS, AccountFailover
from lib.protocol import AgentState
from tests.unit.test_account_plan_hostage import (  # noqa: F401
    Fleet, USAGE_LIMIT, _dispatch, _local_dispatch_runtime, _parked_beside_a_working_turn)

WEEKLY = "You've hit your weekly limit for Fable · resets 3pm"
MINUTE = 60.0


def _fable_parked_beside_sonnet(tmp_path, monkeypatch):
    """Fable's week is spent, Sonnet's is not: the first Fable turn parks and
    the Sonnet turn works on. Then the quota is back, at minute 0, and nothing
    asks: the selector is not run while Sonnet works."""
    fleet = Fleet(tmp_path, monkeypatch, "claude", ["fable", "sonnet"], unserved=("fable",))
    start = fleet.clock[0]
    fleet.limit(0, WEEKLY)
    fleet.run_until(start)
    assert fleet.waiting() == ["waiting", None]
    fleet.sessions_started()
    fleet.no_account_offers()
    return fleet, start


def _started(fleet, session):
    return fleet.started_at.get(f"trace-{session}", [])


def test_when_the_model_is_back_only_the_first_turn_waits_its_half_hour(tmp_path, monkeypatch):
    fleet, start = _fable_parked_beside_sonnet(tmp_path, monkeypatch)

    for number in range(1, 6):          # a Fable turn from a different agent every 25 minutes
        fleet.run_until(start + number * 25 * MINUTE)
        _dispatch(fleet, f"fable-{number}", "fable")
    fleet.run_until(start + 150 * MINUTE)

    # The first one waited its half hour, and was the trial.
    assert fleet.starts(0)[1:] == [start + MAX_PARK_SECONDS]
    # The one parked behind it started a minute later, not at its own half hour.
    assert _started(fleet, "fable-1") == [start + MAX_PARK_SECONDS + RECHECK_SECONDS]
    # And nobody after that waited at all.
    assert [_started(fleet, f"fable-{number}") for number in (2, 3, 4, 5)] == [
        [start + number * 25 * MINUTE] for number in (2, 3, 4, 5)]
    # Sonnet was never stopped for any of it, and the selector never ran again.
    assert len(fleet.spawns(1)) == 2 and len(fleet.selector_calls()) == 1
    assert not fleet.coordinator.recovering and fleet.scheduled == []
    assert fleet.coordinator.trials == {}
    # The turn that waited behind the trial is told once that it was recovered.
    first = [call for _, call in fleet.backends.spawned if call["trace_id"] == "trace-fable-1"]
    assert len(first) == 1 and "Clarp recovered from" not in first[0]["text"]   # it had never run


def test_a_trial_that_meets_the_limit_again_leaves_the_others_parked(tmp_path, monkeypatch):
    """The model is not back. The released turn fails visibly, as a released
    turn does, and the turn behind it waits for its own cap, where it is the
    next trial."""
    fleet, start = _fable_parked_beside_sonnet(tmp_path, monkeypatch)
    fleet.run_until(start + 25 * MINUTE)
    waiting, _spawned = _dispatch(fleet, "fable-1", "fable")
    fleet.run_until(start + MAX_PARK_SECONDS)
    assert len(fleet.spawns(0)) == 2 and fleet.coordinator.trials

    fleet.run_until(start + MAX_PARK_SECONDS + 10)
    fleet.spawns(0)[-1]["on_error"](WEEKLY)
    assert fleet.state(0)["kind"] == AgentState.INTERRUPTED and fleet.coordinator.trials == {}

    fleet.run_until(start + 54 * MINUTE)
    assert _started(fleet, "fable-1") == []
    assert agents_db.latest_state(waiting)["detail"]["account_recovery"] == "waiting"
    fleet.run_until(start + 56 * MINUTE)                 # its own half hour, from minute 25
    assert _started(fleet, "fable-1") == [start + 55 * MINUTE]
    assert len(fleet.spawns(1)) == 2 and len(fleet.selector_calls()) == 1
    assert fleet.coordinator.trials == {}                # nobody is left for it to prove anything to


def test_a_trial_that_is_over_within_the_minute_proves_nothing(tmp_path, monkeypatch):
    """Only a turn still running a recheck later counts. One that ended
    sooner, however it ended, is not asked."""
    fleet, start = _fable_parked_beside_sonnet(tmp_path, monkeypatch)
    fleet.run_until(start + 25 * MINUTE)
    _dispatch(fleet, "fable-1", "fable")
    fleet.run_until(start + MAX_PARK_SECONDS + 20)
    fleet.sessions_started()
    fleet.spawns(0)[-1]["on_result"]({"duration_ms": 5})      # done in twenty seconds

    fleet.run_until(start + 40 * MINUTE)

    assert fleet.coordinator.trials == {} and _started(fleet, "fable-1") == []


def test_a_trial_that_was_stopped_proves_nothing(tmp_path, monkeypatch):
    """The released turn is no longer the agent's turn when the look comes
    (a Stop took its slot). That says nothing about the model."""
    import lib.turn_dispatch as _td

    fleet, start = _fable_parked_beside_sonnet(tmp_path, monkeypatch)
    fleet.run_until(start + 25 * MINUTE)
    _dispatch(fleet, "fable-1", "fable")
    fleet.run_until(start + MAX_PARK_SECONDS + 20)
    assert _td._SLOTS.release(fleet.agents[0], "trace-0")

    fleet.run_until(start + 40 * MINUTE)

    assert fleet.coordinator.trials == {} and _started(fleet, "fable-1") == []


def test_a_released_turn_that_meets_the_limit_ends_its_trial_there():
    """The coordinator alone: the answer to "is the model back" is known the
    moment the released turn fails on its account, not when the turn is
    cleared away."""
    coordinator, clock, scheduled, _asked, (first, second), attempt = (
        _parked_beside_a_working_turn([frozenset({"sonnet"})]))
    clock[0] = 600.0
    assert coordinator.register(attempt("d", "fable")) is True       # parked behind them
    clock[0] = MAX_PARK_SECONDS
    scheduled.pop()()                                                # their half hour
    assert first.resume.call_count == second.resume.call_count == 1
    assert set(coordinator.trials) == {("b", "trace"), ("c", "trace")}

    assert coordinator.register(attempt("b", "fable")) is False      # its released process
    assert coordinator.request("b", "trace", ("selector",)) is False # the limit again

    assert set(coordinator.trials) == {("c", "trace")}


def test_a_new_turn_for_the_model_still_parks_while_the_trial_is_undecided(tmp_path, monkeypatch):
    fleet, start = _fable_parked_beside_sonnet(tmp_path, monkeypatch)
    fleet.run_until(start + 25 * MINUTE)
    _dispatch(fleet, "fable-1", "fable")
    fleet.run_until(start + MAX_PARK_SECONDS + 30)            # released, not yet a minute ago

    _dispatch(fleet, "fable-2", "fable")
    assert _started(fleet, "fable-2") == []
    fleet.coordinator.recover()                               # a pass that comes early
    assert _started(fleet, "fable-1") == _started(fleet, "fable-2") == []
    assert fleet.coordinator.trials                           # still undecided

    # That pass set the next look a recheck after itself.
    fleet.run_until(start + MAX_PARK_SECONDS + 30 + RECHECK_SECONDS)
    assert _started(fleet, "fable-1") == _started(fleet, "fable-2") == [
        start + MAX_PARK_SECONDS + 30 + RECHECK_SECONDS]


# --- 3 ---------------------------------------------------------------------------------

def test_a_recoverys_own_pass_releases_a_parked_turn_whose_cap_falls_inside_it(
        tmp_path, monkeypatch):
    """Ten seconds before the Fable turn's half hour the Sonnet turn meets its
    own limit. The recovery that follows takes twenty seconds and finds
    Sonnet only. The Fable turn is released by that pass, not a minute later
    by the next look at the parked turns."""
    fleet, start = _fable_parked_beside_sonnet(tmp_path, monkeypatch)
    fleet.no_account_offers("fable")
    fleet.run_until(start + MAX_PARK_SECONDS - 10)
    fleet.selector_seconds = 20
    fleet.limit(1)

    fleet.run_until(start + MAX_PARK_SECONDS + 15)

    assert fleet.asked_at[-1] == start + MAX_PARK_SECONDS - 10
    assert fleet.starts(0)[1:] == [start + MAX_PARK_SECONDS + 10]      # with the answer
    assert fleet.starts(1)[-1] == start + MAX_PARK_SECONDS + 10


def test_a_turn_released_by_a_recoverys_pass_is_a_trial_too(tmp_path, monkeypatch):
    """The same release with a second Fable turn parked behind the first. The
    selector still leaves Fable out, but the released turn runs: a minute
    later the one behind it starts."""
    fleet, start = _fable_parked_beside_sonnet(tmp_path, monkeypatch)
    fleet.no_account_offers("fable")
    fleet.run_until(start + 25 * MINUTE)
    _dispatch(fleet, "fable-1", "fable")
    fleet.run_until(start + MAX_PARK_SECONDS - 10)
    fleet.selector_seconds = 20
    fleet.limit(1)
    fleet.run_until(start + MAX_PARK_SECONDS + 15)
    assert fleet.starts(0)[1:] == [start + MAX_PARK_SECONDS + 10] and _started(fleet, "fable-1") == []

    fleet.run_until(start + MAX_PARK_SECONDS + 10 + RECHECK_SECONDS)

    assert _started(fleet, "fable-1") == [start + MAX_PARK_SECONDS + 10 + RECHECK_SECONDS]
    assert len(fleet.selector_calls()) == 2 and fleet.coordinator.trials == {}


# --- 2 ---------------------------------------------------------------------------------

def test_a_hand_over_of_parked_turns_with_no_recovery_running_still_carries_them():
    """The last working turn has finished and the look that would begin a
    recovery for the parked ones has not come yet. The drain hands the parked
    turns over; the next runtime must ask the selector before it starts them."""
    coordinator, clock, _scheduled, _asked, parked, attempt = _parked_beside_a_working_turn(
        [frozenset({"sonnet"})])
    clock[0] = 40.0
    coordinator.discard("a", "trace")                    # the working turn is done
    assert not coordinator.recovering
    assert coordinator.carry_over(1_000_000.0) is None   # nothing handed over: nothing to carry

    taken = coordinator.take_parked(lambda item: True)
    assert taken == parked
    carry = coordinator.carry_over(1_000_000.0, [item.agent_id for item in taken])

    # Their park began at 0 on this clock, forty seconds before the hand-over.
    assert carry == {"parked_since_ms": int((1_000_000.0 - 40.0) * 1000), "agents": ["b", "c"]}

    # The next runtime.
    later, asked, scheduled = [500.0], [], []

    def switch(command, models):
        asked.append(list(models))
        return True

    following = AccountFailover(threading.RLock(), switch=switch, now=lambda: later[0],
                                schedule=lambda delay, cb: scheduled.append(cb))
    assert following.resume_recovery(("selector",), carry["parked_since_ms"], 1_000_010.0,
                                     carry["agents"])
    carried = [attempt("b", "fable"), attempt("c", "fable")]
    assert [following.register(item) for item in carried] == [True, True]    # parked, not started
    assert [item.resume.call_count for item in carried] == [0, 0]

    scheduled.pop(0)()

    assert asked == [["fable"]]                           # asked first
    assert [item.resume.call_count for item in carried] == [1, 1] and not following.recovering
    # And the half hour did not start again: fifty seconds of it were used.
    assert following.parked_since == 500.0 - 50.0


# --- the runtime's journal, 2026-11-12 06:49:31 ----------------------------------------

def _coordinator(answers):
    from unittest.mock import Mock
    from lib.account_failover import Attempt

    def attempt(agent, model="sonnet", handle=True):
        item = Attempt(agent, "trace", model, {}, Mock(return_value=True), Mock(), Mock(),
                       handle=Mock(drain_thread=None, process_group=None) if handle else None)
        item.spawned.set()
        return item

    scheduled, asked = [], []

    def switch(command, models):
        asked.append(list(models))
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    coordinator = AccountFailover(threading.RLock(), switch=switch, now=lambda: 100.0,
                                  schedule=lambda delay, cb: scheduled.append((delay, cb)))
    return coordinator, attempt, scheduled, asked


def test_a_turn_that_registers_while_a_recovery_stops_the_others_does_not_fail_the_pass(capsys):
    """"claudeAccountRecoveryFail AttributeError": stopping seven processes
    took three seconds, a new turn of another agent registered meanwhile and
    was parked, and the pass, finding work it had not stopped, read a "stop
    them" decision as "ask for these models". The pass was lost and every
    parked turn of the pool waited another minute. A turn parked before it
    started has nothing to stop."""
    coordinator, attempt, scheduled, asked = _coordinator([True])
    running, late = attempt("running"), attempt("late", "opus", handle=False)
    running.handle.terminate.side_effect = lambda: coordinator.register(late)
    coordinator.register(running)
    assert coordinator.request("running", "trace", ("selector",))

    scheduled.pop()[1]()

    assert late.stopped is True and late.state["account_recovery"] is True
    assert asked == [["opus", "sonnet"]]                  # asked in that same pass, for both
    assert running.resume.call_count == late.resume.call_count == 1
    assert not coordinator.recovering and scheduled == []
    assert "claudeAccountRecoveryFail" not in capsys.readouterr().err


def test_work_found_still_running_after_the_stop_gets_a_pass_of_its_own(capsys):
    """Should anything owned still run when the pass looks again, it is
    stopped by the next pass, at once, instead of failing this one."""
    coordinator, attempt, scheduled, asked = _coordinator([True])
    running, late = attempt("running"), attempt("late", "opus")

    def arrives():
        coordinator.register(late)
        late.stopped = False                               # as work put back unstopped would be

    running.handle.terminate.side_effect = arrives
    coordinator.register(running)
    assert coordinator.request("running", "trace", ("selector",))

    scheduled.pop()[1]()
    assert asked == [] and [delay for delay, _ in scheduled] == [0.0]
    scheduled.pop()[1]()

    assert late.handle.terminate.call_count == 1 and asked == [["opus", "sonnet"]]
    assert running.resume.call_count == late.resume.call_count == 1
    assert "claudeAccountRecoveryFail" not in capsys.readouterr().err


def test_a_failed_recovery_pass_says_where_it_failed(capsys):
    """One line in the journal, with the exception's message and the place:
    the type alone ("AttributeError") left nothing to go on."""
    coordinator, attempt, scheduled, _asked = _coordinator([AttributeError("no attribute 'models'")])
    coordinator.register(attempt("running"))
    assert coordinator.request("running", "trace", ("selector",))

    scheduled.pop()[1]()

    lines = [line for line in capsys.readouterr().err.splitlines()
             if "claudeAccountRecoveryFail" in line]
    assert len(lines) == 1
    assert "AttributeError: no attribute 'models'" in lines[0]
    assert "test_account_failover_followup.py" in lines[0] and "switch" in lines[0]
    assert "account_failover.py" in lines[0] and "_recover" in lines[0]
    assert coordinator.recovering and len(scheduled) == 1   # parked for the next check, as before
