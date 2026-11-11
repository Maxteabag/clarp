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
import re
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

    # The scheduler sees its wake did not complete and says so, where the
    # owner and the apps read the goal: it ran and met the usage limit.
    assert _tick(host.service) == 0
    failed = _history(plan, "wake_failed")
    assert [entry["detail"]["request_id"] for entry in failed] == [request]
    assert "Usage limit" in failed[0]["detail"]["reason"]
    assert (failed[0]["detail"]["started"], failed[0]["detail"]["code"]) == (True, "usage_limit")
    # It started, so it is not "not delivered"; and the provider's own words stay.
    assert _continuation(plan)["reason"].startswith(
        "Wake started and failed: no account with usage (You've hit your session limit); "
        "next try ")
    # In the append-only ledger too, as the Host's own event: a bookkeeping
    # delegate on the goal must never be able to refuse it.
    ledger = db.conn().execute(
        "SELECT actor_kind, reason FROM goal_events WHERE plan_id=? AND kind='wake_failed'",
        (plan["plan_id"],)).fetchall()
    assert [tuple(row) for row in ledger] == [
        ("system", failed[0]["detail"]["reason"])]

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
    assert undelivered[0]["detail"]["reason"] == "Agent process exited unexpectedly: " + WAITING
    requests = [_continuation(p)["request_id"] for p in (plan, other)]
    assert request not in requests and _continuation(plan)["attempts"] == 2
    # The older runtime called it a runner exit, but the wake had waited for an
    # account and was refused with the park's own message: lack of usage, which
    # does not use the goal's recovery limit.
    assert undelivered[0]["detail"]["code"] == "usage_limit"
    assert _continuation(plan)["capacity_failures"] == 1
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
    assert stuck["reason"] == (
        "Recovery attempt limit reached; the last wake started and failed "
        "(Turn failed: boom: the runner died); checkpoint or resume to continue")
    assert [entry["detail"]["request_id"]
            for entry in _history(plan, "wake_failed")] == [request]


# --- a turn that never closed (the review of 258, finding 1) ---------------------


def _one_broken_spawn(backends):
    """The backend cannot start once, then works again."""
    spawn = backends.spawn_turn

    def broken(*_args, **_kwargs):
        backends.spawn_turn = spawn
        raise OSError("claude binary went away")

    backends.spawn_turn = broken


def test_a_wake_whose_backend_fails_to_spawn_closes_its_turn_and_is_tried_again(tmp_path):
    """spawn_turn raises on the wake's first dispatch. Nothing can prove at
    which phase, so no "never started" marker is written. The turn used to
    stay open and the row ``queued``: recovery skipped it for good, and an
    open turn read as a running one."""
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())
    _one_broken_spawn(backends)

    assert _tick(service) == 0           # the dispatch raised
    wake = _continuation(plan)
    request = wake["request_id"]
    assert wake["state"] == "retry" and "spawn failed" in wake["reason"]
    turns = db.conn().execute(
        "SELECT ended_at FROM turns WHERE agent_id=? AND trace_id=?", (agent_id, request)).fetchall()
    assert len(turns) == 1 and turns[0]["ended_at"] is not None, "the abandoned turn stays open"
    assert turn_queue.pending_count(agent_id) == 0
    assert turn_queue.status(request) == "cancelled"
    assert _td._INFLIGHT.get(agent_id) is None

    # The next pass after the backoff uses a new receipt (the spent one could
    # never launch) and the wake runs.
    assert _tick(service, offset_ms=HOUR_MS) == 1
    again = _continuation(plan)
    assert again["request_id"] != request and len(_wakes(backends)) == 1
    assert turn_queue.status(again["request_id"]) == "started"
    undelivered = _history(plan, "wake_undelivered")
    assert [entry["detail"]["request_id"] for entry in undelivered] == [request]
    assert undelivered[0]["detail"]["started"] is False


class _Killed(BaseException):
    """Not an Exception: it passes the dispatcher's spawn-error handling."""


def test_an_abandoned_launch_with_a_live_process_keeps_its_turn_and_its_row(tmp_path):
    """The backend raised after a process existed. Nobody can prove what was
    launched, but something of this agent runs: the turn is not closed and
    the wake's row is not retired, or the wake would be sent a second time
    beside the process that has it."""
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())

    def started_then_raised(*_args, **_kwargs):
        backends.live = True              # the process is there
        raise _Killed("interrupted while the backend was starting")

    backends.spawn_turn = started_then_raised
    with pytest.raises(_Killed):
        _tick(service)

    request = db.conn().execute(
        "SELECT queue_id FROM queued_turns WHERE agent_id=?", (agent_id,)).fetchone()["queue_id"]
    turn = db.conn().execute(
        "SELECT ended_at, settled_at FROM turns WHERE agent_id=? AND trace_id=?",
        (agent_id, request)).fetchone()
    assert (turn["ended_at"], turn["settled_at"]) == (None, None)
    assert turn_queue.status(request) == "queued"


