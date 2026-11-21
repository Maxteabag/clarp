"""An accepted steer says its item started, as the handover says it for an item
whose turn starts: after the commit and after the row's own event, one
``queue-updated`` with ``queue_started`` true, the item's ``client_msg_id`` and
who it is from (origin, sender; R182)."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from lib import turn_queue

from test_waited_send_order import _SteeredWorld, _send, clock  # noqa: F401 - fixture


def _capture(world):
    seen: list[dict] = []
    world.service.ctx.stream = SimpleNamespace(broadcast=seen.append)
    return seen


def _started_events(seen, cid):
    return [i for i, e in enumerate(seen) if e.get("type") == "queue-updated"
            and e.get("queue_started") is True and e.get("client_msg_id") == cid]


@pytest.mark.parametrize("start", ["at once", "after a failed start"])
def test_an_accepted_steer_says_its_item_started_after_its_row(tmp_path, clock, monkeypatch, start):
    world = _SteeredWorld(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    seen = _capture(world)
    if start == "after a failed start":
        real, fails = turn_queue.start_receipt, {"left": 1}

        def failing_start(queue_id):
            if queue_id == "two" and fails["left"]:
                fails["left"] -= 1
                raise sqlite3.OperationalError("database is locked")
            return real(queue_id)
        monkeypatch.setattr(turn_queue, "start_receipt", failing_start)
    clock.at(7_600)
    answer = _send(world, "two")
    assert answer.queued is False and world.provider.steered == ["two"]
    assert turn_queue.status("two") == "started"
    started = _started_events(seen, "two")
    assert len(started) == 1, seen
    event = seen[started[0]]
    assert event["origin"] == "user" and event.get("sender") is None
    assert "queue_depth" in event and "queue_paused" in event and "queue_revision" in event
    rows = [i for i, e in enumerate(seen) if e.get("type") == "transcript-updated"]
    assert rows and rows[0] < started[0]                  # the row's event first


def test_a_steer_whose_start_never_succeeds_says_nothing_started(tmp_path, clock, monkeypatch):
    world = _SteeredWorld(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    seen = _capture(world)
    real = turn_queue.start_receipt

    def failing_start(queue_id):
        if queue_id == "two":
            raise sqlite3.OperationalError("database is locked")
        return real(queue_id)
    monkeypatch.setattr(turn_queue, "start_receipt", failing_start)
    clock.at(7_600)
    _send(world, "two")
    assert _started_events(seen, "two") == []
