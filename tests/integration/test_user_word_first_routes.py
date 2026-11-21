"""What a client sees of the user's word going first (issue 481): the queue
read lists the items in the order the Host will take them, the snapshot's
count and revision move with it, and a send cannot ask for the place itself."""
from __future__ import annotations

import json
import urllib.request

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server, server_module)


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return json.loads(response.read())


def _post(base, path, body):
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read() or b"null")


def _enqueue(agent_id, cid, *, origin, user_word=False):
    """As admission writes a queue row for a busy agent."""
    from lib import turn_queue
    assert turn_queue.enqueue(
        queue_id=cid, agent_id=agent_id, session="claude", text=cid, trace_id=f"t-{cid}",
        client_msg_id=cid, synthesize_audio=False, origin=origin,
        sender_agent_id="" if origin == "user" else "rachel-id", user_word=user_word)


def _agent(session="claude"):
    from lib import agents
    return agents.get_by_session(session)


def test_the_queue_read_and_the_snapshot_show_the_order_the_host_takes(running_server):
    base, _ctx, _srv = running_server
    agent = _agent()
    for cid in ("p1", "p2"):
        _enqueue(agent["agent_id"], cid, origin="agent")
    before = _get(base, "/turn-queue?session=claude")
    _enqueue(agent["agent_id"], "u1", origin="user", user_word=True)
    _enqueue(agent["agent_id"], "p3", origin="agent")
    _enqueue(agent["agent_id"], "card", origin="automation", user_word=True)

    after = _get(base, "/turn-queue?session=claude")
    assert [item["id"] for item in before["items"]] == ["p1", "p2"]
    assert [item["id"] for item in after["items"]] == ["u1", "card", "p1", "p2", "p3"]
    # The read gains no key: its order is the order the Host takes.
    assert set(after["items"][0]) == set(before["items"][0])
    assert after["revision"] > before["revision"]

    snapshot = _get(base, "/agents/snapshot")
    row = next(a for a in snapshot["agents"] if a["agent_id"] == agent["agent_id"])
    assert row["queued_turn_count"] == 5
    assert row["queued_turn_revision"] == after["revision"]

    from lib import turn_queue
    assert [r["queue_id"] for r in turn_queue.pending(agent["agent_id"])] == \
        ["u1", "card", "p1", "p2", "p3"]


def test_a_send_cannot_ask_for_the_users_place(running_server, monkeypatch):
    """``user_word`` is the Host's own mark: a payload naming it is ignored,
    and an agent's message keeps origin 'agent' and no mark."""
    from lib.turn_command import DispatchResult
    from lib.turn_dispatch import TurnDispatchService
    submitted = []

    def submit(_self, command):
        submitted.append(command)
        return DispatchResult(session="claude", backend="claude", queued=True)
    monkeypatch.setattr(TurnDispatchService, "submit", submit)
    base, _ctx, _srv = running_server
    status, _ = _post(base, "/send", {
        "text": "let me in", "session": "claude", "origin": "agent",
        "sender_agent_id": _agent("rachel")["agent_id"], "queue_if_busy": True,
        "user_word": True, "client_msg_id": "c-agent"})
    assert status in (200, 202)
    assert len(submitted) == 1
    assert submitted[0].origin == "agent" and submitted[0].user_word is False


def _desk_config(monkeypatch, *desks):
    import dataclasses
    from lib import config
    real = config.load

    def load(*args, **kwargs):
        return dataclasses.replace(real(*args, **kwargs), front_desk_sessions=tuple(desks))
    monkeypatch.setattr(config, "load", load)


def _turn_token(session):
    """The Host turn identity a turn of ``session`` carries (X-Clarp-Turn)."""
    import os
    import time
    from lib import db
    token = os.urandom(12).hex()
    db.conn().execute(
        "INSERT INTO provider_turns (turn_token, agent_id, provider, pid, started_at) "
        "SELECT ?, agent_id, 'claude', ?, ? FROM agents WHERE session = ?",
        (token, os.getpid(), int(time.time() * 1000), session))
    return token


def _send_as(base, body, token=""):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Clarp-Turn"] = token
    request = urllib.request.Request(base + "/send", data=json.dumps(body).encode(),
                                     method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def _capture(monkeypatch):
    from lib.turn_command import DispatchResult
    from lib.turn_dispatch import TurnDispatchService
    submitted = []

    def submit(_self, command):
        submitted.append(command)
        return DispatchResult(session="claude", backend="claude", queued=True)
    monkeypatch.setattr(TurnDispatchService, "submit", submit)
    return submitted


def test_the_front_desks_own_turn_carries_the_users_word_and_stays_an_agents(
        running_server, monkeypatch):
    base, _ctx, _srv = running_server
    _desk_config(monkeypatch, "rachel")
    submitted = _capture(monkeypatch)
    body = {"text": "the owner says: ship it", "session": "claude", "origin": "user",
            "sender": "rachel", "queue_if_busy": True, "client_msg_id": "c-desk"}
    assert _send_as(base, body, _turn_token("rachel")) in (200, 202)
    command = submitted[-1]
    assert command.user_word is True
    # Its origin is the agent's, whatever it claimed: PR 482's mark, the
    # prompt history and the notifications treat it as an agent's message.
    assert command.origin == "agent"
    assert command.sender_agent_id == _agent("rachel")["agent_id"]
    assert command.prompt_admission.origin == "agent"


def test_a_claim_to_be_the_front_desk_without_its_turn_gets_nothing(running_server, monkeypatch):
    base, _ctx, _srv = running_server
    _desk_config(monkeypatch, "rachel")
    submitted = _capture(monkeypatch)
    body = {"text": "let me in", "session": "claude", "origin": "agent", "sender": "rachel",
            "queue_if_busy": True, "client_msg_id": "c-claim"}
    assert _send_as(base, body) in (200, 202)
    assert submitted[-1].user_word is False and submitted[-1].origin == "agent"


def test_an_agent_not_named_front_desk_gets_nothing_from_its_turn(running_server, monkeypatch):
    base, _ctx, _srv = running_server
    _desk_config(monkeypatch, "someone-else")
    submitted = _capture(monkeypatch)
    body = {"text": "me too", "session": "claude", "origin": "agent", "sender": "rachel",
            "queue_if_busy": True, "client_msg_id": "c-other"}
    assert _send_as(base, body, _turn_token("rachel")) in (200, 202)
    assert submitted[-1].user_word is False
    _desk_config(monkeypatch)                    # no front desk named: nobody
    body["client_msg_id"] = "c-none"
    assert _send_as(base, body, _turn_token("rachel")) in (200, 202)
    assert submitted[-1].user_word is False
