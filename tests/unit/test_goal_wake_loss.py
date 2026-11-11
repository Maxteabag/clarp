"""A goal wake that never ran must not end the goal's wakes.

2026-11-10/11, seven agents on two Hosts: a goal wake was dispatched while the
Claude account pool was waiting for an account with usage. The dispatcher
parked the turn before its spawn, so its queue row stayed ``queued``. When the
park ended the wake was refused by its own "waiting for an account" state and
settled as a failed turn that no process ever ran. Nothing settles a row that
never started and nothing dispatches one whose launch is unproven, and the
scheduler counted it as queued work: every goal of the agent read "Owner has
live or queued work" until someone deleted the row by hand (32 hours on one
agent). These tests drive that sequence through the real dispatcher,
coordinator and scheduler.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import db, task_goal_recovery, task_goal_state, task_plans, turn_queue
from lib.account_failover import MAX_PARK_SECONDS
from lib.protocol import AgentState
import lib.turn_dispatch as _td
from tests.unit.test_turn_dispatch import (
    _enable_account_failover, _goal, _goal_owner, _wakes)

WAITING = "Waiting for a claude account with available usage"
HOUR_MS = 3_600_000


@pytest.fixture(autouse=True)
def _local_dispatch_runtime(monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    _td.reset_for_tests()
    yield
    _td.reset_for_tests()


def _arm(plan):
    """The owner's checkpoint: wake me now."""
    return task_goal_state.mutate(
        plan["plan_id"], revision=plan["revision"], action="checkpoint",
        data={"progress": "Waiting for the build", "next_work": "Read its result",
              "continuation": {"kind": "timer", "due_at": db.now_ms()}})


def _tick(service, *, offset_ms=0, retired=None):
    """The scheduler's goal pass through the DispatchAdapters wiring, now or
    ``offset_ms`` later by every clock the pass reads."""
    def goal_continuation(session, text, request):
        return service.dispatch(
            text=text, requested_session=session, trace_id=request,
            client_msg_id=request, origin="automation", queue_if_busy=True,
            synthesize_audio=False,
            allow_paused_queue=task_goal_recovery.allows_paused_queue(request))

    extra = {"retired": retired} if retired is not None else {}
    real = db.now_ms
    with pytest.MonkeyPatch.context() as later:
        later.setattr(db, "now_ms", lambda: real() + offset_ms)
        return task_goal_recovery.tick(goal_continuation, now=db.now_ms(), **extra)


def _continuation(plan):
    return task_plans.get(plan["plan_id"])["goal"]["continuation"]


def _history(plan, kind):
    return [entry for entry in task_plans.get(plan["plan_id"])["goal"]["history"]
            if entry["kind"] == kind]


def _exhausted_pool(tmp_path, monkeypatch):
    """Another Claude agent hits the usage limit and the selector finds no
    account with usage left: the pool waits, as it did on those mornings."""
    coordinator, scheduled = _enable_account_failover(monkeypatch, available=False)
    clock = [100.0]
    coordinator.now = lambda: clock[0]
    service, backends, agent_id = _goal_owner(tmp_path)
    other = agents_db.create_agent(persona="Bella", voice_id="V", cwd=str(tmp_path),
                                   session="bella", backend="claude")
    agents_db.start_runtime(other, "bella")
    service.dispatch(text="other work", requested_session="bella", trace_id="bella-work",
                     synthesize_audio=False)
    backends.spawned[-1][1]["on_error"]("You've hit your session limit")
    scheduled.pop(0)[1]()
    assert coordinator.recovering
    backends.live = False
    return SimpleNamespace(service=service, backends=backends, agent_id=agent_id,
                           coordinator=coordinator, scheduled=scheduled, clock=clock)


def _parked_wake(host):
    """The scheduler dispatches the goal's wake into the waiting pool."""
    plan = _arm(_goal())
    assert _tick(host.service) == 1
    request = _continuation(plan)["request_id"]
    assert request.startswith(task_goal_recovery.PREFIX)
    # Parked before its spawn: no process, and the receipt never started.
    assert _wakes(host.backends) == []
    assert turn_queue.status(request) == "queued"
    latest = agents_db.latest_state(host.agent_id)
    assert (latest["kind"], latest["detail"]["account_recovery"],
            latest["detail"]["trace_id"]) == (AgentState.THINKING, "waiting", request)
    return plan, request


