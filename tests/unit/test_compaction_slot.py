"""The user's compaction holds the agent's turn slot (R148).

Sends that arrive while it runs wait behind it: none is lost, none starts on
top of it, and they run after it in the order they came, also across a
runtime restart. A Stop takes the slot back and leaves the agent usable.
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


def _waiting(world):
    return [row["client_msg_id"] for row in turn_queue.pending(world.agent)]


def test_sends_during_a_compaction_wait_and_run_after_it_in_order(tmp_path):
    world = World(tmp_path)
    assert td.claim_for_compaction(world.agent) == ""
    world.send("u1")
    world.send("p1", origin="agent")
    world.send("q1", origin="agent", queue=True)
    world.send("u2")
    world.service.recover_queued()       # recovery does not start them either
    world._collect()
    assert world.started == []
    assert world.provider.spawned == []
    assert _waiting(world) == ["u1", "p1", "q1", "u2"]   # durable, in order
    td.finish_compaction(world.service, world.agent)
    world.settle()
    assert world.started == ["u1", "p1", "q1", "u2"]
    assert _waiting(world) == []


def test_a_user_send_during_a_compaction_survives_a_runtime_restart(tmp_path):
    world = World(tmp_path)
    assert td.claim_for_compaction(world.agent) == ""
    world.send("u1")
    world.send("u2")
    world.restart()                      # memory gone, the compaction with it
    assert world.started == ["u1", "u2"]
    assert _waiting(world) == []


def test_a_compaction_is_refused_while_the_agent_is_busy_or_has_work_waiting(tmp_path):
    world = World(tmp_path)
    world.send("t0")
    assert td.claim_for_compaction(world.agent) == "agent is busy — try again when idle"
    world.settle()
    assert td.claim_for_compaction(world.agent) == ""
    assert td.claim_for_compaction(world.agent) == "already compacting"
    td.finish_compaction(world.service, world.agent)
    assert td.compaction_holds_slot(world.agent) is False


def test_nothing_preempts_a_compaction(tmp_path):
    """No origin interrupts it: the provider is never asked to stop."""
    world = World(tmp_path)
    assert td.claim_for_compaction(world.agent) == ""
    for origin in ("user", "agent", "system"):
        world.send(f"x-{origin}", origin=origin)
    assert world.provider.spawned == []
    assert td.compaction_holds_slot(world.agent) is True


def test_stop_during_a_compaction_leaves_the_agent_usable(tmp_path):
    world = World(tmp_path)
    assert td.claim_for_compaction(world.agent) == ""
    world.send("u1")
    world.stop()
    # The compaction sees its slot is gone and ends; its finish is a no-op.
    assert td.compaction_holds_slot(world.agent) is False
    td.finish_compaction(world.service, world.agent)
    world._collect()
    started_or_waiting = set(world.started) | set(_waiting(world))
    assert "u1" in started_or_waiting    # not lost
    world.settle()
    world.send("u2")                     # the agent takes new work
    world._collect()
    assert "u2" in world.started
    world.settle()
    assert td.claim_for_compaction(world.agent) == ""   # and can be compacted again
    td.finish_compaction(world.service, world.agent)


def test_the_compaction_slot_is_not_freed_as_stale(tmp_path):
    world = World(tmp_path)
    assert td.claim_for_compaction(world.agent) == ""
    assert td.free_stale_slot(world.agent) is None
    assert td.compaction_holds_slot(world.agent) is True
    snapshot = td.snapshot_stop_state(world.agent)
    assert snapshot["trace_id"] == ""    # nothing for a strict Stop to confirm
    td.finish_compaction(world.service, world.agent)