def _retry_wake(tmp_path):
    """A goal whose dispatch raised six hours ago, as the scheduler recorded it."""
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    sent = []

    def raised(session, text, request):
        sent.append((request, text))
        raise RuntimeError("claude spawn failed: claude binary went away")

    real = db.now_ms
    with pytest.MonkeyPatch.context() as earlier:
        earlier.setattr(db, "now_ms", lambda: real() - STALE_AGE_MS)
        plan = _arm(_goal())
        assert task_goal_recovery.tick(raised, now=db.now_ms()) == 0
    request, text = sent[0]
    assert _continuation(plan)["state"] == "retry"
    return service, backends, agent_id, plan, request, text


def _never_closed_row(agent_id, request, text, *, later_turn_age_ms=5 * HOUR_MS):
    """What the unfixed runtime left: the row queued, its turn opened and
    never ended or settled, no spawn record. The agent has run other turns
    since (or not, with ``later_turn_age_ms=None``)."""
    start = db.now_ms() - STALE_AGE_MS + 47
    turn_queue.enqueue(
        queue_id=request, agent_id=agent_id, session="mike", text=text,
        trace_id=request, client_msg_id=request, synthesize_audio=False,
        origin="automation", sender_agent_id="")
    db.conn().execute("UPDATE queued_turns SET enqueued_at=? WHERE queue_id=?", (start, request))
    _turn(agent_id, request, start + 1)
    if later_turn_age_ms is not None:
        then = db.now_ms() - later_turn_age_ms
        _turn(agent_id, "a-later-user-turn", then, ended=then + 60_000, settled=then + 60_000,
              outcome="completed")
    row = turn_queue.get(request)
    assert turn_queue._launch_status(row) == "unknown"
    assert turn_queue.pending_for_dispatch() == []


def test_a_wake_row_whose_turn_never_closed_heals_once_the_agent_has_moved_on(tmp_path):
    service, backends, agent_id, plan, request, text = _retry_wake(tmp_path)
    other = _arm(_goal(title="A second goal of the same agent"))
    _never_closed_row(agent_id, request, text)

    assert _tick(service, offset_ms=HOUR_MS) == 2

    assert turn_queue.status(request) == "cancelled"
    requests = [_continuation(p)["request_id"] for p in (plan, other)]
    assert request not in requests
    assert sorted(turn_queue.status(r) for r in requests) == ["queued", "started"]
    assert [entry["detail"]["request_id"]
            for entry in _history(plan, "wake_undelivered")] == [request]


@pytest.mark.parametrize("case", ["no-later-turn", "later-turn-just-started", "slot-held"])
def test_an_open_turn_is_over_only_when_nothing_holds_it_and_the_agent_moved_on(
        tmp_path, case):
    service, backends, agent_id, plan, request, text = _retry_wake(tmp_path)
    _never_closed_row(agent_id, request, text, later_turn_age_ms={
        "no-later-turn": None, "later-turn-just-started": 30_000, "slot-held": 5 * HOUR_MS}[case])
    if case == "slot-held":
        _td._SLOTS.claim(agent_id, request)     # the runtime still owns this very turn

    assert task_goal_recovery.tick(lambda *_: pytest.fail("woke"), now=db.now_ms()) == 0

    assert turn_queue.status(request) == "queued"
    assert turn_queue.dead_rows(live_trace=_td.owns_inflight_trace) == []
    if case == "slot-held":
        _td._SLOTS.release(agent_id, request)


def test_a_parked_wake_stays_live_whatever_else_the_agent_opened(tmp_path, monkeypatch):
    """The bound on the rule above: a later turn row of the same agent can
    exist while the first is alive (a hook inside a running process mints a
    new trace), so a later turn alone proves nothing. A turn that runs or is
    parked holds its agent's slot until it finishes; that is what decides."""
    host = _exhausted_pool(tmp_path, monkeypatch)
    plan, request = _parked_wake(host)
    db.conn().execute("UPDATE turns SET started_at = started_at - ? WHERE trace_id = ?",
                      (2 * HOUR_MS, request))
    then = db.now_ms() - HOUR_MS
    _turn(host.agent_id, "hook-minted-trace", then, ended=then + 1000, settled=then + 1000,
          outcome="completed")
    assert _td.owns_inflight_trace(host.agent_id, request)

    assert _tick(host.service, offset_ms=HOUR_MS) == 0

    assert turn_queue.status(request) == "queued"
    _end_park(host, account_available=True)       # and it still runs when an account is back
    assert [call["trace_id"] for call in _wakes(host.backends)] == [request]


def test_an_open_turn_that_was_spawned_is_left_to_its_receipt(tmp_path):
    """A spawn was recorded for it: it ran, and may still. Its receipt is
    converged by the receipt reconciler, never retired as a wake that did
    not start."""
    service, backends, agent_id, plan, request, text = _retry_wake(tmp_path)
    _never_closed_row(agent_id, request, text)
    _state(agent_id, "thinking", db.now_ms() - STALE_AGE_MS + 60, {
        "source": "pwa", "dispatch": "claude", "origin": "automation", "trace_id": request})
    assert turn_queue._launch_status(turn_queue.get(request)) == "observed"

    assert turn_queue.dead_rows(live_trace=lambda *_: False) == []


