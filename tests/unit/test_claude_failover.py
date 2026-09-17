import json
import subprocess
import sys
import threading
from unittest.mock import Mock

import pytest

from lib.claude_failover import (
    MAX_PARK_SECONDS, RECHECK_SECONDS, Attempt, ClaudeFailover, switch_account)


def make_attempt(agent="a", *, model="sonnet"):
    item = Attempt(agent, f"trace-{agent}", model, {}, Mock(return_value=True),
                   Mock(), Mock(), handle=Mock(drain_thread=None))
    item.spawned.set()
    return item


def make_coordinator(switch=None):
    scheduled = []
    coordinator = ClaudeFailover(threading.RLock(), switch=switch or Mock(return_value=True),
        schedule=lambda delay, callback: scheduled.append((delay, callback)), now=lambda: 100)
    return coordinator, scheduled


def test_simultaneous_limits_stop_and_drain_all_before_switching_once():
    coordinator, scheduled = make_coordinator()
    a, b = make_attempt(), make_attempt("b")
    coordinator.register(a)
    coordinator.register(b)
    def switch(command, models):
        a.handle.wait.assert_called_once()
        b.handle.wait.assert_called_once()
        assert models == ["sonnet"]
        return True
    coordinator.switch = Mock(side_effect=switch)
    assert coordinator.request("a", "trace-a", ("selector",))
    assert coordinator.request("b", "trace-b", ("selector",))
    assert len(scheduled) == 1
    scheduled.pop()[1]()
    coordinator.switch.assert_called_once()
    a.resume.assert_called_once()
    b.resume.assert_called_once()
    assert a.state["account_recovery"]  # old callbacks stay fenced
    assert coordinator.recovering is False


def make_clocked_coordinator(switch):
    """A coordinator whose clock the test advances, to drive many recheck cycles."""
    clock = {"t": 100.0}
    scheduled = []
    coordinator = ClaudeFailover(
        threading.RLock(), switch=switch,
        schedule=lambda delay, callback: scheduled.append((delay, callback)),
        now=lambda: clock["t"])
    return coordinator, scheduled, clock


def drive_past_the_cap(scheduled, clock):
    """Run recheck cycles until the parking cap has elapsed, then one more."""
    deadline = clock["t"] + MAX_PARK_SECONDS
    while scheduled and clock["t"] <= deadline:
        _delay, callback = scheduled.pop()
        clock["t"] += RECHECK_SECONDS
        callback()


def test_a_selector_that_cannot_answer_does_not_park_the_fleet_for_ever():
    """A selector that crashes, times out or answers unreadably has told us
    nothing about quota. Parking on that indefinitely is how the whole Claude
    fleet went silent for hours: every agent waits, nothing is logged to the
    user, and no turn ever fails visibly."""
    coordinator, scheduled, clock = make_clocked_coordinator(Mock(return_value=None))
    item = make_attempt()
    coordinator.register(item)
    coordinator.request("a", "trace-a", ("selector",))
    scheduled.pop()[1]()
    item.resume.assert_not_called()          # one inconclusive check may wait

    drive_past_the_cap(scheduled, clock)
    assert item.resume.called, "parked work must be released, never frozen for ever"
    assert not coordinator.recovering
    assert not scheduled


def test_a_selector_that_keeps_raising_also_releases_the_work():
    coordinator, scheduled, clock = make_clocked_coordinator(
        Mock(side_effect=RuntimeError("selector exploded")))
    item = make_attempt()
    coordinator.register(item)
    coordinator.request("a", "trace-a", ("selector",))
    scheduled.pop()[1]()
    drive_past_the_cap(scheduled, clock)
    assert item.resume.called, "a failing recovery must not hold the turn for ever"
    assert not coordinator.recovering


def test_a_definite_no_quota_still_parks_before_the_cap():
    """Waiting out a real usage limit is the point of parking; only the
    unbounded part is the bug."""
    coordinator, scheduled, clock = make_clocked_coordinator(Mock(return_value=False))
    item = make_attempt()
    coordinator.register(item)
    coordinator.request("a", "trace-a", ("selector",))
    scheduled.pop()[1]()
    assert coordinator.recovering
    item.resume.assert_not_called()
    clock["t"] += RECHECK_SECONDS
    scheduled.pop()[1]()
    item.resume.assert_not_called()          # still parked well inside the cap


