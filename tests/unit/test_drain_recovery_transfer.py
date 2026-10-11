"""The release drain hands over account-recovery parks (Solu re7d614160304,
item 1): only work no process ever ran for, with its exact prompt, binding,
order and pause state durable, moves into a recovery-park row at the head of
its agent's queue in one transaction; anything uncertain keeps blocking. The
park's start carries over so the next runtime keeps the bound. Credentials
and account choice never move.

Real dispatcher, queue, runtime RPC server and coordinator; fake provider,
clock and selector."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from lib import account_failover, message_store, turn_lifecycle, turn_queue
from lib import agents as agents_db
from lib import turn_dispatch as td
from lib.account_failover import AccountFailover
from lib.protocol import AgentState
from lib.runtime_bridge import RuntimeRPCServer
from lib.runtime_release import (consume_account_recovery_carry,
                                 write_account_recovery_carry)

from test_graceful_release_drain import (  # noqa: F401 - autouse fixture
    _agent, _fresh_runtime_memory, _service)

SESSION_ID = "11111111-2222-3333-4444-555555555555"


class Selector:
    """The account selector's verdicts, in order; records each ask."""

    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)
        self.asked = 0

    def __call__(self, _command, _models):
        self.asked += 1
        return self.verdicts.pop(0) if self.verdicts else True


class Pool:
    """A fresh claude coordinator (one per runtime process) whose timers we
    run by hand."""

    def __init__(self, monkeypatch, selector=None, now=100.0):
        self.scheduled = []
        self.clock = [now]
        self.selector = selector or Selector()
        self.coordinator = AccountFailover(
            td.OwnershipLock(), switch=self.selector,
            schedule=lambda delay, callback: self.scheduled.append(callback),
            now=lambda: self.clock[0])
        monkeypatch.setitem(td._FAILOVERS, "claude", self.coordinator)

    def run_timers(self, rounds=1):
        """Fire what is due now; a pass may schedule the next (one round)."""
        for _ in range(rounds):
            due, self.scheduled[:] = list(self.scheduled), []
            for callback in due:
                callback()


@pytest.fixture
def selector_configured(monkeypatch, tmp_path):
    cfg = replace(td.config.load(), claude_account_switch_command=("selector",))
    monkeypatch.setattr(td.config, "load", lambda *args, **kwargs: cfg)
    # The bound conversation exists, so turns resume it (not new sessions).
    monkeypatch.setattr(td.backends.by_id("claude"), "resume_target",
                        lambda *args, **kwargs: tmp_path / "native.jsonl")


def _bound_agent(tmp_path, session="mike"):
    agent = _agent(tmp_path, session)
    agents_db.bind_backend_session(agent, SESSION_ID)
    return agent


def _runtime(tmp_path, service, carry=True):
    runtime = RuntimeRPCServer(
        tmp_path / "runtime.sock", dispatch_service=service, release_id="old",
        account_recovery_carry=(tmp_path / "carry.json") if carry else None)
    runtime.shutdown = lambda: None
    return runtime


def _send(service, text, *, queue=False, origin="user"):
    return service.dispatch(
        text=text, requested_session="mike", trace_id=f"t-{text}",
        client_msg_id=text, synthesize_audio=False, queue_if_busy=queue,
        origin=origin)


def _parked_scene(tmp_path, monkeypatch, *, selector=None):
    """mike's turn "work" parked for an account before any process ran
    (another agent's usage limit started the recovery), then the fence, then
    a held arrival "next"."""
    pool = Pool(monkeypatch, selector)
    agent = _bound_agent(tmp_path)
    service, provider = _service(tmp_path)
    pool.coordinator.recovering = True
    pool.coordinator.parked_since = 40.0          # parked 60 s ago
    pool.coordinator.command = ("selector",)
    _send(service, "work")
    assert provider.spawned == []                  # parked, no process
    assert pool.coordinator.status()["waiting"] == [agent]
    service.begin_admission_fence()
    _send(service, "next")
    runtime = _runtime(tmp_path, service)
    return agent, service, provider, pool, runtime