def test_only_a_later_turn_of_the_agent_counts_as_moved_on(tmp_path):
    service, backends, agent_id, plan, request, text = _retry_wake(tmp_path)
    then = db.now_ms() - 20 * HOUR_MS
    _turn(agent_id, "an-earlier-turn", then, ended=then + 1000, settled=then + 1000,
          outcome="completed")
    _never_closed_row(agent_id, request, text, later_turn_age_ms=None)

    assert turn_queue.dead_rows(live_trace=lambda *_: False) == []


def _peer_row_with_a_never_closed_turn(agent_id):
    """A peer message whose turn was opened and never closed, on an agent
    that has run other turns since. Not a goal row: the pass never retires it,
    so only the scheduler's own count decides whether it holds the wake."""
    then = db.now_ms() - STALE_AGE_MS
    turn_queue.enqueue(
        queue_id="peer-1", agent_id=agent_id, session="mike", text="peer says hello",
        trace_id="peer-trace", client_msg_id="peer-1", synthesize_audio=False,
        origin="agent", sender_agent_id="someone")
    _turn(agent_id, "peer-trace", then)
    later = db.now_ms() - HOUR_MS
    _turn(agent_id, "a-later-turn", later, ended=later + 1000, settled=later + 1000,
          outcome="completed")


def test_the_scheduler_asks_about_the_slot_before_it_stops_counting_an_open_turn(tmp_path):
    """Nothing holds the peer row's turn: it is not the owner's queued work,
    and the goal wakes."""
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())
    _peer_row_with_a_never_closed_turn(agent_id)

    assert _tick(service) == 1

    assert len(_wakes(backends)) == 1 and turn_queue.status("peer-1") == "queued"


def test_the_scheduler_keeps_counting_an_open_turn_whose_slot_is_held(tmp_path):
    """The same row while the runtime still holds its turn (parked for an
    account, say): the owner has queued work, and the goal waits."""
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())
    _peer_row_with_a_never_closed_turn(agent_id)
    _td._SLOTS.claim(agent_id, "peer-trace")
    # Past its spawn: no process, no terminal, nothing spawning. The slot is
    # the only thing that says this turn is still someone's.
    _td._SLOTS.mark_spawned(agent_id, "peer-trace")
    from lib import reconcile
    assert not reconcile.has_live_work(agent_id, "claude")
    try:
        assert task_goal_recovery.tick(lambda *_: pytest.fail("woke"), now=db.now_ms()) == 0
    finally:
        _td._SLOTS.release(agent_id, "peer-trace")

    assert _continuation(plan)["observed_state"] == "working"


class _Status:
    """A runtime client whose status answer is given."""
    def __init__(self, answer):
        self.answer = answer

    def status(self, **_kwargs):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize("answer,held", [
    ({"spawning": []}, True),                        # no active map at all
    ({"active": ["agent"]}, True),                   # a list of agents, no traces
    ({"active": {"AGENT": None}}, True),             # the agent, but no trace for it
    ({"active": {"AGENT": ["trace"]}}, True),
    ({"active": {}}, False),                         # a usable answer: nothing holds it
    ({"active": {"AGENT": "another-trace"}}, False), # another turn holds the slot
    ({"active": {"AGENT": "TRACE"}}, True),
], ids=["no-active", "active-is-a-list", "no-trace", "trace-is-a-list", "empty", "other-trace",
        "this-trace"])
def test_a_status_answer_that_cannot_be_read_counts_as_held(tmp_path, monkeypatch, answer, held):
    """The Host asks the runtime whether it still holds an agent's slot for a
    trace. An answer without a usable agent-to-trace map is no answer, and no
    answer never frees a row."""
    service, backends, agent_id, plan, request, text = _retry_wake(tmp_path)
    _never_closed_row(agent_id, request, text)
    answer = json.loads(json.dumps(answer).replace("AGENT", agent_id).replace("TRACE", request))
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", _Status(answer))

    assert _td.owns_inflight_trace(agent_id, request) is held
    dead = [row["queue_id"] for row in turn_queue.dead_rows(live_trace=_td.owns_inflight_trace)]
    assert dead == ([] if held else [request])


def test_a_liveness_check_that_cannot_answer_leaves_the_row(tmp_path):
    service, backends, agent_id, plan, request, text = _retry_wake(tmp_path)
    _never_closed_row(agent_id, request, text)

    def unreachable(_agent_id, _trace_id):
        raise OSError("runtime socket is gone")

    assert turn_queue.dead_rows(live_trace=unreachable) == []
    assert turn_queue.dead_rows() == []            # no liveness source: open means open
    assert [row["queue_id"] for row in turn_queue.dead_rows(
        live_trace=lambda *_: False)] == [request]


# --- properties of dead_rows and the boundary (the review's surviving mutants) ---


def _handed_over(agent_id, request, start):
    first = _turn(agent_id, request, start + 1, ended=start + 400_000)
    _state(agent_id, "idle", start + 400_000, {
        "reason": "update_handoff", "trace_id": request, "dispatch_not_started": True,
        "dispatch_not_started_through_turn_id": first, "origin": "automation"})
    return first


def _queued_wake_row(tmp_path):
    service, backends, agent_id, plan, request, text = _admitted_wake(tmp_path)
    turn_queue.enqueue(
        queue_id=request, agent_id=agent_id, session="mike", text=text,
        trace_id=request, client_msg_id=request, synthesize_audio=False,
        origin="automation", sender_agent_id="")
    return agent_id, request, db.now_ms() - STALE_AGE_MS


