"""A user's Stop holds the queue until the user's next fresh message, and a shown
item a backend already took never stays 'queued' until a restart.

Through the real dispatcher (test_drain_pause_parity.World).
"""
from __future__ import annotations

import pytest

from lib import agents as agents_db
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


def _stopped_with_held_work(tmp_path):
    """The user writes while the agent works (a), another agent's message waits (p1),
    then the user presses Stop: both are held."""
    world = World(tmp_path)
    world.send("t0")
    world.send("a", queue=True)
    world.send("p1", origin="agent", queue=True)
    world.stop()
    world.settle()
    assert world.started == ["t0"] and turn_queue.is_paused(world.agent)
    assert _waiting(world) == ["a", "p1"]
    return world


@pytest.mark.parametrize("fresh", ["user", "oracle"])
def test_the_users_next_message_ends_the_pause_and_the_held_work_runs_after_it(tmp_path, fresh):
    world = _stopped_with_held_work(tmp_path)
    world.send("b", origin=fresh)                    # the user carries on
    assert not turn_queue.is_paused(world.agent)
    world.settle(recover=True)
    assert world.started == ["t0", "b", "a", "p1"]   # held work after it, the user's first
    assert _waiting(world) == []


def test_a_send_now_of_a_held_item_does_not_end_the_pause(tmp_path):
    world = _stopped_with_held_work(tmp_path)
    world.service.dispatch_queued("a")
    world.settle(recover=True)
    assert "a" in world.started and "p1" not in world.started
    assert turn_queue.is_paused(world.agent)
    assert _waiting(world) == ["p1"]


@pytest.mark.parametrize("origin", ["agent", "automation", "schedule", "heartbeat"])
def test_automatic_work_does_not_end_the_pause(tmp_path, origin):
    world = _stopped_with_held_work(tmp_path)
    try:
        world.send("x", origin=origin, queue=True)
    except Exception:  # noqa: BLE001 - refused by its own rule: still no release
        pass
    world.settle(recover=True)
    assert turn_queue.is_paused(world.agent)
    assert "a" not in world.started and "p1" not in world.started


def test_a_stop_with_nothing_after_it_stays_in_force_across_a_restart(tmp_path):
    world = _stopped_with_held_work(tmp_path)
    world.restart()
    assert world.started == ["t0"] and turn_queue.is_paused(world.agent)
    assert _waiting(world) == ["a", "p1"]
    world.send("b")                                  # and the next fresh message still ends it
    world.settle(recover=True)
    assert world.started == ["t0", "b", "a", "p1"]


# --- a shown item a backend took -------------------------------------------------

def _shown_row(world, cid, *, turn: str | None):
    """A queue row whose conversation row is written (shown), with a turn under its
    trace that is ended ('ended'), open ('open') or none."""
    turn_queue.enqueue(queue_id=cid, agent_id=world.agent, session="mike", text=cid,
                       trace_id=f"t-{cid}", client_msg_id=cid, synthesize_audio=False,
                       origin="agent", sender_agent_id="other-agent", shown=True)
    if turn is not None:
        agents_db.conn().execute(
            "INSERT INTO turns (agent_id, source, trace_id, started_at, ended_at, settled_at, outcome)"
            " VALUES (?, 'pwa', ?, 1, ?, ?, ?)",
            (world.agent, f"t-{cid}", 2 if turn == "ended" else None,
             2 if turn == "ended" else None, "completed" if turn == "ended" else None))


def test_a_shown_item_taken_by_an_ended_turn_leaves_queued_on_a_recovery_pass(tmp_path):
    world = World(tmp_path)
    world.send("t0")                                 # busy: nothing of these would start
    _shown_row(world, "taken", turn="ended")
    _shown_row(world, "running", turn="open")
    _shown_row(world, "waits", turn=None)
    world.service.recover_queued()                   # an ordinary pass, not a runtime start
    assert turn_queue.status("taken") != "queued"
    assert turn_queue.status("running") == "queued"  # its turn still runs
    assert turn_queue.status("waits") == "queued"    # it waits for its own turn
    assert "taken" not in _waiting(world)


def test_a_delivered_steer_leaves_queued_on_any_pass(tmp_path, monkeypatch):
    from lib import launch_receipts
    world = World(tmp_path)
    world.send("t0")
    _shown_row(world, "steered", turn=None)
    real = launch_receipts.get
    monkeypatch.setattr(launch_receipts, "get", lambda trace: (
        {"state": launch_receipts.ATTEMPTED} if trace == "t-steered" else real(trace)))
    world.service.recover_queued()
    assert turn_queue.status("steered") != "queued"
