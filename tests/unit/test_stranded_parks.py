"""A turn that waits for an account though the runtime no longer holds it.

12 November 2026: a parked turn was dropped by the account coordinator while
its agent kept saying it waited for an account. Nothing would ever resume it,
and it was found by accident four hours later. The Host now compares each
agent that says it waits with what the runtime lists as waiting, and tells the
owner when the two disagree for five minutes.
"""
from __future__ import annotations

import pytest

from lib import agents, artifacts, attention_index, backends, scheduler, stranded_parks
from lib import task_goal_recovery, turn_dispatch_state
from lib.runtime_bridge import RuntimeUnavailable
from lib.error_classify import account_wait_message
from tests.unit.test_account_plan_hostage import Fleet, _local_dispatch_runtime  # noqa: F401

MINUTE = 60_000
START = 1_800_000_000_000


class Runtime:
    """What the runtime answers to ``status``."""

    def __init__(self):
        self.waiting: dict[str, list[str]] = {"claude": [], "codex": []}
        self.down = False
        self.asked = 0

    def status(self) -> dict:
        self.asked += 1
        if self.down:
            raise RuntimeUnavailable("agent runtime status unavailable")
        return {"active": {}, **{
            f"{pool}_account_recovery": {"recovering": False, "checking": False,
                                         "waiting": list(agents_waiting)}
            for pool, agents_waiting in self.waiting.items()}}


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *a: [])
    lines: list[tuple[str, str]] = []
    monkeypatch.setattr(stranded_parks, "log", lambda event, detail="": lines.append((event, detail)))
    monkeypatch.setattr(stranded_parks, "log_exception",
                        lambda event, exc, detail="": lines.append((event, detail)))
    runtime = Runtime()
    watch = stranded_parks.StrandedParks(status=runtime.status)
    return watch, runtime, lines


def _agent(name: str, backend: str = "claude") -> str:
    return agents.create_agent(persona=name, voice_id="", cwd="/tmp",
                               session=name.lower(), backend=backend)


def _park(agent: str, trace: str = "trace-1", backend: str = "claude") -> None:
    agents.record_state(agent, "thinking", {
        "dispatch": backend, "trace_id": trace, "account_recovery": "waiting",
        "message": account_wait_message(backend)})


def _alerts(agent: str) -> list[dict]:
    return [a for a in artifacts.list_artifacts(agent_id=agent)
            if (a.get("payload") or {}).get("attention_kind") == stranded_parks.KIND]


def _attention() -> list[tuple[str, str]]:
    return [(item["session"], item["attention_bucket"])
            for item in attention_index.page()["artifacts"] if not item.get("archived_at")]


def _minutes(watch, start: int, minutes: int) -> int:
    """One check a minute; returns what the checks changed."""
    return sum(watch.tick(START + (start + m) * MINUTE) for m in range(minutes + 1))


def test_a_park_the_runtime_does_not_hold_is_logged_and_shown_after_five_minutes(host):
    watch, runtime, lines = host
    sam = _agent("Sam")
    _park(sam)

    assert _minutes(watch, 0, 4) == 0
    assert _alerts(sam) == [] and lines == [] and _attention() == []

    assert watch.tick(START + 5 * MINUTE) == 1
    [alert] = _alerts(sam)
    assert alert["status"] == "failed" and alert["type"] == "document"
    assert alert["title"] == "Sam is stuck waiting for an account"
    assert "Stop" in alert["summary"] and "send" in alert["summary"]
    assert alert["payload"]["trace_id"] == "trace-1"
    assert _attention() == [("sam", "blocking")]
    assert [event for event, _ in lines] == ["strandedPark"]
    assert f"agent={sam}" in lines[0][1] and "trace=trace-1" in lines[0][1]

    # Told once: the next hours add neither a line nor a second alert.
    assert _minutes(watch, 6, 240) == 0
    assert len(_alerts(sam)) == 1 and len(lines) == 1


def test_a_park_the_runtime_lists_is_never_reported(host):
    watch, runtime, lines = host
    sam, kim = _agent("Sam"), _agent("Kim", "codex")
    _park(sam)
    _park(kim, backend="codex")
    runtime.waiting = {"claude": [sam], "codex": [kim]}

    assert _minutes(watch, 0, 240) == 0
    assert _alerts(sam) == [] and _alerts(kim) == [] and lines == []


