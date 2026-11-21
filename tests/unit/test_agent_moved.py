"""An agent that moved to another Host takes no turn here (lib.agent_moves).

Through the real dispatcher (test_drain_pause_parity.World): every door that
starts a turn meets admission's refusal; queued items are held, not dispatched,
and released when the move is taken back; a running turn is not cut.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agent_moves, turn_queue
from lib import turn_dispatch as td
from lib.turn_command import DispatchError

from test_drain_pause_parity import World


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    yield
    td.reset_for_tests()


def _waiting(world):
    return [row["client_msg_id"] for row in turn_queue.pending(world.agent)]


def _move(world):
    mark, changed = agent_moves.set_moved(world.agent, host="rust-host", remote_id="r-1")
    assert changed
    return mark


def _refused(call):
    with pytest.raises(DispatchError) as caught:
        call()
    assert caught.value.status == 409 and str(caught.value) == agent_moves.AGENT_MOVED


# --- the mark --------------------------------------------------------------------

def test_the_mark_is_idempotent_refuses_another_place_and_is_taken_back(tmp_path):
    world = World(tmp_path)
    mark = _move(world)
    assert mark["to"] == {"host": "rust-host", "id": "r-1"} and mark["at"] > 0
    again, changed = agent_moves.set_moved(world.agent, host="rust-host", remote_id="r-1")
    assert not changed and again == mark
    with pytest.raises(agent_moves.MoveConflict):
        agent_moves.set_moved(world.agent, host="elsewhere", remote_id="r-2")
    assert agent_moves.moved(world.agent) == mark
    assert agent_moves.all_moved() == {world.agent: mark}
    assert agent_moves.clear(world.agent)
    assert not agent_moves.clear(world.agent)
    assert agent_moves.moved(world.agent) is None
    with pytest.raises(ValueError):
        agent_moves.set_moved(world.agent, host="", remote_id="r-1")


# --- every door ------------------------------------------------------------------

@pytest.mark.parametrize("origin,queue", [
    ("user", False), ("user", True), ("oracle", False),       # the apps' send and the Oracle
    ("agent", False), ("agent", True),                         # clarp-admin prompt, a parent, a peer
    ("automation", True), ("schedule", True),                  # self-prompts, goal wakes, schedules
    ("heartbeat", True), ("leader_tick", True),
])
def test_every_send_is_refused_and_nothing_starts(tmp_path, origin, queue):
    world = World(tmp_path)
    _move(world)
    _refused(lambda: world.service.dispatch(
        text="hi", requested_session="mike", trace_id="t-x", client_msg_id="x",
        synthesize_audio=False, queue_if_busy=queue, origin=origin,
        sender_agent_id="other-agent" if origin == "agent" else ""))
    world._collect()
    assert world.started == []
    assert _waiting(world) == []


def test_the_hosts_own_wakes_are_refused_at_the_same_admission(tmp_path):
    from lib.dispatch_adapters import DispatchAdapters
    world = World(tmp_path)
    _move(world)
    adapters = DispatchAdapters(world.service.ctx, lambda _ctx: world.service)
    _refused(lambda: adapters.goal_continuation("mike", "continue", "goal-wake-1"))
    _refused(lambda: adapters.scheduled_job("mike", "scheduled"))
    _refused(lambda: adapters.decided_heartbeat("mike", "beat", "janitor-demand-1"))
    world._collect()
    assert world.started == []


def test_a_steer_into_a_running_turn_is_refused_too(tmp_path):
    """A turn already running is not cut; a send that would steer into it is
    refused like any other (it would write the session the other Host writes)."""
    world = World(tmp_path)
    world.send("t0")
    _move(world)
    assert world.agent in world.provider.live           # the running turn goes on
    _refused(lambda: world.send("u1"))
    world._collect()
    assert world.started == ["t0"]


def test_an_explicit_send_of_a_queued_item_is_refused_and_it_stays(tmp_path):
    world = World(tmp_path)
    world.send("t0")
    world.send("p1", origin="agent", queue=True)
    _move(world)
    _refused(lambda: world.service.dispatch_queued("p1"))
    assert turn_queue.status("p1") == "queued"


# --- queued items are held, and released on the way back -------------------------

def test_queued_items_are_held_and_released_when_the_move_is_taken_back(tmp_path):
    world = World(tmp_path)
    world.send("t0")
    world.send("p1", origin="agent", queue=True)
    world.send("u2", queue=True)
    _move(world)
    world.settle()                                       # t0 ends; nothing else starts
    assert world.started == ["t0"]
    assert sorted(_waiting(world)) == ["p1", "u2"]
    world.service.recover_queued()
    world._collect()
    assert world.started == ["t0"]
    assert agent_moves.clear(world.agent)
    world.service.recover_queued()
    world.settle()
    assert world.started == ["t0", "u2", "p1"]           # the user's word first (#481)
    assert _waiting(world) == []


def test_held_items_survive_a_restart_while_moved(tmp_path):
    world = World(tmp_path)
    world.send("t0")
    world.send("p1", origin="agent", queue=True)
    _move(world)
    world.restart()
    assert "p1" not in world.started and _waiting(world) == ["p1"]
    agent_moves.clear(world.agent)
    world.service.recover_queued()
    world.settle()
    assert world.started[-1] == "p1"


# --- the paths that start a process without a send ------------------------------

def test_compaction_and_a_dream_are_refused(tmp_path):
    from lib import compaction, dreaming
    world = World(tmp_path)
    _move(world)
    answer = compaction.compact_session("mike")
    assert not answer.get("ok", False) and "moved" in str(answer).lower()
    agent = {"agent_id": world.agent, "session": "mike", "backend": "claude"}
    assert dreaming.dispatch_isolated_dream(agent, "dream") is False
    world._collect()
    assert world.started == []


def test_the_native_session_is_not_touched(tmp_path):
    from lib import agents as agents_db
    world = World(tmp_path)
    world.send("t0")
    before = agents_db.live_backend_session(world.agent)
    _move(world)
    agent_moves.clear(world.agent)
    assert agents_db.live_backend_session(world.agent) == before