def _restart(tmp_path, monkeypatch, *, clean=True, consume_carry=True,
             selector=None, now=100.0):
    """A new runtime process: fresh memory and coordinator, same database.
    ``consume_carry=False`` is an older runtime that does not know the carry."""
    td.reset_for_tests()
    pool = Pool(monkeypatch, selector, now=now)
    if consume_carry:
        carry = consume_account_recovery_carry(tmp_path / "carry.json")
        td.resume_account_recovery(carry)
    service, provider = _service(tmp_path)
    if not clean:
        from lib.interrupted_turns import recover_after_restart
        recover_after_restart()
    service.recover_queued()
    return service, provider, pool


def _user_rows(client_msg_id):
    from lib.db import conn
    # The tests use the text as the client id; count every user row of it.
    return conn().execute(
        "SELECT COUNT(*) AS n FROM messages WHERE text = ? AND role = 'user'",
        (client_msg_id,)).fetchone()["n"]


# --- the hand-over ---------------------------------------------------------------

def test_a_park_no_process_ran_for_moves_to_the_head_and_the_runtime_seals(
        tmp_path, monkeypatch, selector_configured):
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)
    try:
        assert runtime.seal_if_drained() == {}
        assert runtime._draining is True
    finally:
        runtime.server_close()
    # One durable row per message, the parked turn first.
    rows = turn_queue.parked(agent)
    assert [row["queue_id"] for row in rows] == [
        "recovery-park-t-work", "drain-park-t-next"]
    assert [row["text"] for row in rows] == ["work", "next"]
    assert rows[0]["client_msg_id"] == "work"
    assert rows[0]["allow_paused"] == 0
    # The coordinator no longer owns it; the slot is free; nothing ran.
    assert pool.coordinator.attempts == {}
    assert agent not in td._INFLIGHT
    assert provider.spawned == []
    # Its turn ended without an outcome, and the agent is not busy.
    assert not [t for t in turn_lifecycle.open_turns() if t["agent_id"] == agent]
    latest = agents_db.latest_state(agent)
    assert latest["kind"] == AgentState.IDLE
    assert latest["detail"]["reason"] == "update_handoff"
    # The park's start carries over (only timing, no credentials).
    carry = json.loads((tmp_path / "carry.json").read_text())
    assert set(carry["pools"]) == {"claude"}
    assert set(carry["pools"]["claude"]) == {"parked_since_ms"}


def test_after_the_handoff_the_work_runs_once_in_order_and_the_bound_carries_on(
        tmp_path, monkeypatch, selector_configured):
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)
    try:
        assert runtime.seal_if_drained() == {}
    finally:
        runtime.server_close()
    new_service, new_provider, new_pool = _restart(tmp_path, monkeypatch)
    # The new runtime continues the recovery: the park did not restart.
    assert new_pool.coordinator.recovering is True
    assert 55 <= new_pool.clock[0] - new_pool.coordinator.parked_since <= 75
    assert new_provider.spawned == []          # parked again, nothing ran
    new_pool.run_timers()                      # the selector verifies an account
    assert new_pool.selector.asked == 1
    new_provider.finish("work")
    new_service.recover_queued()
    assert new_provider.texts() == ["work", "next"]
    assert [c["trace_id"] for c in new_provider.spawned] == ["t-work", "t-next"]
    assert _user_rows("work") == 1 and _user_rows("next") == 1
    assert turn_queue.parked(agent) == [] and turn_queue.pending(agent) == []


def test_a_runtime_that_ignores_the_carry_still_runs_the_work_once_in_order(
        tmp_path, monkeypatch, selector_configured):
    """Old/new compatibility: the rows are ordinary parked queue rows, so an
    older runtime (or a rollback) recovers them as at every boot."""
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)
    try:
        assert runtime.seal_if_drained() == {}
    finally:
        runtime.server_close()
    new_service, new_provider, _pool = _restart(
        tmp_path, monkeypatch, consume_carry=False)
    new_provider.finish("work")
    new_service.recover_queued()
    assert new_provider.texts() == ["work", "next"]
    assert _user_rows("work") == 1


# --- crashes -------------------------------------------------------------------