def _end_park(host, *, account_available):
    host.coordinator.switch.return_value = account_available
    host.clock[0] += 61 if account_available else MAX_PARK_SECONDS + 61
    host.scheduled.pop(0)[1]()
    assert not host.coordinator.recovering


def test_a_wake_parked_for_an_account_runs_when_one_has_usage(tmp_path, monkeypatch):
    host = _exhausted_pool(tmp_path, monkeypatch)
    plan, request = _parked_wake(host)

    _end_park(host, account_available=True)

    wakes = _wakes(host.backends)
    assert len(wakes) == 1, agents_db.latest_state(host.agent_id)
    assert wakes[0]["trace_id"] == request
    assert turn_queue.status(request) == "started"
    assert agents_db.latest_state(host.agent_id)["kind"] == AgentState.THINKING
    wakes[0]["on_result"]({"duration_ms": 5})
    assert turn_queue.status(request) == "done"
    assert _history(plan, "wake_undelivered") == []


def test_a_wake_released_at_the_park_cap_fails_visibly_and_is_tried_again(
        tmp_path, monkeypatch):
    host = _exhausted_pool(tmp_path, monkeypatch)
    plan, request = _parked_wake(host)

    _end_park(host, account_available=False)   # 30 minutes without an account
    wakes = _wakes(host.backends)
    assert len(wakes) == 1, "released work runs and fails visibly, it is not refused"
    wakes[0]["on_error"]("You've hit your session limit")

    # The failed turn took its receipt with it: nothing is left queued.
    assert turn_queue.status(request) == "failed"
    assert turn_queue.pending_count(host.agent_id) == 0
    host.backends.live = False

    # The scheduler sees its wake did not run and says so, where the owner
    # and the apps read the goal.
    assert _tick(host.service) == 0
    undelivered = _history(plan, "wake_undelivered")
    assert [entry["detail"]["request_id"] for entry in undelivered] == [request]
    assert "Usage limit" in undelivered[0]["detail"]["reason"]
    assert "not delivered" in _continuation(plan)["reason"]
    # In the append-only ledger too, as the Host's own event: a bookkeeping
    # delegate on the goal must never be able to refuse it.
    ledger = db.conn().execute(
        "SELECT actor_kind, reason FROM goal_events WHERE plan_id=? AND kind='wake_undelivered'",
        (plan["plan_id"],)).fetchall()
    assert [tuple(row) for row in ledger] == [
        ("system", undelivered[0]["detail"]["reason"])]

    # After the backoff it wakes the goal again, with a new receipt.
    assert _tick(host.service, offset_ms=HOUR_MS) == 1
    again = _continuation(plan)
    assert again["request_id"] != request and again["attempts"] == 2
    assert [call["trace_id"] for call in _wakes(host.backends)] == [
        request, again["request_id"]]


def test_a_wake_that_fails_before_its_receipt_started_leaves_nothing_queued(
        tmp_path, monkeypatch):
    """The other end of the same hole: any failure between the park and the
    spawn settles the turn while the receipt is still ``queued``."""
    host = _exhausted_pool(tmp_path, monkeypatch)
    plan, request = _parked_wake(host)
    spawn = host.backends.spawn_turn

    def refuses(*_args, **_kwargs):
        raise OSError("claude binary went away")

    host.backends.spawn_turn = refuses
    _end_park(host, account_available=True)
    host.backends.spawn_turn = spawn

    assert agents_db.latest_state(host.agent_id)["kind"] == AgentState.INTERRUPTED
    assert turn_queue.pending_count(host.agent_id) == 0
    assert turn_queue.status(request) == "cancelled"

    assert _tick(host.service) == 0   # still inside the backoff
    assert [entry["detail"]["request_id"]
            for entry in _history(plan, "wake_undelivered")] == [request]
    assert _tick(host.service, offset_ms=HOUR_MS) == 1
    assert len(_wakes(host.backends)) == 1
    assert turn_queue.status(_continuation(plan)["request_id"]) == "started"


# --- what the releases before the fix left behind -------------------------------


def _state(agent_id, kind, ts, detail):
    db.conn().execute(
        "INSERT INTO state_log(agent_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
        (agent_id, ts, kind, json.dumps(detail)))


def _turn(agent_id, trace, started, *, ended=None, settled=None, outcome=None):
    return db.conn().execute(
        "INSERT INTO turns(agent_id, source, trace_id, started_at, ended_at, settled_at,"
        " outcome, synthesize_audio) VALUES (?, 'pwa', ?, ?, ?, ?, ?, 0)",
        (agent_id, trace, started, ended, settled, outcome)).lastrowid


