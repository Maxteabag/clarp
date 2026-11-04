"""AUT-05: background ticks queue behind a busy agent. A queued tick that a
restart left in the durable queue must not stay there: the heartbeat
adapters and the janitor skip an agent with queued work, so a row nobody
admits would end that agent's autonomy for good."""
import itertools
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import db, turn_queue
from lib.dispatch_adapters import DispatchAdapters
from lib.policies.admission import JANITOR_DEMAND_PREFIX
from lib.turn_dispatch import TurnDispatchService
import lib.turn_dispatch as _td


@pytest.fixture(autouse=True)
def _local_dispatch_runtime(monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    _td.reset_for_tests()
    yield
    _td.reset_for_tests()


class _Backends:
    CLAUDE = "claude"

    def __init__(self):
        self.spawned = []

    def normalize(self, backend):
        return backend or self.CLAUDE

    def interrupt(self, backend, agent_id):
        return 0

    def active_handles(self, backend, agent_id):
        return []

    def spawn_turn(self, backend, **kwargs):
        self.spawned.append(kwargs)


def _service(tmp_path):
    backends = _Backends()
    ids = itertools.count(1)
    ctx = SimpleNamespace(default_session="mike",
                          agents_path=tmp_path / "unused.json",
                          stream=SimpleNamespace(broadcast=lambda _e: None))
    service = TurnDispatchService(
        ctx, backend_registry=backends, home=tmp_path,
        uuid_factory=lambda: f"backend-session-{next(ids)}", now=lambda: 1000.0,
        retry_scheduler=lambda _delay, _fn: None)
    return service, backends


class _Ticks:
    def __init__(self):
        self.sent = []

    def submit(self, command):
        self.sent.append(command)
        return SimpleNamespace(queued=False)


@pytest.mark.parametrize("origin,client_msg_id", [
    ("heartbeat", JANITOR_DEMAND_PREFIX + "run-7"),   # a decided janitor wake
    ("heartbeat", ""),
    ("leader_tick", ""),
])
def test_a_restart_drops_a_queued_tick_and_the_agent_gets_its_next_one(
        tmp_path, origin, client_msg_id):
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike",
        backend="claude")
    agents_db.start_runtime(agent_id, "mike")
    turn_queue.enqueue(
        queue_id="q-tick", agent_id=agent_id, session="mike", text="tick",
        trace_id="t-tick", client_msg_id=client_msg_id or "t-tick",
        synthesize_audio=False, origin=origin, sender_agent_id="")
    service, backends = _service(tmp_path)   # a new process: no memory of the queue

    service.recover_queued()

    assert backends.spawned == []   # a decision about an older state never runs
    assert db.conn().execute(
        "SELECT COUNT(*) FROM queued_turns WHERE agent_id=?"
        " AND status IN ('queued','claimed')", (agent_id,)).fetchone()[0] == 0
    ticks = _Ticks()
    DispatchAdapters(SimpleNamespace(), lambda _ctx: ticks).heartbeat("mike", "next")
    assert [c.origin for c in ticks.sent] == ["heartbeat"]
