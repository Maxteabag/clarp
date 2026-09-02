from __future__ import annotations

import threading

from lib import agents as agents_db
from lib import codex_app_server
from lib.codex_runner import _TurnState


class _Handle:
    def __init__(self):
        self._done = threading.Event()


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


def test_agent_message_deltas_grow_one_live_row_and_flush_on_completion():
    agent_id = agents_db.create_agent(
        persona="Caleb", voice_id="v", cwd="/tmp", session="caleb",
        backend="codex")
    agents_db.open_turn(
        agent_id=agent_id, source="pwa", trace_id="trace-1")
    client = object.__new__(codex_app_server._Client)
    client.agent_id = agent_id
    client.active = codex_app_server._ActiveTurn(
        turn_id="turn-1",
        thread_id="thread-1",
        agent_id=agent_id,
        session="caleb",
        trace_id="trace-1",
        state=_TurnState(live_backend_session_id="thread-1"),
        handle=_Handle(),
        on_result=None,
        on_error=None,
        stream=None,
        enqueue=lambda **_kwargs: 0,
    )

    client._notification("item/agentMessage/delta", {"delta": "Hel"})
    client._notification("item/agentMessage/delta", {"delta": "lo"})
    client._notification("turn/completed", {"turn": {"status": "completed"}})

    rows = agents_db.conn().execute(
        """SELECT text, kind FROM messages
             WHERE agent_id = ? AND source_file LIKE 'live:%'""",
        (agent_id,),
    ).fetchall()
    assert [(row["text"], row["kind"]) for row in rows] == [("Hello", "live")]


def test_normalize_item_keeps_official_activity_types_semantic():
    expected = {
        "dynamicToolCall": "dynamic_tool_call",
        "collabToolCall": "collab_tool_call",
        "collabAgentToolCall": "collab_agent_tool_call",
        "imageView": "image_view",
        "imageGeneration": "image_generation",
        "plan": "plan",
    }
    assert {
        source: codex_app_server._normalize_item({"type": source})["type"]
        for source in expected
    } == expected


def test_rate_limit_update_normalizes_and_broadcasts_new_events(monkeypatch):
    stream = _Stream()
    client = object.__new__(codex_app_server._Client)
    client.agent_id = "agent-1"
    client.active = codex_app_server._ActiveTurn(
        turn_id="turn-1", thread_id="thread-1", agent_id="agent-1",
        session="codex", trace_id="trace-1", state=_TurnState(),
        handle=_Handle(), on_result=None, on_error=None, stream=stream,
        enqueue=lambda **_kwargs: 0)
    seen = []

    def capture(payload):
        seen.append(payload)
        return {"limit_events": [{
            "type": "provider-limit", "kind": "warning",
            "provider_limit_event_id": "ple-1",
        }]}

    monkeypatch.setattr(
        codex_app_server.backend_usage, "capture_codex_rate_limits", capture)
    payload = {"rateLimits": {"primary": {"usedPercent": 82}}}
    client._notification("account/rateLimits/updated", payload)

    assert seen == [payload]
    assert stream.events == [{
        "type": "provider-limit", "kind": "warning",
        "provider_limit_event_id": "ple-1",
    }]

    client.active = None
    client.stream = stream
    client._notification("account/rateLimits/updated", payload)
    assert len(stream.events) == 2, "between-turn updates must still broadcast"


def test_client_factory_attaches_stream_before_initialization(monkeypatch):
    stream = _Stream()
    created = []

    class FakeClient:
        def __init__(self, agent_id, session, stream=None):
            self.agent_id = agent_id
            self.session = session
            self.stream = stream
            self.proc = type("Proc", (), {"poll": lambda _self: None})()
            created.append(self)

    monkeypatch.setattr(codex_app_server, "_Client", FakeClient)
    codex_app_server._CLIENTS.clear()
    client = codex_app_server._client("agent-1", "codex", stream=stream)
    assert created == [client]
    assert client.stream is stream
