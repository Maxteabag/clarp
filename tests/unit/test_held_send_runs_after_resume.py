"""A plain send to a busy agent, held by the user's Stop, runs after the resume (R120).

Maximus's corpus case (delivery-of-a-held-send; synthetic), rebuilt on the dispatcher harness:
  1. send 'papa' (plain): it starts; 2. the agent is busy;
  3. send 'quebec' (plain, no queue_if_busy) while busy;
  4. Stop; 5. the Host comes to rest with the queue held;
  6. quebec's delivery reads handed_over (the corpus's step 6);
  7. resume; 8. quebec must start.
The same steps with queue_if_busy on the second send (the corpus's stop-with-queue) start it.

The provider refuses a mid-turn steer, as the real registry does for Claude (backends.steer_turn:
the base backend raises Unsupported and steer_turn answers False), so a plain send meets the
steer path first, as on a Host."""
from __future__ import annotations

import pytest

from lib import db, launch_receipts, message_writes, turn_queue
from lib import turn_dispatch as td

from test_drain_pause_parity import Provider, World


class _ClaudeLikeProvider(Provider):
    """The registry as a Host has it for Claude: a steer is asked for and refused."""

    def __init__(self):
        super().__init__()
        self.steers: list[str] = []

    def steer_turn(self, backend, agent_id, text, **kwargs):
        self.steers.append(kwargs.get("client_msg_id", ""))
        return False

    def by_id(self, backend):
        """Claude's catalogue entry, as far as steering goes: it cannot be steered."""
        from types import SimpleNamespace
        return SimpleNamespace(id=backend or self.CLAUDE, supports_steer=False)


class _World(World):
    def _new_process(self):
        super()._new_process()
        self.provider = _ClaudeLikeProvider()
        self.service.backends = self.provider

    def send(self, cid, *, origin="user", queue=False, trace=None):
        self.service.dispatch(
            text=cid, requested_session="mike", trace_id=trace or f"t-{cid}",
            client_msg_id=cid, synthesize_audio=False, queue_if_busy=queue,
            origin=origin, sender_agent_id="")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    yield
    td.reset_for_tests()


def _resume(world):
    """POST /turn-queue/resume: lift the pause, then recovery runs the queue."""
    assert turn_queue.set_paused(world.agent, False)
    world.service.recover_queued()
    world.settle()


def _delivery(world, cid):
    receipt = launch_receipts.delivery_for_send(cid, agent_id=world.agent, origin="user", sender_agent_id="")
    return (receipt or {}).get("state")


def _held_send(tmp_path, *, queue):
    world = _World(tmp_path)
    world.send("papa")                                   # 1
    world._collect()
    assert world.started == ["papa"]
    assert world.agent in world.provider.live            # 2: busy
    world.send("quebec", queue=queue)                    # 3
    world.stop()                                         # 4
    world.settle(recover=True)                           # 5: at rest, the queue held
    assert world.started == ["papa"] and turn_queue.is_paused(world.agent)
    return world


@pytest.mark.parametrize("queue", [False, True], ids=["plain send", "queue_if_busy"])
def test_a_send_held_by_the_users_stop_runs_after_the_resume(tmp_path, queue):
    world = _held_send(tmp_path, queue=queue)
    held = _delivery(world, "quebec")                    # 6 (recorded, not the claim here)
    _resume(world)                                       # 7
    assert world.started == ["papa", "quebec"], (       # 8
        f"the held send never ran after the resume (delivery before it read {held!r}; "
        f"steers asked: {world.provider.steers})")


def _queue_rows(client_msg_id):
    return [(row["queue_id"], row["trace_id"], row["status"]) for row in db.conn().execute(
        "SELECT queue_id, trace_id, status FROM queued_turns WHERE client_msg_id=? ORDER BY queue_seq",
        (client_msg_id,))]


def test_a_resend_of_a_held_send_answers_from_the_first_and_runs_it_once(tmp_path):
    """The corpus's second observation: the same client id sent again while held. It is answered
    from the first send: the same trace, no second queue entry; after the resume it runs once."""
    world = _held_send(tmp_path, queue=False)
    assert _queue_rows("quebec") == [("quebec", "t-quebec", "queued")]
    world.send("quebec", trace="t-quebec-again")         # the resend, under a new trace
    assert _queue_rows("quebec") == [("quebec", "t-quebec", "queued")]
    assert message_writes.client_message_trace("quebec") == "t-quebec"
    _resume(world)
    assert world.started.count("quebec") == 1, world.started