def test_a_crash_before_the_transfer_persists_changes_nothing(
        tmp_path, monkeypatch, selector_configured):
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)

    def fail(**_kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(turn_queue, "park_at_head", fail)
    try:
        blockers = runtime.seal_if_drained()
    finally:
        runtime.server_close()
    assert blockers["claude_account_recovery"] == [agent]
    assert blockers["account_recovery_refused"] == [f"{agent}:persist_failed"]
    assert runtime._draining is False
    assert [r["queue_id"] for r in turn_queue.parked(agent)] == ["drain-park-t-next"]
    assert pool.coordinator.status()["waiting"] == [agent]   # still parked
    assert td._INFLIGHT[agent] == "t-work"
    assert [t["trace_id"] for t in turn_lifecycle.open_turns()
            if t["agent_id"] == agent] == ["t-work"]
    assert agents_db.latest_state(agent)["kind"] == AgentState.THINKING


def test_a_failed_transfer_after_the_recovery_ended_resumes_the_work(
        tmp_path, monkeypatch, selector_configured):
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)

    def end_recovery_then_fail(**_kwargs):
        pool.coordinator.recovering = False      # an account was verified
        raise OSError("disk full")
    monkeypatch.setattr(turn_queue, "park_at_head", end_recovery_then_fail)
    try:
        runtime.seal_if_drained()
    finally:
        runtime.server_close()
    assert provider.texts() == ["work"]          # resumed, not stranded


@pytest.mark.parametrize("sealed", [False, True])
def test_a_crash_after_the_transfer_runs_the_work_once_without_an_interruption(
        tmp_path, monkeypatch, selector_configured, sealed):
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)
    try:
        if sealed:
            assert runtime.seal_if_drained() == {}
        else:
            write_account_recovery_carry(tmp_path / "carry.json",
                                         td.account_recovery_carry())
            assert service.transfer_recovery_parks()["transferred"] == [agent]
    finally:
        runtime.server_close()
    new_service, new_provider, new_pool = _restart(
        tmp_path, monkeypatch, clean=False)
    # No "interrupted by restart" marker: the turn was not running.
    assert agents_db.latest_state(agent)["kind"] != AgentState.INTERRUPTED
    new_pool.run_timers()
    new_provider.finish("work")
    new_service.recover_queued()
    assert [c["trace_id"] for c in new_provider.spawned] == ["t-work", "t-next"]


# --- what keeps the blocker ------------------------------------------------------

def _mutate_parked_spec(pool, agent, **fields):
    attempt = pool.coordinator.attempts[agent]
    spec, number = attempt.work
    attempt.work = (replace(spec, **fields), number)


@pytest.mark.parametrize("change,reason", [
    (lambda pool, agent: pool.coordinator.attempts[agent].state.__setitem__(
        "spawn_started", True), None),                      # a process ran
    (lambda pool, agent: setattr(pool.coordinator.attempts[agent], "work",
        (pool.coordinator.attempts[agent].work[0], 2)), "retried"),
    (lambda pool, agent: _mutate_parked_spec(pool, agent, recovery_text="go on"),
     "continuation"),
    (lambda pool, agent: _mutate_parked_spec(pool, agent, janitor_run_id="j1"),
     "maintenance"),
    (lambda pool, agent: _mutate_parked_spec(pool, agent, unheard_audio=True),
     "unheard_audio"),
    (lambda pool, agent: _mutate_parked_spec(pool, agent, is_new_session=True),
     "new_session"),
    (lambda pool, agent: agents_db.bind_backend_session(
        agent, "99999999-2222-3333-4444-555555555555"), "binding_changed"),
    (lambda pool, agent: turn_queue.set_paused(agent, True), "queue_paused"),
    (lambda pool, agent: setattr(pool.coordinator, "checking", True),
     "selector_running"),
    (lambda pool, agent: setattr(pool.coordinator.attempts[agent], "work", None),
     "unknown_work"),
])
def test_uncertain_parked_work_keeps_its_blocker(
        tmp_path, monkeypatch, selector_configured, change, reason):
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)
    change(pool, agent)
    try:
        blockers = runtime.seal_if_drained()
    finally:
        runtime.server_close()
    assert runtime._draining is False
    assert agent in (blockers.get("claude_account_recovery") or [])
    if reason:
        assert f"{agent}:{reason}" in blockers["account_recovery_refused"]
    assert [r["queue_id"] for r in turn_queue.parked(agent)] == ["drain-park-t-next"]
    assert td._INFLIGHT[agent] == "t-work"
    assert pool.coordinator.attempts[agent].trace_id == "t-work"


