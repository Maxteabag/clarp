"""A Stop holds the work queued before it until Resume, whatever path starts it.

Live, 2026-10-11 (eu-codex-profile-dc68): peer messages queued before a
``clarp-admin stop`` kept starting turns with no Resume. The runtime's Stop
put the work queued behind the stopped turn back in memory and
``finish_stop`` started its head; each later turn end (a finish, or a process
killed by hand) handed the slot to the next one. The pause was only read by
admission and durable recovery, never by the in-memory handover.
"""
from __future__ import annotations

import pytest

from lib import turn_queue
from lib import turn_dispatch as td

from test_drain_pause_parity import World


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    yield
    td.reset_for_tests()


def _running(world):
    return [c for c in world.provider.spawned
            if c["agent_id"] in world.provider.live]


def _end_running(world, *, killed=False):
    """The running turn ends: normally, or its process killed by hand."""
    call = _running(world)[-1]
    world.provider.live.discard(call["agent_id"])
    if killed:
        call["on_error"]("clarp exited rc=143")
    else:
        call["on_result"]({"result": "done"})


def _resume(world):
    """POST /turn-queue/resume: lift the pause, then recovery runs the queue."""
    assert turn_queue.set_paused(world.agent, False)
    world.service.recover_queued()
    world.settle()


def _waiting(world):
    return [row["client_msg_id"] for row in turn_queue.pending(world.agent)]


@pytest.mark.parametrize("queue", [False, True], ids=["peer", "peer-queued"])
def test_peer_messages_queued_before_a_stop_wait_for_resume(tmp_path, queue):
    world = World(tmp_path)
    world.send("t0")
    for cid in ("p1", "p2", "p3"):
        world.send(cid, origin="agent", queue=queue)
    world.stop()
    world._collect()
    assert world.started == ["t0"]
    assert _running(world) == []
    world.stop()                      # Stop again: still held
    world.settle(recover=True)
    assert world.started == ["t0"]
    assert _waiting(world) == ["p1", "p2", "p3"]
    assert turn_queue.is_paused(world.agent)
    _resume(world)
    assert world.started == ["t0", "p1", "p2", "p3"]  # once each, FIFO
    assert _waiting(world) == []


@pytest.mark.parametrize("killed", [False, True], ids=["finished", "killed"])
def test_a_turn_ending_after_stop_does_not_start_held_work(tmp_path, killed):
    """The user carries on past the pause; when that turn ends (or its process
    is killed by hand), the older held work still waits."""
    world = World(tmp_path)
    world.send("t0")
    for cid in ("p1", "p2"):
        world.send(cid, origin="agent", queue=True)
    world.stop()
    world.send("u1")                  # a plain user send runs past the pause
    world._collect()
    assert world.started == ["t0", "u1"]
    _end_running(world, killed=killed)
    world.settle()
    assert world.started == ["t0", "u1"]
    assert _waiting(world) == ["p1", "p2"]
    _resume(world)
    assert world.started == ["t0", "u1", "p1", "p2"]


def test_held_work_is_not_started_by_handoff_retries_or_ownerless_starts(tmp_path):
    world = World(tmp_path)
    world.send("t0")
    world.send("p1", origin="agent", queue=True)
    world.send("p2", origin="agent", queue=True)
    world.stop()
    world.service.start_held_work()
    world.service._retry_handoff(world.agent)
    td.drain_after_terminal(world.service.ctx, world.agent)
    world.settle()
    assert world.started == ["t0"]
    assert _waiting(world) == ["p1", "p2"]
    _resume(world)
    assert world.started == ["t0", "p1", "p2"]


def test_arrivals_during_the_stop_keep_their_documented_rights(tmp_path):
    """docs/protocol.md (send rules): a plain user send and a peer message run
    past the pause; an agent's explicit queue send waits for Resume."""
    world = World(tmp_path)
    world.send("t0")
    world.send("p_pre", origin="agent", queue=True)
    snapshot, _dropped, _was = td.begin_stop(world.agent)
    world.provider.interrupt("claude", world.agent)
    world.send("pq_mid", origin="agent", queue=True)
    world.send("u_mid")
    td.complete_stop(world.service.ctx, world.agent, snapshot, set(),
                     backend_registry=world.provider)
    world.settle()
    assert world.started == ["t0", "u_mid"]
    assert _waiting(world) == ["p_pre", "pq_mid"]
    _resume(world)
    assert world.started == ["t0", "u_mid", "p_pre", "pq_mid"]


