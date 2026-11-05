"""TP-N1 review (provider condition): a scheduled retry runs outside the
runtime's admission mutex, so a Stop can take its slot after the retry's
ownership check. The retry must then not hand a new turn to the provider; and
a Stop that lands during the handoff itself must not leave the turn the
provider just started running unseen."""
from __future__ import annotations

from lib import team_store, turn_dispatch as td

from test_drain_pause_parity import Provider, World

CONNECTION_DROP = "API Error: The socket connection was closed unexpectedly."


class Handle:
    def __init__(self):
        self.terminated = 0

    def terminate(self):
        self.terminated += 1


class HandleProvider(Provider):
    def __init__(self):
        super().__init__()
        self.handles: list[Handle] = []
        self.during_spawn = None

    def spawn_turn(self, backend, **kwargs):
        super().spawn_turn(backend, **kwargs)
        if self.during_spawn is not None:
            hook, self.during_spawn = self.during_spawn, None
            hook()
        self.handles.append(Handle())
        return self.handles[-1]


def _world_with_a_scheduled_retry(tmp_path):
    td.reset_for_tests()
    world = World(tmp_path)
    world.provider = HandleProvider()
    world.service.backends = world.provider
    retries = []
    world.service.retry_scheduler = lambda _delay, fn: retries.append(fn)
    world.send("first")
    assert len(world.provider.spawned) == 1
    world.provider.live.discard(world.agent)
    world.provider.spawned[0]["on_error"](CONNECTION_DROP)
    assert len(retries) == 1, "a connection drop schedules one retry"
    return world, retries[0]


def test_a_stop_during_retry_preparation_launches_nothing(tmp_path, monkeypatch):
    world, retry = _world_with_a_scheduled_retry(tmp_path)
    digest = team_store.pending_digest

    def stop_then_digest(agent_id):
        monkeypatch.setattr(team_store, "pending_digest", digest)
        world.stop()
        return digest(agent_id)

    monkeypatch.setattr(team_store, "pending_digest", stop_then_digest)
    retry()

    assert len(world.provider.spawned) == 1, "the retry was handed to the provider after Stop"
    td.reset_for_tests()


def test_a_stop_during_the_retry_handoff_interrupts_the_started_turn(tmp_path):
    world, retry = _world_with_a_scheduled_retry(tmp_path)
    # The provider has the turn but Stop ran before its handle existed, so
    # Stop's own interrupt found nothing to end.
    world.provider.during_spawn = world.stop
    retry()

    assert len(world.provider.spawned) == 2
    assert world.provider.handles[-1].terminated == 1
    td.reset_for_tests()
