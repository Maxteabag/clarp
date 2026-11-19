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
    provider_class = _ClaudeLikeProvider

    def _new_process(self):
        """World's process, with this provider in place BEFORE the boot recovery runs."""
        import test_drain_pause_parity as parity
        real = parity.Provider
        parity.Provider = lambda: self.make_provider()
        try:
            super()._new_process()
        finally:
            parity.Provider = real

    def make_provider(self):
        return self.provider_class()

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


# ---- (B) Codex can be steered: a receipt says ATTEMPTED only once the steer can have left ----

class _CodexLikeProvider(_ClaudeLikeProvider):
    """A backend that can be steered but answers False: nothing was sent (no active turn yet)."""

    def __init__(self, *, raises=False):
        super().__init__()
        self.raises = raises

    def steer_turn(self, backend, agent_id, text, **kwargs):
        self.steers.append(kwargs.get("client_msg_id", ""))
        if self.raises:
            raise OSError("the app-server socket broke during turn/steer")
        return False

    def by_id(self, backend):
        from types import SimpleNamespace
        return SimpleNamespace(id="codex", supports_steer=True)


class _CodexWorld(_World):
    raises = False

    def make_provider(self):
        return _CodexLikeProvider(raises=self.raises)


def _receipt(trace):
    return (launch_receipts.get(trace) or {}).get("state")


@pytest.mark.parametrize("then", ["stop and resume", "restart"])
def test_a_codex_steer_answered_false_leaves_the_receipt_pending_and_the_send_runs_once(tmp_path, then):
    world = _CodexWorld(tmp_path)
    world.send("papa")
    world._collect()
    world.send("quebec")
    assert world.provider.steers == ["quebec"]
    assert _receipt("t-quebec") == launch_receipts.PENDING          # nothing left: not ATTEMPTED
    if then == "stop and resume":
        world.stop()
        world.settle(recover=True)
        assert world.started == ["papa"]
        _resume(world)
    else:
        world.restart()
    assert world.started.count("quebec") == 1, world.started


def test_a_codex_steer_that_raised_stays_possibly_sent(tmp_path):
    """An exception during the request may have sent it: the receipt stays ATTEMPTED, as before."""
    _CodexWorld.raises = True
    try:
        world = _CodexWorld(tmp_path)
        world.send("papa")
        world._collect()
        world.send("quebec")
    finally:
        _CodexWorld.raises = False
    assert _receipt("t-quebec") == launch_receipts.ATTEMPTED


def test_codex_answers_false_to_a_steer_only_before_it_sends_anything():
    """Backend.steer's contract on Codex's client: no active turn, or one not yet started, answers
    False and writes no request."""
    import threading
    from types import SimpleNamespace
    from lib import codex_app_server
    client = codex_app_server._Client.__new__(codex_app_server._Client)
    sent = []
    client.request = lambda method, params, *a, **k: sent.append(method)
    client._actives, client.active = {}, None
    assert client.steer("words", agent_id="agent-1") is False and sent == []
    not_started = SimpleNamespace(handle=SimpleNamespace(is_alive=lambda: True),
                                  steer_ready=SimpleNamespace(wait=lambda timeout: False), turn_id="")
    client._actives = {"agent-1": not_started}
    assert client.steer("words", agent_id="agent-1") is False and sent == []
    started = SimpleNamespace(handle=SimpleNamespace(is_alive=lambda: True), steer_ready=threading.Event(),
                              turn_id="turn-1", thread_id="thread-1", voice=True, session="s")
    started.steer_ready.set()
    client._actives = {"agent-1": started}
    assert client.steer("words", agent_id="agent-1") is True and sent == ["turn/steer"]


# ---- the one-time pass for rows the defect left (Arnold's rule) ----------------------------------

def _plant_stuck_send(world, cid, *, age_ms):
    """What the defect left: a queued user send, its receipt ATTEMPTED, no turn ever for its trace."""
    trace = f"t-{cid}"
    turn_queue.enqueue(queue_id=cid, agent_id=world.agent, session="mike", text=cid, trace_id=trace,
                       client_msg_id=cid, synthesize_audio=False, origin="user", sender_agent_id="")
    launch_receipts.record_pending(agent_id=world.agent, trace_id=trace, client_msg_id=cid, origin="user")
    assert launch_receipts.mark_attempted(trace) == "attempted"
    db.conn().execute("UPDATE queued_turns SET enqueued_at=? WHERE queue_id=?", (db.now_ms() - age_ms, cid))
    db.conn().commit()


def _status(cid):
    return db.conn().execute("SELECT status FROM queued_turns WHERE queue_id=?", (cid,)).fetchone()[0]


def _notices(world):
    return [row["text"] for row in db.conn().execute(
        "SELECT text FROM messages WHERE agent_id=? AND origin='system' AND message_id LIKE 'notice:held_send_not_run:%'",
        (world.agent,))]


def test_a_stuck_send_younger_than_ten_minutes_runs_after_the_next_runtime_start(tmp_path):
    world = _World(tmp_path)
    _plant_stuck_send(world, "young", age_ms=60_000)
    world.restart()
    assert world.started == ["young"] and _status("young") != "failed" and _notices(world) == []


def test_an_older_stuck_send_ends_failed_and_its_sender_is_told(tmp_path):
    world = _World(tmp_path)
    _plant_stuck_send(world, "old", age_ms=9 * 3600_000)
    world.restart()
    assert world.started == [] and _status("old") == "failed"
    assert _notices(world) == [turn_queue.HELD_SEND_NOTICE]
    world.restart()                                                  # once: no second notice
    assert len(_notices(world)) == 1


def test_a_stuck_send_on_a_steerable_backend_is_left_and_logged(tmp_path):
    world = _CodexWorld(tmp_path)
    _plant_stuck_send(world, "codex-old", age_ms=9 * 3600_000)
    world.restart()
    assert world.started == [] and _status("codex-old") == "queued" and _notices(world) == []
    assert launch_receipts.get("t-codex-old")["state"] == launch_receipts.ATTEMPTED


def test_a_send_whose_trace_has_a_turn_is_not_touched(tmp_path):
    world = _World(tmp_path)
    _plant_stuck_send(world, "ran", age_ms=9 * 3600_000)
    from lib import turn_lifecycle
    turn_lifecycle.open_turn(agent_id=world.agent, source="pwa", trace_id="t-ran")
    done = turn_queue.settle_unsent_held_sends(can_steer=lambda backend: False)
    assert done == {"released": [], "failed": [], "left": []} and _status("ran") == "queued"
