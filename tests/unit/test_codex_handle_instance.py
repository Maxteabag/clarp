"""TP-N1 review: a Codex turn handle cancels only the turn it was returned for.
The dispatcher may terminate a handle late (a retry that lost its slot during
the handoff); by then the agent can be running its next turn, which a stale
handle must leave alone."""
from __future__ import annotations

from tests.codex_request import result_reply

import threading
from unittest.mock import Mock

from lib import codex_app_server as cas


def _client_with_turn(agent_id):
    client = object.__new__(cas._Client)
    client.agent_id, client.active, client.thread_id = agent_id, None, "synthetic-thread"
    client._actives, client._loaded_threads, client.rate_limits = {}, set(), {}
    client._lock = threading.Lock()
    client.proc = Mock()
    requests = []
    client.request = result_reply(lambda method, params, timeout=30: requests.append((method, params)) or {})
    return client, requests


def _active(client, agent_id, turn_id):
    handle = cas.AppTurnHandle(client, agent_id=agent_id)
    active = cas._ActiveTurn(turn_id, "synthetic-thread", agent_id, "synthetic", f"trace-{turn_id}",
                             cas.TurnState(live_backend_session_id="synthetic-thread"),
                             handle, None, None, None, lambda *a, **k: 0)
    client._actives[agent_id] = active
    return handle


def test_a_finished_handle_does_not_interrupt_the_agents_next_turn():
    client, requests = _client_with_turn("synthetic-agent")
    old = _active(client, "synthetic-agent", "turn-a")
    old._done.set()
    _active(client, "synthetic-agent", "turn-b")

    old.terminate()

    assert [m for m, _ in requests if m == "turn/interrupt"] == []


def test_a_live_handle_still_interrupts_its_own_turn():
    client, requests = _client_with_turn("synthetic-agent")
    live = _active(client, "synthetic-agent", "turn-b")

    live.terminate()

    assert [p["turnId"] for m, p in requests if m == "turn/interrupt"] == ["turn-b"]


def test_a_handle_whose_turn_was_replaced_does_not_interrupt_the_replacement():
    client, requests = _client_with_turn("synthetic-agent")
    old = _active(client, "synthetic-agent", "turn-a")
    _active(client, "synthetic-agent", "turn-b")

    old.terminate()

    assert [m for m, _ in requests if m == "turn/interrupt"] == []