def test_a_missing_process_alone_is_not_a_park(tmp_path, monkeypatch, selector_configured):
    """A spawning slot with no process and no coordinator record is not
    account-recovery work: it keeps blocking as active/spawning."""
    pool = Pool(monkeypatch)
    agent = _bound_agent(tmp_path)
    service, provider = _service(tmp_path)
    td._INFLIGHT[agent] = "t-ghost"
    td._CLAIMED_AT[agent] = 1.0
    service.begin_admission_fence()
    runtime = _runtime(tmp_path, service)
    monkeypatch.setattr(service, "release_leaked_slots", lambda **_k: {})
    try:
        blockers = runtime.seal_if_drained()
    finally:
        runtime.server_close()
    assert blockers["active"] == [agent] and blockers["spawning"] == [agent]
    assert runtime._draining is False


def test_other_owners_keep_the_seal_and_nothing_is_transferred(
        tmp_path, monkeypatch, selector_configured):
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)
    # A shared-provider turn (a Codex goal continuation) is still running.
    monkeypatch.setattr(td, "provider_turns", lambda: ["codex-agent"])
    try:
        blockers = runtime.seal_if_drained()
    finally:
        runtime.server_close()
    assert blockers["provider_turns"] == ["codex-agent"]
    assert runtime._draining is False
    assert pool.coordinator.attempts[agent].trace_id == "t-work"
    assert [r["queue_id"] for r in turn_queue.parked(agent)] == ["drain-park-t-next"]


def test_without_a_carry_path_parks_block_as_before(tmp_path, monkeypatch, selector_configured):
    agent, service, provider, pool, _runtime_with_carry = _parked_scene(tmp_path, monkeypatch)
    _runtime_with_carry.server_close()
    runtime = _runtime(tmp_path, service, carry=False)
    try:
        blockers = runtime.seal_if_drained()
    finally:
        runtime.server_close()
    assert blockers["claude_account_recovery"] == [agent]
    assert "account_recovery_refused" not in blockers


def test_a_recovery_pass_in_flight_with_nothing_parked_blocks(tmp_path, monkeypatch,
                                                              selector_configured):
    pool = Pool(monkeypatch)
    _bound_agent(tmp_path)
    service, _provider = _service(tmp_path)
    pool.coordinator.recovering = True
    pool.coordinator.checking = True
    service.begin_admission_fence()
    runtime = _runtime(tmp_path, service)
    try:
        assert runtime.seal_if_drained() == {"claude_account_recovery": ["<checking>"]}
        pool.coordinator.checking = False
        assert runtime.seal_if_drained() == {}   # carries over instead
    finally:
        runtime.server_close()


# --- concurrency -----------------------------------------------------------------

def test_a_verified_account_during_the_transfer_never_runs_the_work_twice(
        tmp_path, monkeypatch, selector_configured):
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)
    original = turn_queue.park_at_head

    def verify_meanwhile(**kwargs):
        pool.coordinator.recover()              # the selector says yes now
        return original(**kwargs)
    monkeypatch.setattr(turn_queue, "park_at_head", verify_meanwhile)
    try:
        assert runtime.seal_if_drained() == {}
    finally:
        runtime.server_close()
    assert provider.spawned == []               # not resumed here...
    new_service, new_provider, new_pool = _restart(tmp_path, monkeypatch)
    new_pool.run_timers()
    new_provider.finish("work")
    new_service.recover_queued()
    assert [c["trace_id"] for c in new_provider.spawned] == ["t-work", "t-next"]


