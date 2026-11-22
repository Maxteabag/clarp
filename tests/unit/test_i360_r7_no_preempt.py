"""Issue 360, R7: a plain send from something that is not the user (a schedule, an
automation, a client's automatic origin, the Oracle) never ends a busy agent's
running turn: it waits for it, as the user's own plain send does."""
from __future__ import annotations

import pytest

from lib import turn_dispatch as td
from lib import turn_queue

from test_drain_pause_parity import World


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    yield
    td.reset_for_tests()


@pytest.mark.parametrize("origin", ["automation", "schedule", "watcher", "heartbeat", "oracle"])
def test_a_plain_non_user_send_waits_for_the_running_turn(tmp_path, monkeypatch, origin):
    world = World(tmp_path)
    interrupted = []
    real = world.provider.interrupt
    monkeypatch.setattr(world.provider, "interrupt",
                        lambda backend, agent_id: interrupted.append(agent_id) or real(backend, agent_id))
    world.send("t0")
    world.send("x", origin=origin)                    # plain: no queue_if_busy
    world._collect()
    assert interrupted == []                          # the running turn goes on
    assert world.started == ["t0"]
    assert world.agent in world.provider.live
    assert "x" in [row["client_msg_id"] for row in turn_queue.pending(world.agent)]
    world.settle()
    assert world.started == ["t0", "x"]               # it ran next, once


def test_a_scheduled_job_waits_for_the_running_turn(tmp_path, monkeypatch):
    from lib.dispatch_adapters import DispatchAdapters
    world = World(tmp_path)
    interrupted = []
    monkeypatch.setattr(world.provider, "interrupt",
                        lambda backend, agent_id: interrupted.append(agent_id) or 1)
    world.send("t0")
    DispatchAdapters(world.service.ctx, lambda _ctx: world.service).scheduled_job("mike", "the schedule")
    world._collect()
    assert interrupted == [] and world.started == ["t0"]
    world.settle()
    assert world.started == ["t0", "the schedule"]