def test_a_stop_takes_back_the_arrival_right_of_work_queued_before_it(tmp_path):
    """A peer message arriving after an earlier Stop could run past that pause;
    queued again behind a turn, the next Stop holds it like any other."""
    world = World(tmp_path)
    world.send("t0")
    world.stop()
    world.send("u0")                  # runs past the first pause
    world.send("p1", origin="agent")  # exempt on arrival, queued behind u0
    world.stop()
    world.settle()
    assert world.started == ["t0", "u0"]
    assert _waiting(world) == ["p1"]
    _resume(world)
    assert world.started == ["t0", "u0", "p1"]


def test_lifting_the_release_fence_after_a_stop_holds_pre_stop_work(tmp_path):
    world = World(tmp_path)
    world.send("t0")
    world.service.begin_admission_fence()
    world.send("p1", origin="agent", queue=True)
    world.send("h1", origin="automation")   # waits instead of preempting
    world.send("a1", origin="automation", queue=True)
    world.service.persist_held()              # h1 gets a drain-park row
    world.stop()
    world.service.lower_admission_fence()
    world.service.start_held_work()
    world.settle()
    assert world.started == ["t0"]
    # Held, never withdrawn: the parked send is visible queued work, in order.
    assert [_id.removeprefix("drain-park-t-") for _id in _waiting(world)] == [
        "p1", "h1", "a1"]
    _resume(world)
    assert world.started == ["t0", "p1", "h1", "a1"]


def _stop_with_arrivals(world, arrivals):
    """A Stop with sends admitted while its barrier is up."""
    snapshot, _dropped, _was = td.begin_stop(world.agent)
    world.provider.interrupt("claude", world.agent)
    for cid, origin, queue in arrivals:
        world.send(cid, origin=origin, queue=queue)
    td.complete_stop(world.service.ctx, world.agent, snapshot, set(),
                     backend_registry=world.provider)


def test_a_database_error_while_holding_loses_no_queued_work(tmp_path, monkeypatch):
    """"database is locked" mid-hold: every spec taken from memory goes back,
    in order; the retried handover then holds or starts each one."""
    import sqlite3
    world = World(tmp_path)
    world.send("t0")
    world.send("p_pre", origin="agent", queue=True)
    world.send("p_pre2", origin="agent", queue=True)
    real_get, failed = turn_queue.get, []

    def flaky_get(queue_id):
        if queue_id == "p_pre2" and not failed:
            failed.append(queue_id)
            raise sqlite3.OperationalError("database is locked")
        return real_get(queue_id)

    monkeypatch.setattr(turn_queue, "get", flaky_get)
    _stop_with_arrivals(world, [("u_mid", "user", False)])
    assert failed == ["p_pre2"]
    world.service._retry_handoff(world.agent)   # the scheduled retry
    world.settle()
    assert world.started == ["t0", "u_mid"]
    assert _waiting(world) == ["p_pre", "p_pre2"]
    _resume(world)
    assert world.started == ["t0", "u_mid", "p_pre", "p_pre2"]


def test_a_new_park_row_keeps_older_park_rows_behind_it(tmp_path):
    """A send with no row ahead of drain-parked ones (a failed handoff put it
    back at the head): Resume still runs them in memory order."""
    from dataclasses import replace
    world = World(tmp_path)
    world.send("t0")
    world.service.begin_admission_fence()
    world.send("h1", origin="automation")
    world.send("h2", origin="automation")
    world.service.persist_held()              # h1, h2 get drain-park rows
    world.stop()
    h1 = td._SLOTS.queued_specs()[world.agent][0]
    td._SLOTS.requeue_front(world.agent, replace(
        h1, text="x", trace_id="t-x", client_msg_id="x", park_id="",
        queue_id="", drain_persisted=False))
    world.service.lower_admission_fence()
    world.service.start_held_work()
    world.settle()
    assert world.started == ["t0"]
    assert _waiting(world) == ["x", "h1", "h2"]
    _resume(world)
    assert world.started == ["t0", "x", "h1", "h2"]