def _never_dead(request):
    assert turn_queue.dead_rows(live_trace=_td.owns_inflight_trace) == []
    task_goal_recovery._retire_dead_wakes(db.now_ms())
    assert turn_queue.status(request) == "queued"


def test_a_wake_an_update_handed_over_and_recovery_will_relaunch_is_not_dead(tmp_path):
    agent_id, request, start = _queued_wake_row(tmp_path)
    _handed_over(agent_id, request, start)
    assert turn_queue._launch_status(turn_queue.get(request)) == "handoff"
    assert [row["queue_id"] for row in turn_queue.pending_for_dispatch()] == [request]
    _never_dead(request)


def test_a_wake_with_proof_that_it_never_launched_is_not_dead(tmp_path):
    agent_id, request, start = _queued_wake_row(tmp_path)
    turn = _turn(agent_id, request, start + 1, settled=start + 5000, outcome="failed")
    _state(agent_id, "interrupted", start + 5000, {
        "trace_id": request, "dispatch_not_started": True,
        "dispatch_not_started_turn_id": turn, "message": "Message saved; backend did not start."})
    assert turn_queue._launch_status(turn_queue.get(request)) == "retryable"
    _never_dead(request)


def test_only_the_newest_turn_of_the_trace_decides(tmp_path):
    """A wake parked again after an update handoff: its first turn is long
    over, its newest is open and waiting for an account."""
    agent_id, request, start = _queued_wake_row(tmp_path)
    _handed_over(agent_id, request, start)
    _turn(agent_id, request, start + 400_013)
    _state(agent_id, "thinking", start + 400_014, {
        "dispatch": "claude", "trace_id": request, "account_recovery": "waiting",
        "message": WAITING})
    _never_dead(request)


def test_a_dead_row_of_another_agent_does_not_free_this_agents_queue(tmp_path):
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())
    other = agents_db.create_agent(persona="Bella", voice_id="V", cwd=str(tmp_path),
                                   session="bella", backend="claude")
    then = db.now_ms() - HOUR_MS
    turn_queue.enqueue(queue_id="dead-1", agent_id=other, session="bella", text="x",
                       trace_id="dead-trace", client_msg_id="dead-1", synthesize_audio=False,
                       origin="agent", sender_agent_id="someone")
    _turn(other, "dead-trace", then, settled=then + 1000, outcome="failed")
    turn_queue.enqueue(queue_id="live-1", agent_id=agent_id, session="mike", text="y",
                       trace_id="live-trace", client_msg_id="live-1", synthesize_audio=False,
                       origin="agent", sender_agent_id="someone")
    assert [row["queue_id"] for row in turn_queue.dead_rows()] == ["dead-1"]

    assert task_goal_recovery.tick(lambda *_: pytest.fail("woke"), now=db.now_ms()) == 0

    assert _continuation(plan)["observed_state"] == "working"


def test_only_the_wakes_own_park_marker_is_exempt(tmp_path, monkeypatch):
    host = _exhausted_pool(tmp_path, monkeypatch)
    plan, request = _parked_wake(host)
    row = db.conn().execute(
        "SELECT * FROM task_plans WHERE plan_id=?", (plan["plan_id"],)).fetchone()
    goal = json.loads(row["goal_json"])

    assert task_goal_recovery.boundary(row, goal, check_live=False, wake_request=request) is None
    assert task_goal_recovery.boundary(
        row, goal, check_live=False, wake_request="task-goal-another-wake") == (
            "capacity", WAITING)
    assert task_goal_recovery.boundary(row, goal, check_live=False) == ("capacity", WAITING)


# --- the recovery budget ------------------------------------------------------


USAGE_WINDOW = "You've hit your session limit · resets 3:20pm (Europe/Oslo)"