def test_a_resume_that_already_happened_leaves_nothing_to_transfer(
        tmp_path, monkeypatch, selector_configured):
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)
    pool.coordinator.recover()                  # verified before the seal
    try:
        blockers = runtime.seal_if_drained()
    finally:
        runtime.server_close()
    assert provider.texts() == ["work"]         # it runs here, once
    assert agent in blockers["active"]
    assert [r["queue_id"] for r in turn_queue.parked(agent)] == ["drain-park-t-next"]


# --- Stop, revert, rollback ------------------------------------------------------

def test_a_stop_after_the_handoff_keeps_the_work_held_until_resume(
        tmp_path, monkeypatch, selector_configured):
    """Peter's decision (Keep stopped work held): work from before the latest
    Stop waits for Resume after a handoff."""
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)
    try:
        assert runtime.seal_if_drained() == {}
        result = runtime.dispatch_request("begin_stop", {
            "agent_id": agent, "backend": "claude", "strict": False, "hold": False})
        assert result["ok"]
    finally:
        runtime.server_close()
    new_service, new_provider, new_pool = _restart(tmp_path, monkeypatch)
    new_pool.run_timers()
    assert new_provider.spawned == []
    turn_queue.set_paused(agent, False)          # Resume
    new_service.recover_queued()
    new_pool.run_timers()
    assert new_provider.texts() == ["work"]


def test_a_revert_after_a_transfer_starts_the_work_first(
        tmp_path, monkeypatch, selector_configured):
    """A transfer that did not end in a seal (a new owner appeared) leaves
    durable held work; lifting the fence starts it ahead of later arrivals."""
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)
    try:
        assert service.transfer_recovery_parks()["transferred"] == [agent]
        runtime.release_admission_fence()
    finally:
        runtime.server_close()
    # It parks again here (still recovering); the pending pass verifies.
    assert provider.spawned == []
    pool.coordinator.recover()
    provider.finish("work")
    assert provider.texts() == ["work", "next"]
    assert _user_rows("work") == 1
    assert turn_queue.parked(agent) == []


def test_a_rollback_between_seal_and_shutdown_runs_the_work_here(
        tmp_path, monkeypatch, selector_configured):
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)
    try:
        assert runtime.seal_if_drained() == {}
        runtime.cancel_handover()
        assert not (tmp_path / "carry.json").exists()
        runtime.release_admission_fence()
    finally:
        runtime.server_close()
    # Still recovering here: it parks again (no process), then resumes.
    assert provider.spawned == []
    pool.coordinator.recover()
    provider.finish("work")
    assert provider.texts() == ["work", "next"]


def test_an_account_selection_failure_after_the_handoff_keeps_it_parked_within_the_bound(
        tmp_path, monkeypatch, selector_configured):
    agent, service, provider, pool, runtime = _parked_scene(tmp_path, monkeypatch)
    try:
        assert runtime.seal_if_drained() == {}
    finally:
        runtime.server_close()
    new_service, new_provider, new_pool = _restart(
        tmp_path, monkeypatch, selector=Selector(None, False))
    new_pool.run_timers()                        # the selector: no verdict
    new_pool.clock[0] += account_failover.RECHECK_SECONDS + 1
    new_pool.run_timers()                        # the selector: no account
    assert new_pool.selector.asked == 2
    assert new_provider.spawned == []
    assert new_pool.coordinator.status()["waiting"] == [agent]
    # The bound counts from the original park (60 s before the handoff),
    # not from the handoff.
    new_pool.clock[0] = 100.0 + account_failover.MAX_PARK_SECONDS - 60 + 1
    new_pool.coordinator.recover()
    new_pool.run_timers()
    assert new_provider.texts() == ["work"]     # released to run and fail visibly


def test_a_stale_or_unreadable_carry_is_ignored(tmp_path):
    path = tmp_path / "carry.json"
    write_account_recovery_carry(path, {"claude": {"parked_since_ms": 1}})
    assert consume_account_recovery_carry(path, wall=10**10) == {}
    assert not path.exists()
    path.write_text("{not json")
    assert consume_account_recovery_carry(path) == {}
    write_account_recovery_carry(path, {})
    assert not path.exists()