def test_only_the_agent_the_runtime_dropped_is_reported(host):
    watch, runtime, lines = host
    sam, kim = _agent("Sam"), _agent("Kim")
    _park(sam)
    _park(kim, "trace-2")
    runtime.waiting["claude"] = [kim]

    assert _minutes(watch, 0, 10) == 1
    assert len(_alerts(sam)) == 1 and _alerts(kim) == []


def test_five_minutes_means_five_minutes_without_a_break(host):
    watch, runtime, lines = host
    sam = _agent("Sam")
    _park(sam)

    assert _minutes(watch, 0, 4) == 0
    runtime.waiting["claude"] = [sam]            # a hand-over put it back
    assert watch.tick(START + 5 * MINUTE) == 0
    runtime.waiting["claude"] = []
    assert _minutes(watch, 6, 4) == 0
    assert _alerts(sam) == []
    assert watch.tick(START + 11 * MINUTE) == 1


def test_a_runtime_that_does_not_answer_raises_no_alarm_and_restarts_the_count(host):
    watch, runtime, lines = host
    sam = _agent("Sam")
    _park(sam)
    assert _minutes(watch, 0, 3) == 0

    runtime.down = True
    assert _minutes(watch, 4, 30) == 0
    assert _alerts(sam) == []
    assert [event for event, _ in lines] == ["strandedParkStatusUnavailable"]

    runtime.down = False
    assert _minutes(watch, 35, 4) == 0
    assert watch.tick(START + 40 * MINUTE) == 1


def test_nothing_parked_asks_the_runtime_nothing(host):
    watch, runtime, lines = host
    sam = _agent("Sam")
    agents.record_state(sam, "thinking", {"trace_id": "trace-1"})

    assert _minutes(watch, 0, 30) == 0
    assert runtime.asked == 0


def test_the_check_runs_once_a_minute(host):
    watch, runtime, lines = host
    _park(_agent("Sam"))

    for second in range(0, 120, 15):             # the scheduler's fifteen seconds
        watch.tick(START + second * 1000)
    assert runtime.asked == 2


def test_the_alert_is_withdrawn_when_the_turn_ends(host):
    watch, runtime, lines = host
    sam = _agent("Sam")
    _park(sam)
    assert _minutes(watch, 0, 5) == 1

    agents.record_state(sam, "stopped", {"trace_id": "trace-1"})
    assert watch.tick(START + 6 * MINUTE) == 1
    [alert] = _alerts(sam)
    assert alert["status"] == "cancelled"
    assert _attention() == []

    # The next turn of the same agent strands too: its own alert.
    _park(sam, "trace-2")
    assert _minutes(watch, 7, 5) == 1
    assert sorted(a["status"] for a in _alerts(sam)) == ["cancelled", "failed"]
    assert _attention() == [("sam", "blocking")]


def test_the_alert_is_withdrawn_when_the_runtime_holds_the_turn_again(host):
    watch, runtime, lines = host
    sam = _agent("Sam")
    _park(sam)
    assert _minutes(watch, 0, 5) == 1

    runtime.waiting["claude"] = [sam]
    assert watch.tick(START + 6 * MINUTE) == 1
    assert [a["status"] for a in _alerts(sam)] == ["cancelled"]
    assert _attention() == []

    # Dropped again: the same alert is open again after its own five minutes.
    runtime.waiting["claude"] = []
    assert _minutes(watch, 7, 4) == 0
    assert watch.tick(START + 12 * MINUTE) == 1
    assert [a["status"] for a in _alerts(sam)] == ["failed"]
    assert _attention() == [("sam", "blocking")]
    assert [event for event, _ in lines] == ["strandedPark", "strandedPark"]


def test_an_alert_the_owner_dismissed_does_not_come_back(host):
    watch, runtime, lines = host
    sam = _agent("Sam")
    _park(sam)
    assert _minutes(watch, 0, 5) == 1
    [alert] = _alerts(sam)
    artifacts.archive(alert["artifact_id"], archived=True,
                      expected_updated_at=alert["updated_at"])

    assert _minutes(watch, 6, 60) == 0
    assert _attention() == []

    # Nor when the runtime held the turn for a while and dropped it again.
    runtime.waiting["claude"] = [sam]
    watch.tick(START + 67 * MINUTE)
    runtime.waiting["claude"] = []
    assert _minutes(watch, 68, 30) == 0
    assert _attention() == [] and [a["status"] for a in _alerts(sam)] in ([], ["cancelled"])