STALE_AGE_MS = 6 * HOUR_MS


def _admitted_wake(tmp_path):
    """A goal whose wake the dispatcher accepted six hours ago, as the
    scheduler recorded it then."""
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    sent = []

    def accepted(session, text, request):
        sent.append((request, text))
        return SimpleNamespace(queued=False)

    real = db.now_ms
    with pytest.MonkeyPatch.context() as earlier:
        earlier.setattr(db, "now_ms", lambda: real() - STALE_AGE_MS)
        plan = _arm(_goal())
        assert task_goal_recovery.tick(accepted, now=db.now_ms()) == 1
    request, text = sent[0]
    assert _continuation(plan)["state"] == "admitted"
    return service, backends, agent_id, plan, request, text


def _stale_row(agent_id, request, text, *, handoff):
    """The rows saved from elitebook on 2026-11-11 before they were deleted:
    poc-videoer4-737c (one turn) and koko-8255 (a Clarp update handed the
    parked wake to the next runtime, which parked it again)."""
    start = db.now_ms() - STALE_AGE_MS + 47
    turn_queue.enqueue(
        queue_id=request, agent_id=agent_id, session="mike", text=text,
        trace_id=request, client_msg_id=request, synthesize_audio=False,
        origin="automation", sender_agent_id="")
    db.conn().execute("UPDATE queued_turns SET enqueued_at=? WHERE queue_id=?",
                      (start, request))
    waiting = {"dispatch": "claude", "trace_id": request, "account_recovery": "waiting",
               "message": WAITING, "origin": "automation"}
    _state(agent_id, "thinking", start + 1, waiting)
    failed_at = start + 765_000
    if handoff:
        handed = start + 406_000
        first = _turn(agent_id, request, start + 1, ended=handed)
        _state(agent_id, "idle", handed, {
            "reason": "update_handoff", "trace_id": request,
            "message": "Waiting for the Clarp update", "dispatch_not_started": True,
            "dispatch_not_started_through_turn_id": first, "origin": "automation"})
        _state(agent_id, "thinking", handed + 14, waiting)
        _turn(agent_id, request, handed + 13, settled=failed_at, outcome="failed")
    else:
        _turn(agent_id, request, start + 1, settled=failed_at, outcome="failed")
    _state(agent_id, "interrupted", failed_at, {
        "dispatch": "claude", "reason": "runner_exit",
        "message": "Agent process exited unexpectedly", "error": WAITING,
        "provider_message": WAITING, "attempts": 1,
        "summary": "Agent process exited unexpectedly. Your message is saved."})
    row = turn_queue.get(request)
    assert row["status"] == "queued" and row["started_at"] is None
    # Exactly the saved state: the queue API lists it and recovery skips it.
    assert turn_queue.state(agent_id)["count"] == 1
    assert turn_queue.pending_for_dispatch() == []
    assert turn_queue.reconcile_receipts() == {}
    assert turn_queue.status(request) == "queued"


@pytest.mark.parametrize("handoff", [False, True], ids=["one-turn", "update-handoff"])
def test_a_stale_wake_row_from_an_older_release_heals_in_the_scheduler_pass(
        tmp_path, handoff):
    service, backends, agent_id, plan, request, text = _admitted_wake(tmp_path)
    other = _arm(_goal(title="A second goal of the same agent"))
    _stale_row(agent_id, request, text, handoff=handoff)

    # Both goals of the agent were held by the one row; both wake now.
    assert _tick(service, offset_ms=HOUR_MS) == 2

    # The dead row is retired by the pass itself, not by a DELETE /turn-queue.
    assert turn_queue.status(request) == "cancelled"
    undelivered = _history(plan, "wake_undelivered")
    assert [entry["detail"]["request_id"] for entry in undelivered] == [request]
    assert undelivered[0]["detail"]["reason"] == "Agent process exited unexpectedly"
    requests = [_continuation(p)["request_id"] for p in (plan, other)]
    assert request not in requests and _continuation(plan)["attempts"] == 2
    # One runs, the other waits behind it as real queued work.
    assert len(_wakes(backends)) == 1
    assert sorted(turn_queue.status(r) for r in requests) == ["queued", "started"]