def _every_wake_fails(service, backends, plan, messages, *, hours, step_ms=15_000):
    """Run the scheduler for ``hours`` with every wake ending at once: the
    n-th wake fails on the n-th of ``messages`` (repeating its last entry),
    or completes where that entry is None. Returns the minutes at which wakes
    were dispatched and the minute the goal reached attention (None if it
    never did)."""
    if isinstance(messages, str):
        messages = [messages]
    dispatched, minute = [], None
    for step in range(hours * 3_600_000 // step_ms + 1):
        offset = step * step_ms
        if _tick(service, offset_ms=offset):
            wakes = _wakes(backends)
            assert len(wakes) == len(dispatched) + 1
            message = messages[min(len(dispatched), len(messages) - 1)]
            real = db.now_ms
            with pytest.MonkeyPatch.context() as later:
                later.setattr(db, "now_ms", lambda: real() + offset)
                if message is None:
                    wakes[-1]["on_result"]({"duration_ms": 5})
                else:
                    wakes[-1]["on_error"](message)
            dispatched.append(round(offset / 60_000))
            backends.live = False
        if _continuation(plan)["state"] == "attention":
            minute = round(offset / 60_000)
            break
    return dispatched, minute


def test_a_wake_that_fails_for_lack_of_usage_does_not_use_the_recovery_budget(tmp_path):
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())                      # the default limit of five

    dispatched, attention = _every_wake_fails(
        service, backends, plan, "You've hit your session limit", hours=8)

    assert attention is None, f"attention after {attention} minutes"
    # Its backoff still grows, to an hour between tries.
    assert dispatched[:7] == [0, 2, 6, 14, 30, 62, 122] and len(dispatched) >= 10
    wake = _continuation(plan)
    assert wake["state"] == "admitted" and wake["attempts"] == len(dispatched)
    assert wake["capacity_failures"] == len(dispatched)
    # The ceiling's clock runs from the dispatch of the first wake that failed.
    first_dispatch = wake["last_dispatch_at"] - dispatched[-1] * 60_000
    assert abs(wake["uncompleted_since"] - first_dispatch) < 60_000
    assert re.fullmatch(
        r"Wake started and failed: no account with usage \(You've hit your session limit\); "
        r"next try \d\d:\d\d", wake["reason"]), wake["reason"]
    assert {entry["detail"]["code"] for entry in _history(plan, "wake_failed")} == {
        "usage_limit"}

    # The owner's next checkpoint starts both counts again.
    current = task_plans.get(plan["plan_id"])
    task_goal_state.mutate(
        current["plan_id"], revision=current["revision"], action="checkpoint",
        data={"progress": "Usage is back", "next_work": "Carry on",
              "continuation": {"kind": "timer", "due_at": db.now_ms()}})
    assert _tick(service, offset_ms=9 * HOUR_MS) == 1
    fresh = _continuation(plan)
    assert fresh["attempts"] == 1 and not fresh.get("capacity_failures")
    assert "uncompleted_since" not in fresh    # and the ceiling's clock


def test_other_failures_reach_attention_after_five_wakes_in_62_minutes(tmp_path):
    """The figures docs/architecture/durable-goals.md gives."""
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())

    dispatched, attention = _every_wake_fails(
        service, backends, plan, "boom: the runner died", hours=3)

    assert dispatched == [0, 2, 6, 14, 30] and attention == 62
    assert "the last wake started and failed" in _continuation(plan)["reason"]


def test_capacity_failures_between_others_do_not_shorten_the_budget(tmp_path):
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = task_plans.create(
        session="mike", title="Finish the outcome", items=[{"id": "probe", "title": "Probe"}],
        goal={"criteria": ["Verified result"], "limits": "No deployment", "enroll": True,
              "recovery_limit": 2})
    plan = _arm(plan)

    dispatched, attention = _every_wake_fails(
        service, backends, plan,
        ["You've hit your session limit", "boom: the runner died"] * 2, hours=4)

    # Two wakes that count, two that do not: four dispatched before attention.
    assert len(dispatched) == 4 and attention is not None
    wake = _continuation(plan)
    assert (wake["attempts"], wake["capacity_failures"]) == (4, 2)


# What the classifier also files under "usage limit" but no reset will clear:
# depleted credits, payment, a suspended account, hard limits, a trial, a model
# that refuses this client. A person has to act; waiting does nothing.
NEEDS_A_PERSON = [
    "agy exited rc=1: Your AI credits balance is too low to continue.",
    "Your workspace is out of credits. Ask your workspace owner to refill in order to continue.",
    "API error (status 402 Payment Required)",
    "Account acct-1 is suspended, possibly due to reaching the monthly spending limit",
    "You have reached your billing hard limit",
    "Your trial quota is used up for this model",
    "free tier can only be used from within opencode",
]


@pytest.mark.parametrize("message", NEEDS_A_PERSON, ids=[
    "credits-too-low", "out-of-credits", "402-payment", "monthly-spending-limit",
    "billing-hard-limit", "trial-quota", "opencode-free-tier"])
def test_a_limit_that_no_reset_clears_uses_the_budget_and_says_what_to_do(tmp_path, message):
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())

    dispatched, attention = _every_wake_fails(service, backends, plan, message, hours=12)

    assert dispatched == [0, 2, 6, 14, 30] and attention == 62
    stuck = _continuation(plan)
    assert not stuck.get("capacity_failures")
    assert "no account with usage" not in stuck["reason"]
    assert stuck["reason"].startswith("Recovery attempt limit reached; the last wake started and failed (")
    assert stuck["reason"].endswith(
        "a person has to pay or raise the limit, or pick another model or account, "
        "then resume the goal")
    assert {entry["detail"]["code"] for entry in _history(plan, "wake_failed")} == {"billing"}


def test_credits_the_host_knows_are_depleted_count_whatever_the_error_says(
        tmp_path, monkeypatch):
    """The provider's own statement (backend_usage: credits_depleted) wins
    over an error text that reads like an ordinary usage window."""
    from lib import backend_usage
    monkeypatch.setattr(backend_usage, "exhausted_backends", lambda: {
        "claude": {"state": "exhausted", "provider_id": "claude", "reason": "credits_depleted"}})
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())

    dispatched, attention = _every_wake_fails(service, backends, plan, USAGE_WINDOW, hours=3)

    assert dispatched == [0, 2, 6, 14, 30] and attention == 62
    assert {entry["detail"]["code"] for entry in _history(plan, "wake_failed")} == {"billing"}


DAY_MIN = 24 * 60