def test_no_capacity_keeps_work_parked_and_stop_cancels_it():
    coordinator, scheduled = make_coordinator(Mock(return_value=False))
    item = make_attempt()
    coordinator.register(item)
    coordinator.request("a", "trace-a", ("selector",))
    scheduled.pop()[1]()
    assert coordinator.recovering
    assert scheduled[0][0] == 60
    item.resume.assert_not_called()
    item.owned.return_value = False
    scheduled.pop()[1]()
    assert not coordinator.recovering
    assert not scheduled
    coordinator.switch.assert_called_once()


def test_new_turn_waits_while_accounts_are_checked():
    coordinator, scheduled = make_coordinator()
    a, b = make_attempt(), make_attempt("b")
    coordinator.register(a)
    coordinator.request("a", "trace-a", ("selector",))
    b.handle = None  # no subprocess was launched for this parked turn
    assert coordinator.register(b)
    scheduled.pop()[1]()
    b.pause.assert_called_once()
    b.resume.assert_called_once()


def test_new_model_during_probe_requires_another_check():
    coordinator, scheduled = make_coordinator()
    a, b = make_attempt(), make_attempt("b", model="opus")
    coordinator.register(a)
    coordinator.request("a", "trace-a", ("selector",))
    def switch(*_):
        b.handle = None
        coordinator.register(b)
        return True
    coordinator.switch = switch
    scheduled.pop()[1]()
    a.resume.assert_not_called()
    b.resume.assert_not_called()
    assert coordinator.recovering


def test_a_still_draining_transcript_prevents_switch_and_resume():
    coordinator, scheduled = make_coordinator()
    item = make_attempt()
    item.handle.drain_thread = Mock()
    item.handle.drain_thread.is_alive.return_value = True
    coordinator.register(item)
    coordinator.request("a", "trace-a", ("selector",))
    scheduled.pop()[1]()
    coordinator.switch.assert_not_called()
    item.resume.assert_not_called()
    assert scheduled


def test_kill_unresponsive_owned_process_before_resume():
    coordinator, scheduled = make_coordinator()
    item = make_attempt()
    item.handle.wait.side_effect = [subprocess.TimeoutExpired("claude", 10), 0]
    coordinator.register(item)
    coordinator.request("a", "trace-a", ("selector",))
    scheduled.pop()[1]()
    item.handle.kill.assert_called_once()
    item.resume.assert_called_once()


@pytest.mark.parametrize("payload,exit_code,expected", [
    ('{"available": true}', 0, True),
    # A read verdict of "no account serves these models" is definite.
    ('{"available": false}', 0, False),
    # Everything below is the selector failing to answer, which says nothing
    # about quota and must not be mistaken for "no quota".
    ('{"available": "true"}', 0, None),
    ('{"available": true}', 1, None),
    ('not json', 0, None), ('[]', 0, None),
    ('{"available": false, "error": "HTTPError"}', 0, None),
])
def test_selector_requires_successful_explicit_json(payload, exit_code, expected):
    code = "import sys; print(sys.argv[1]); sys.exit(int(sys.argv[2]))"
    assert switch_account((sys.executable, "-c", code, payload, str(exit_code)),
                          ["sonnet"]) is expected


def test_selector_receives_models_without_a_shell():
    code = "import json,sys; x=json.load(sys.stdin); print(json.dumps({'available': x['models']==['literal; $value']}))"
    assert switch_account((sys.executable, "-c", code), ["literal; $value"])


def test_surviving_descendant_is_killed_when_parent_exits_before_drain():
    coordinator, scheduled = make_coordinator()
    item = make_attempt()
    item.handle.drain_thread = Mock()
    item.handle.drain_thread.is_alive.side_effect = [True, False]
    coordinator.register(item)
    coordinator.request("a", "trace-a", ("selector",))
    scheduled.pop()[1]()
    item.handle.kill.assert_called_once()
    assert item.handle.wait.call_count == 2
    item.resume.assert_called_once()