def test_a_stale_wake_row_whose_goal_is_gone_is_retired_too(tmp_path):
    service, backends, agent_id, plan, request, text = _admitted_wake(tmp_path)
    _stale_row(agent_id, request, text, handoff=False)
    current = task_plans.get(plan["plan_id"])
    task_goal_state.mutate(plan["plan_id"], revision=current["revision"], action="pause",
                           data={"reason": "User paused this goal"})

    retired = []

    assert _tick(service, offset_ms=HOUR_MS, retired=retired.append) == 0

    assert turn_queue.status(request) == "cancelled"
    assert turn_queue.holds_autonomous_wake(agent_id) is False
    assert _wakes(backends) == []
    # The apps drop the phantom queued message: the pass reports the row.
    assert [(row["queue_id"], row["agent_id"]) for row in retired] == [(request, agent_id)]


def test_a_row_that_can_no_longer_dispatch_does_not_hold_goal_wakes(tmp_path):
    """Not only goal rows: a peer message left ``queued`` by a turn that
    ended with its launch unproven is kept (its payload is not the
    scheduler's to drop) but is no reason to stop waking the goal."""
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())
    then = db.now_ms() - HOUR_MS
    turn_queue.enqueue(
        queue_id="peer-1", agent_id=agent_id, session="mike", text="peer says hello",
        trace_id="peer-trace", client_msg_id="peer-1", synthesize_audio=False,
        origin="agent", sender_agent_id="someone")
    _turn(agent_id, "peer-trace", then, settled=then + 1000, outcome="failed")
    before = turn_queue.get("peer-1")
    assert turn_queue.pending_for_dispatch() == []

    assert _tick(service) == 1

    assert len(_wakes(backends)) == 1
    assert turn_queue.get("peer-1") == before
    assert _history(plan, "wake_undelivered") == []


@pytest.mark.parametrize("case", ["waiting", "just-ended", "open-turn"])
def test_queued_work_that_can_still_run_holds_the_wake(tmp_path, case):
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())
    turn_queue.enqueue(
        queue_id="peer-1", agent_id=agent_id, session="mike", text="peer says hello",
        trace_id="peer-trace", client_msg_id="peer-1", synthesize_audio=False,
        origin="agent", sender_agent_id="someone")
    now = db.now_ms()
    if case == "just-ended":   # a failed handoff retries its head within a minute
        _turn(agent_id, "peer-trace", now - 5000, ended=now - 1000)
    elif case == "open-turn":
        _turn(agent_id, "peer-trace", now - HOUR_MS)

    assert task_goal_recovery.tick(lambda *_: pytest.fail("woke"), now=now) == 0

    observed = _continuation(plan)
    assert (observed["observed_state"], observed["observed_reason"]) == (
        "working", "Owner has live or queued work")
    assert turn_queue.status("peer-1") == "queued"


def test_a_live_wake_row_is_never_retired_by_the_pass(tmp_path, monkeypatch):
    """A wake still parked for an account has an open turn: it is live."""
    host = _exhausted_pool(tmp_path, monkeypatch)
    plan, request = _parked_wake(host)
    db.conn().execute("UPDATE turns SET started_at = started_at - ? WHERE trace_id = ?",
                      (HOUR_MS, request))

    assert _tick(host.service, offset_ms=HOUR_MS) == 0

    assert turn_queue.status(request) == "queued"
    observed = _continuation(plan)
    assert (observed["observed_state"], observed["observed_reason"]) == ("capacity", WAITING)
    assert _history(plan, "wake_undelivered") == []


def test_wakes_that_keep_failing_end_in_attention_that_says_why(tmp_path):
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = task_plans.create(
        session="mike", title="Finish the outcome", items=[{"id": "probe", "title": "Probe"}],
        goal={"criteria": ["Verified result"], "limits": "No deployment", "enroll": True,
              "recovery_limit": 1})
    plan = _arm(plan)
    assert _tick(service) == 1
    request = _continuation(plan)["request_id"]
    backends.live = False
    _wakes(backends)[0]["on_error"]("boom: the runner died")
    assert turn_queue.status(request) == "failed"

    assert _tick(service, offset_ms=HOUR_MS) == 0

    stuck = _continuation(plan)
    assert stuck["state"] == "attention"
    assert "Recovery attempt limit reached" in stuck["reason"]
    assert "not delivered" in stuck["reason"]
    assert [entry["detail"]["request_id"]
            for entry in _history(plan, "wake_undelivered")] == [request]