def test_a_goal_none_of_whose_wakes_completed_in_8_days_reaches_attention(tmp_path):
    """The net under the exemption. A real usage window lasts up to a week
    and must not reach attention; a wait longer than any window is not a
    window, whatever the failure said."""
    assert task_goal_recovery.WAKE_COMPLETION_CEILING_MS == 8 * 24 * HOUR_MS
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())

    dispatched, attention = _every_wake_fails(
        service, backends, plan, USAGE_WINDOW, hours=9 * 24, step_ms=300_000)

    # Hourly tries once the backoff has grown: the first one past the 8th day.
    assert attention is not None and 8 * DAY_MIN <= attention <= 8 * DAY_MIN + 65, attention
    stuck = _continuation(plan)
    assert stuck["reason"] == (
        "No wake of this goal has completed in 8 days; the last wake found no account with "
        "usage (Usage limit reached: You've hit your session limit · resets 3:20pm "
        "(Europe/Oslo)); a person has to find out why, then resume the goal")
    assert stuck["capacity_failures"] == len(dispatched)


def test_a_week_without_usage_does_not_reach_attention(tmp_path):
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())

    dispatched, attention = _every_wake_fails(
        service, backends, plan, USAGE_WINDOW, hours=7 * 24, step_ms=300_000)

    assert attention is None and len(dispatched) > 150
    assert _continuation(plan)["state"] == "admitted"


def test_a_wake_that_starts_and_fails_does_not_reset_the_ceiling_clock(tmp_path):
    """Only a wake that completes ends the streak. One that starts and then
    fails, for whatever reason, is one more failure in it."""
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())
    # About three days without usage, one wake that starts and crashes (it
    # counts once against the budget of five), then no usage again.
    outage = [USAGE_WINDOW] * 80

    dispatched, attention = _every_wake_fails(
        service, backends, plan, [*outage, "boom: the runner died", USAGE_WINDOW],
        hours=9 * 24, step_ms=300_000)

    crashed_at = dispatched[80]
    assert 2 * DAY_MIN <= crashed_at <= 4 * DAY_MIN
    # Eight days after the FIRST failure, not after the crash.
    assert attention is not None and 8 * DAY_MIN <= attention <= 8 * DAY_MIN + 65, attention
    stuck = _continuation(plan)
    assert stuck["reason"].startswith("No wake of this goal has completed in 8 days; ")
    assert stuck["attempts"] - stuck["capacity_failures"] == 1


def test_a_wake_that_completes_resets_the_ceiling_clock(tmp_path):
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())
    # About three days without usage, one wake that runs to its end (without
    # a checkpoint), then no usage again.
    outage = [USAGE_WINDOW] * 80

    dispatched, attention = _every_wake_fails(
        service, backends, plan, [*outage, None, USAGE_WINDOW], hours=10 * 24,
        step_ms=300_000)

    ran_at = dispatched[80]
    assert 2 * DAY_MIN <= ran_at <= 4 * DAY_MIN
    # Nothing 8 days after the first failure; only 8 days after the first
    # failure that followed the wake that completed.
    assert attention is None, (ran_at, attention)
    wake = _continuation(plan)
    streak = wake["last_dispatch_at"] - wake["uncompleted_since"]
    assert streak < (10 * DAY_MIN - ran_at) * 60_000


def test_the_clock_is_cleared_when_the_completed_wake_is_seen_not_guessed(tmp_path):
    """A wake whose turn ended without a recorded outcome did not complete:
    the absence of a failure is not a completion."""
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())
    assert _tick(service) == 1
    _wakes(backends)[0]["on_error"](USAGE_WINDOW)
    backends.live = False
    assert _tick(service) == 0
    since = _continuation(plan)["uncompleted_since"]

    # The next wake's turn is closed with no outcome (a launch nobody settled).
    assert _tick(service, offset_ms=HOUR_MS) == 1
    request = _continuation(plan)["request_id"]
    db.conn().execute("UPDATE turns SET ended_at=? WHERE agent_id=? AND trace_id=?",
                      (db.now_ms(), agent_id, request))
    backends.live = False
    _td._SLOTS.release(agent_id, request)
    _tick(service, offset_ms=3 * HOUR_MS)
    assert _continuation(plan)["uncompleted_since"] == since

    # And one that does complete clears it.
    request = _continuation(plan)["request_id"]
    real = db.now_ms
    with pytest.MonkeyPatch.context() as later:
        later.setattr(db, "now_ms", lambda: real() + 3 * HOUR_MS)
        _wakes(backends)[-1]["on_result"]({"duration_ms": 5})
    backends.live = False
    _tick(service, offset_ms=3 * HOUR_MS + 60_000)
    assert "uncompleted_since" not in _continuation(plan)


class _Unconfirmed(str):
    """What the Codex runner hands over when the provider did not confirm
    that the quota is exhausted."""
    quota_confirmed = False


def test_a_codex_limit_the_provider_did_not_confirm_is_not_exempt(tmp_path):
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())

    dispatched, attention = _every_wake_fails(
        service, backends, plan, _Unconfirmed("You've hit your usage limit"), hours=6)

    assert dispatched == [0, 2, 6, 14, 30] and attention == 62
    stuck = _continuation(plan)
    assert not stuck.get("capacity_failures")
    assert "Codex could not complete this request" in stuck["reason"]
    assert {entry["detail"]["code"] for entry in _history(plan, "wake_failed")} == {
        "unconfirmed_limit"}