def test_a_host_restart_finds_the_alert_it_already_raised(host):
    watch, runtime, lines = host
    sam = _agent("Sam")
    _park(sam)
    assert _minutes(watch, 0, 5) == 1

    again = stranded_parks.StrandedParks(status=runtime.status)
    assert _minutes(again, 6, 10) == 0
    assert len(_alerts(sam)) == 1


def test_a_deleted_agent_is_not_reported(host):
    watch, runtime, lines = host
    sam = _agent("Sam")
    _park(sam)
    agents.soft_delete(sam)

    assert _minutes(watch, 0, 10) == 0
    assert runtime.asked == 0


def test_the_status_is_the_runtimes_and_this_processs_own(monkeypatch):
    waiting = lambda *agents_waiting: {"recovering": False, "checking": False,   # noqa: E731
                                       "waiting": list(agents_waiting)}
    monkeypatch.setattr(backends, "runtime_status", lambda **kw: {
        "active": {"a": "t"}, "claude_account_recovery": waiting("in-the-runtime")})
    monkeypatch.setattr(turn_dispatch_state, "runtime_status", lambda: {
        "active": {}, "claude_account_recovery": waiting("in-this-process")})

    # A Host that runs its own turns holds the coordinators itself.
    monkeypatch.setattr(backends, "_RUNTIME_CLIENT", None)
    assert stranded_parks._listed(stranded_parks.runtime_status()) == {"in-this-process"}

    # Beside a runtime both count, and the runtime's other facts are the runtime's.
    monkeypatch.setattr(backends, "_RUNTIME_CLIENT", object())
    status = stranded_parks.runtime_status()
    assert stranded_parks._listed(status) == {"in-the-runtime", "in-this-process"}
    assert status["active"] == {"a": "t"}


def test_a_runtime_that_cannot_be_reached_is_an_error_not_an_empty_answer(monkeypatch):
    def unreachable(**kw):
        raise RuntimeUnavailable("agent runtime status unavailable")

    monkeypatch.setattr(backends, "_RUNTIME_CLIENT", object())
    monkeypatch.setattr(backends, "runtime_status", unreachable)
    with pytest.raises(RuntimeUnavailable):
        stranded_parks.runtime_status()


def test_the_host_scheduler_runs_the_check(monkeypatch):
    seen: list[int] = []

    class Watch:
        def tick(self, now):
            seen.append(now)
            raise RuntimeError("the check broke")

    monkeypatch.setattr(stranded_parks, "log", lambda *a: None)
    failures: list[str] = []
    monkeypatch.setattr(task_goal_recovery, "log_failure_throttled",
                        lambda event, *a, **kw: failures.append(event))
    runner = scheduler.AgentScheduleRunner(dispatch_turn=lambda *a: None)
    assert isinstance(runner._stranded_parks, stranded_parks.StrandedParks)
    runner._stranded_parks = Watch()

    assert runner.tick() == 0                    # a broken check stops no schedule
    assert len(seen) == 1 and failures == ["strandedParkCheckFail"]


def test_the_real_dispatcher_and_coordinator_a_held_park_is_quiet_a_dropped_one_is_told(
        tmp_path, monkeypatch):
    """The real park state and the real status, in a process that runs its
    own turns. While the coordinator holds the parked turn nothing is said;
    once it has dropped it (what it did on 12 November, reading the moved
    trace marker as "replaced") the owner is told five minutes later."""
    lines = []
    monkeypatch.setattr(stranded_parks, "log", lambda event, detail="": lines.append(event))
    fleet = Fleet(tmp_path, monkeypatch, "claude", ["sonnet"], unserved=("sonnet",))
    agent = fleet.agents[0]
    fleet.limit(0)
    fleet.run_until(fleet.clock[0] + 120)
    assert fleet.waiting() == ["waiting"]
    watch = stranded_parks.StrandedParks()

    assert _minutes(watch, 0, 30) == 0 and _alerts(agent) == []

    with fleet.coordinator.lock:
        fleet.coordinator.attempts.pop(agent)
    assert _minutes(watch, 31, 4) == 0
    assert watch.tick(START + 36 * MINUTE) == 1
    [alert] = _alerts(agent)
    assert alert["payload"]["trace_id"] == "trace-0" and lines == ["strandedPark"]

    # The owner's Stop ends the turn, and the alert with it.
    agents.record_state(agent, "stopped", {"trace_id": "trace-0"})
    assert watch.tick(START + 37 * MINUTE) == 1
    assert [a["status"] for a in _alerts(agent)] == ["cancelled"]
