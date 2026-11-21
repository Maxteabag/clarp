"""R182 (issue 491): GET /turn-queue says who each waiting item is from, so an app draws the
user's own send from another device and not an agent's message or a goal wake as the user's."""
from __future__ import annotations

import json
import queue

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _delete, _get, _put, fake_ctx, running_server,
)


def _enqueue(agent, queue_id, *, origin, sender=""):
    from lib import turn_queue
    turn_queue.enqueue(
        queue_id=queue_id, agent_id=agent["agent_id"], session="claude", text=queue_id,
        trace_id=f"trace-{queue_id}", client_msg_id=queue_id, synthesize_audio=False,
        origin=origin, sender_agent_id=sender)


def _items(base):
    status, body = _get(base + "/turn-queue?session=claude")
    assert status == 200
    return {item["id"]: item for item in json.loads(body)["items"]}


def test_each_waiting_item_says_who_it_is_from(running_server):
    from lib import agents as agents_db
    base, _ctx, _srv = running_server
    agent = agents_db.get_by_session("claude")
    mike = agents_db.create_agent(persona="Mike", voice_id="v", cwd="/tmp", session="mike-r182")
    _enqueue(agent, "from-the-phone", origin="user")                   # the user, another device
    _enqueue(agent, "from-mike", origin="agent", sender=mike)          # another agent's message
    _enqueue(agent, "goal-wake-r182", origin="automation")             # a goal wake
    _enqueue(agent, "from-a-gone-agent", origin="agent", sender="agent-that-is-gone")
    items = _items(base)
    assert {k: (v["origin"], v["sender"]) for k, v in items.items()} == {
        "from-the-phone": ("user", None),
        "from-mike": ("agent", {"agent_id": mike, "name": "Mike"}),
        "goal-wake-r182": ("automation", None),
        "from-a-gone-agent": ("agent", {"agent_id": "agent-that-is-gone", "name": None}),
    }
    # The keys the item had before are unchanged.
    assert items["from-the-phone"]["client_msg_id"] == "from-the-phone"
    assert items["from-the-phone"]["text"] == "from-the-phone"


def test_the_events_that_name_an_item_say_who_it_is_from(running_server):
    from lib import agents as agents_db
    base, ctx, _srv = running_server
    agent = agents_db.get_by_session("claude")
    mike = agents_db.create_agent(persona="Mike", voice_id="v", cwd="/tmp", session="mike-r182")
    _enqueue(agent, "from-mike", origin="agent", sender=mike)
    _enqueue(agent, "from-the-phone", origin="user")
    events = ctx.stream.subscribe()
    try:
        assert _put(base + "/turn-queue/from-mike", {"text": "edited"})[0] == 200
        assert _delete(base + "/turn-queue/from-the-phone")[0] == 200
        seen = []
        while len(seen) < 2:
            try:
                event = json.loads(events.get(timeout=2))
            except queue.Empty:
                break
            if event.get("type") == "queue-updated" and event.get("queue_change"):
                seen.append(event)
    finally:
        ctx.stream.unsubscribe(events)
    assert [(e["client_msg_id"], e["queue_change"], e["origin"], e["sender"]) for e in seen] == [
        ("from-mike", "edited", "agent", {"agent_id": mike, "name": "Mike"}),
        ("from-the-phone", "deleted", "user", None),
    ]