@pytest.mark.parametrize("confirmed", [False, True])
def test_a_backend_that_records_its_limits_is_confirmed_by_the_event_not_the_wording(
        tmp_path, monkeypatch, confirmed):
    """Codex records a limit event for a quota the provider confirmed, and the
    failed state carries its id. Without the id the limit is unconfirmed,
    whatever the message says."""
    from lib import backends as registry
    backend = registry.by_id("claude")
    monkeypatch.setattr(backend, "records_usage_limits", lambda: True, raising=False)
    monkeypatch.setattr(backend, "classify_usage_limit", lambda message, **_kw: (
        {"provider_limit_event_id": "limit-event-1", "_new": False} if confirmed else None))
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())

    dispatched, attention = _every_wake_fails(service, backends, plan, USAGE_WINDOW, hours=3)

    codes = {entry["detail"]["code"] for entry in _history(plan, "wake_failed")}
    if confirmed:
        assert attention is None and codes == {"usage_limit"}
    else:
        assert attention == 62 and codes == {"unconfirmed_limit"}


def test_a_plan_without_the_model_counts_and_asks_for_a_person(tmp_path):
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = task_plans.create(
        session="mike", title="Finish the outcome", items=[{"id": "probe", "title": "Probe"}],
        goal={"criteria": ["Verified result"], "limits": "No deployment", "enroll": True,
              "recovery_limit": 1})
    plan = _arm(plan)
    assert _tick(service) == 1
    _wakes(backends)[0]["on_error"]("This model is not included in your plan")
    backends.live = False

    assert _tick(service, offset_ms=HOUR_MS) == 0

    stuck = _continuation(plan)
    assert _history(plan, "wake_failed")[0]["detail"]["code"] == "account_plan"
    assert not stuck.get("capacity_failures")
    assert stuck["state"] == "attention"
    assert stuck["reason"].endswith(
        "a person has to switch to an account whose plan includes the model, or choose "
        "another model, then resume the goal")


# --- how tight the reading of an older runtime's park refusal is -----------------


def _old_refusal(tmp_path, **detail_changes):
    """The stale row an older runtime left, optionally with its terminal state
    altered."""
    service, backends, agent_id, plan, request, text = _admitted_wake(tmp_path)
    _stale_row(agent_id, request, text, handoff=False)
    for key, value in detail_changes.items():
        db.conn().execute(
            "UPDATE state_log SET detail = json_set(detail, ?, ?) "
            "WHERE agent_id=? AND kind='interrupted'", (f"$.{key}", value, agent_id))
    return service, plan, request


def _noted(plan):
    return (_history(plan, "wake_undelivered") + _history(plan, "wake_failed"))[0]["detail"]


def test_a_wake_an_older_runtime_refused_reads_not_delivered(tmp_path):
    """It never started, so "not delivered" is the word; there are no
    provider's words to quote. Read while the owner is busy, so that the next
    dispatch has not replaced the reason yet."""
    service, plan, request = _old_refusal(tmp_path)
    agent_id = task_plans.get(plan["plan_id"])["agent_id"]
    turn_queue.enqueue(
        queue_id="live-1", agent_id=agent_id, session="mike", text="queued behind",
        trace_id="live-trace", client_msg_id="live-1", synthesize_audio=False,
        origin="agent", sender_agent_id="someone")

    assert _tick(service, offset_ms=HOUR_MS) == 0

    assert _noted(plan)["code"] == "usage_limit" and _noted(plan)["started"] is False
    assert _continuation(plan)["reason"] == (
        "Wake not delivered: no account with usage; next try now")


def test_the_park_text_is_lack_of_usage_only_for_a_wake_that_never_started(tmp_path):
    service, plan, request = _old_refusal(tmp_path)
    db.conn().execute("UPDATE queued_turns SET status='failed', started_at=? WHERE queue_id=?",
                      (db.now_ms() - HOUR_MS, request))

    _tick(service, offset_ms=HOUR_MS)

    assert (_noted(plan)["started"], _noted(plan)["code"]) == (True, "runner_exit")
    assert not _continuation(plan).get("capacity_failures")


def test_the_park_text_is_lack_of_usage_only_under_a_refusal_code(tmp_path):
    service, plan, request = _old_refusal(tmp_path, reason="timeout")

    _tick(service, offset_ms=HOUR_MS)

    assert _noted(plan)["code"] == "timeout"
    assert not _continuation(plan).get("capacity_failures")


def test_the_park_text_has_to_be_the_whole_error(tmp_path):
    service, plan, request = _old_refusal(
        tmp_path, error="claude spawn failed: " + WAITING + " (binary went away)")

    _tick(service, offset_ms=HOUR_MS)

    assert _noted(plan)["code"] == "runner_exit"
    assert not _continuation(plan).get("capacity_failures")


def test_a_sign_in_failure_counts_and_attention_says_a_person_is_needed(tmp_path):
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = task_plans.create(
        session="mike", title="Finish the outcome", items=[{"id": "probe", "title": "Probe"}],
        goal={"criteria": ["Verified result"], "limits": "No deployment", "enroll": True,
              "recovery_limit": 1})
    plan = _arm(plan)
    assert _tick(service) == 1
    _wakes(backends)[0]["on_error"]("OAuth session expired; please run /login")
    backends.live = False

    assert _tick(service, offset_ms=HOUR_MS) == 0

    stuck = _continuation(plan)
    assert stuck["state"] == "attention"
    assert "sign in" in stuck["reason"] and "person" in stuck["reason"]
    assert _history(plan, "wake_failed")[0]["detail"]["code"] == "auth"


# --- what the ledger says happened -----------------------------------------------


def test_a_wake_that_ran_and_failed_is_recorded_as_failed_not_as_undelivered(tmp_path):
    service, backends, agent_id = _goal_owner(tmp_path)
    backends.live = False
    plan = _arm(_goal())
    assert _tick(service) == 1
    request = _continuation(plan)["request_id"]
    _wakes(backends)[0]["on_error"]("boom: the runner died")
    backends.live = False

    assert _tick(service) == 0

    assert _history(plan, "wake_undelivered") == []
    failed = _history(plan, "wake_failed")
    assert [(e["detail"]["request_id"], e["detail"]["started"]) for e in failed] == [
        (request, True)]
    # Any failure begins the streak the 8-day ceiling measures.
    assert _continuation(plan)["uncompleted_since"] == _continuation(plan)["last_dispatch_at"]
    assert _continuation(plan)["reason"].startswith("Wake started and failed: ")
    kinds = [tuple(row) for row in db.conn().execute(
        "SELECT kind, actor_kind FROM goal_events WHERE plan_id=? AND kind LIKE 'wake_%' "
        "AND kind <> 'wake_claim'", (plan["plan_id"],))]
    assert kinds == [("wake_failed", "system")]


def test_a_wake_refused_at_the_end_of_its_park_says_it_never_started(tmp_path, monkeypatch):
    """The resume path used to report every refusal as "Agent process exited
    unexpectedly" (runner_exit), though no process had run."""
    host = _exhausted_pool(tmp_path, monkeypatch)
    plan, request = _parked_wake(host)
    spawn = host.backends.spawn_turn

    def refuses(*_args, **_kwargs):
        raise OSError("claude binary went away")

    host.backends.spawn_turn = refuses
    _end_park(host, account_available=True)
    host.backends.spawn_turn = spawn

    latest = agents_db.latest_state(host.agent_id)
    assert latest["kind"] == AgentState.INTERRUPTED
    assert latest["detail"]["reason"] == "not_started"
    assert "exited unexpectedly" not in latest["detail"]["message"]
    assert "claude binary went away" in latest["detail"]["error"]

    assert _tick(host.service) == 0
    undelivered = _history(plan, "wake_undelivered")
    assert [(e["detail"]["request_id"], e["detail"]["started"], e["detail"]["code"])
            for e in undelivered] == [(request, False, "not_started")]
    assert _continuation(plan)["reason"].startswith("Wake did not start: ")
    assert _history(plan, "wake_failed") == []


def test_a_wake_that_completed_is_never_reported_though_its_row_was_retired(tmp_path):
    service, backends, agent_id, plan, request, text = _admitted_wake(tmp_path)
    start = db.now_ms() - STALE_AGE_MS + 47
    turn_queue.enqueue(
        queue_id=request, agent_id=agent_id, session="mike", text=text,
        trace_id=request, client_msg_id=request, synthesize_audio=False,
        origin="automation", sender_agent_id="")
    _turn(agent_id, request, start, ended=start + 9000, settled=start + 9000, outcome="completed")
    assert turn_queue.cancel(request)

    _tick(service, offset_ms=HOUR_MS)

    assert _history(plan, "wake_undelivered") == [] and _history(plan, "wake_failed") == []


# --- the queue event reaches the apps ---------------------------------------------


def test_the_scheduler_runner_passes_retired_rows_to_its_callback(tmp_path):
    from lib import scheduler
    service, backends, agent_id, plan, request, text = _admitted_wake(tmp_path)
    _stale_row(agent_id, request, text, handoff=False)
    retired = []
    runner = scheduler.AgentScheduleRunner(
        dispatch_turn=lambda *_: None, dispatch_goal=lambda *_: SimpleNamespace(queued=False),
        goal_wake_retired=retired.append)

    runner.tick()

    assert [(row["queue_id"], row["agent_id"]) for row in retired] == [(request, agent_id)]


def test_the_adapter_tells_the_apps_that_the_row_left_the_queue(tmp_path):
    from lib.dispatch_adapters import DispatchAdapters
    service, backends, agent_id, plan, request, text = _admitted_wake(tmp_path)
    _stale_row(agent_id, request, text, handoff=False)
    row = turn_queue.get(request)
    assert turn_queue.cancel(request)
    sent = []
    adapters = DispatchAdapters(
        SimpleNamespace(runtime_client=None, stream=SimpleNamespace(broadcast=sent.append)),
        lambda _ctx: pytest.fail("no dispatch"), new_trace_id=lambda: "unused")

    adapters.goal_wake_retired(row)

    assert len(sent) == 1
    event = sent[0]
    assert (event["type"], event["session"], event["agent_id"], event["client_msg_id"],
            event["queue_depth"], event["queue_started"]) == (
                "queue-updated", "mike", agent_id, request, 0, False)
