"""Goals: the table, the Codex mirror, adopted continuation turns, routing."""
from __future__ import annotations

import threading

import pytest
from lib import agent_goals, backends, codex_app_server
from lib import agents as agents_db
from lib.backend.codex import TurnState as _TurnState
from lib.protocol import SSEType

from tests.unit.test_codex_app_server import _Handle, _install_fake_codex


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


def _agent(tmp_path, session="theo", persona="Theo"):
    agent_id = agents_db.create_agent(
        persona=persona, voice_id="v", cwd=str(tmp_path), session=session, backend="codex")
    agents_db.start_runtime(agent_id, session)
    agents_db.bind_backend_session(agent_id, f"thread-{session}")
    return agent_id


def test_table_round_trip_normalizes_codex_states(tmp_path):
    agent_id = _agent(tmp_path)
    row = agent_goals.upsert(agent_id, session="theo", backend="codex", goal=agent_goals.from_codex({
        "objective": "make the tests pass", "status": "usageLimited",
        "tokensUsed": 1200, "timeUsedSeconds": 30, "createdAt": 5, "updatedAt": 9}))
    assert row["status"] == "usage_limited"
    # Codex counts in seconds; a raw value would render as 1970 in the app.
    assert row["created_at"] == 5000 and row["updated_at"] == 9000
    public = agent_goals.public(row)
    assert public == {
        "objective": "make the tests pass", "status": "usage_limited", "token_budget": None,
        "tokens_used": 1200, "time_used_seconds": 30, "native": True, "backend": "codex",
        "created_at": 5000, "updated_at": 9000}
    assert agent_goals.by_agent()[agent_id]["objective"] == "make the tests pass"
    # A later update keeps the original start time when Codex omits it.
    again = agent_goals.upsert(agent_id, session="theo", backend="codex",
                               goal={"objective": "make the tests pass", "status": "active"})
    assert again["created_at"] == 5000 and again["updated_at"] >= 9000
    assert agent_goals.clear(agent_id) and agent_goals.get(agent_id) is None
    assert agent_goals.public(None) is None
    with pytest.raises(ValueError):
        agent_goals.upsert(agent_id, session="theo", backend="codex",
                           goal={"objective": "x", "status": "sideways"})


def test_codex_second_timestamps_become_milliseconds():
    """A real 0.154 goal: createdAt/updatedAt are epoch seconds."""
    row = agent_goals.from_codex({"objective": "x", "status": "active",
                                  "createdAt": 1789631121, "updatedAt": 1789631123})
    assert row["created_at"] == 1789631121000
    assert row["updated_at"] == 1789631123000
    # Already-millisecond values and junk are left alone.
    assert agent_goals.from_codex({"createdAt": 1789631121000})["created_at"] == 1789631121000
    assert agent_goals.from_codex({})["created_at"] is None
    assert agent_goals.from_codex({"createdAt": "soon"})["created_at"] is None


def _client(stream=None):
    client = object.__new__(codex_app_server._Client)
    client.agent_id = ""
    client.active = None
    client._actives = {}
    client._loaded_threads = set()
    client.stream = stream
    return client


def test_goal_notifications_are_mirrored_and_broadcast(tmp_path):
    agent_id = _agent(tmp_path)
    stream = _Stream()
    client = _client(stream)
    client._notification("thread/goal/updated", {"threadId": "thread-theo", "goal": {
        "threadId": "thread-theo", "objective": "ship it", "status": "active",
        "tokensUsed": 0, "timeUsedSeconds": 0, "createdAt": 1, "updatedAt": 1}})
    assert agent_goals.get(agent_id)["status"] == "active"
    assert stream.events[-1]["type"] == SSEType.GOAL_UPDATED
    assert stream.events[-1]["session"] == "theo"
    assert stream.events[-1]["goal"]["objective"] == "ship it"
    client._notification("thread/goal/cleared", {"threadId": "thread-theo"})
    assert agent_goals.get(agent_id) is None
    assert stream.events[-1]["goal"] is None
    # An unknown thread is ignored, not an error.
    client._notification("thread/goal/updated", {"threadId": "nobody", "goal": {"status": "active"}})
    assert len(stream.events) == 2


def test_server_started_turn_is_adopted_streams_and_completes(tmp_path):
    """A continuation turn Codex starts for a goal has no Clarp caller. It must
    still mark the agent busy, land its text in the transcript and end DONE."""
    agent_id = _agent(tmp_path)
    client = _client(_Stream())
    client._notification("turn/started", {
        "threadId": "thread-theo", "turn": {"id": "turn-g1", "threadId": "thread-theo"}})
    active = client._actives[agent_id]
    assert active.turn_id == "turn-g1" and active.handle.is_alive()
    assert agents_db.latest_state(agent_id)["kind"] == "thinking"
    client._notification("item/agentMessage/delta", {"threadId": "thread-theo", "delta": "Step one."})
    client._notification("turn/completed", {
        "threadId": "thread-theo", "turn": {"id": "turn-g1", "status": "completed"}})
    assert not active.handle.is_alive()
    assert agent_id not in client._actives
    deadline = threading.Event()
    for _ in range(50):
        if agents_db.latest_state(agent_id)["kind"] == "done":
            break
        deadline.wait(0.05)
    assert agents_db.latest_state(agent_id)["kind"] == "done"
    rows = agents_db.conn().execute(
        "SELECT text FROM messages WHERE agent_id = ? AND text LIKE 'Step one%'",
        (agent_id,)).fetchall()
    assert rows
    # No agent owns that thread: nothing to adopt, nothing raised.
    client._notification("turn/started", {"threadId": "ghost", "turn": {"id": "t2"}})
    assert "ghost" not in {a.thread_id for a in client._actives.values()}


def test_goal_lifecycle_against_fake_appserver(tmp_path, monkeypatch):
    _install_fake_codex(tmp_path, monkeypatch)
    agent_id = agents_db.create_agent(
        persona="Ada", voice_id="v", cwd=str(tmp_path), session="ada", backend="codex")
    done = threading.Event()

    def bind(thread_id):
        # What the dispatcher does on a first turn: give the agent its thread.
        agents_db.start_runtime(agent_id, "ada")
        agents_db.bind_backend_session(agent_id, thread_id)
        return True

    codex_app_server.spawn_turn(
        text="hello", cwd=tmp_path, is_new_session=True, session="ada", agent_id=agent_id,
        on_session_init=bind, on_result=lambda _payload: done.set(),
        on_error=lambda _m: done.set(), trace_id="tr-ada", stream=_Stream(),
        enqueue=lambda **_kwargs: 0)
    assert done.wait(8)
    assert agents_db.live_backend_session(agent_id)
    stream = _Stream()
    started = codex_app_server.goal(agent_id, "start", objective="finish the report", stream=stream)
    assert started["objective"] == "finish the report"
    assert started["status"] in ("active", "complete")
    # The fake continues the goal with a turn of its own and completes it.
    for _ in range(80):
        if (agent_goals.get(agent_id) or {}).get("status") == "complete":
            break
        threading.Event().wait(0.05)
    row = agent_goals.get(agent_id)
    assert row["status"] == "complete" and row["tokens_used"] == 7
    assert any(e["type"] == SSEType.GOAL_UPDATED for e in stream.events)
    for _ in range(80):
        if agents_db.latest_state(agent_id)["kind"] == "done":
            break
        threading.Event().wait(0.05)
    text_rows = agents_db.conn().execute(
        "SELECT text FROM messages WHERE agent_id = ? AND text LIKE 'Goal step:%'",
        (agent_id,)).fetchall()
    assert text_rows, "the adopted continuation turn must reach the transcript"
    paused = codex_app_server.goal(agent_id, "pause", stream=stream)
    assert paused["status"] == "paused"
    resumed = codex_app_server.goal(agent_id, "resume", stream=stream)
    assert resumed["status"] in ("active", "complete")
    assert codex_app_server.goal(agent_id, "clear", stream=stream) is None
    assert agent_goals.get(agent_id) is None
    codex_app_server.recycle_clients()


def test_goal_requires_a_conversation_and_an_objective(tmp_path):
    agent_id = agents_db.create_agent(
        persona="New", voice_id="v", cwd=str(tmp_path), session="new", backend="codex")
    client = _client()
    with pytest.raises(ValueError, match="message first"):
        client.goal(agents_db.get_by_agent_id(agent_id), "start", "x")
    bound = _agent(tmp_path, session="bound", persona="Bound")
    with pytest.raises(ValueError, match="objective"):
        client.goal(agents_db.get_by_agent_id(bound), "start", "  ")
    with pytest.raises(ValueError, match="unknown goal action"):
        client.goal(agents_db.get_by_agent_id(bound), "dance")


def test_stop_pauses_an_active_goal_before_interrupting(tmp_path):
    agent_id = _agent(tmp_path)
    agent_goals.upsert(agent_id, session="theo", backend="codex",
                       goal={"objective": "keep going", "status": "active"})
    client = _client(_Stream())
    sent = []

    def fake_request(method, params, timeout=30):
        sent.append((method, params))
        if method == "thread/goal/set":
            return {"goal": {"threadId": "thread-theo", "objective": "keep going",
                             "status": "paused", "tokensUsed": 0, "timeUsedSeconds": 0,
                             "createdAt": 1, "updatedAt": 2}}
        return {}

    client.request = fake_request
    client._actives[agent_id] = codex_app_server._ActiveTurn(
        "turn-1", "thread-theo", agent_id, "theo", "tr", _TurnState(), _Handle(),
        None, None, None, lambda **_kwargs: 0)
    client.interrupt_active(agent_id)
    assert [m for m, _ in sent] == ["thread/goal/set", "turn/interrupt"]
    assert sent[0][1]["status"] == "paused"
    assert agent_goals.get(agent_id)["status"] == "paused"


def test_backends_refuse_goals_outside_codex(tmp_path):
    with pytest.raises(backends.GoalUnsupported, match="Claude"):
        backends.goal("claude", "agent", "start", objective="x")
    with pytest.raises(backends.GoalUnsupported, match="OpenCode"):
        backends.goal("opencode", "agent", "start", objective="x")


def test_runtime_bridge_goal_method_maps_errors(tmp_path, monkeypatch):
    from lib import runtime_bridge

    class _Ctx:
        stream = _Stream()

    class _Service:
        ctx = _Ctx()

    server = object.__new__(runtime_bridge.RuntimeRPCServer)
    server.dispatch_service = _Service()
    missing = server._dispatch_request("goal", {"agent_id": "nope", "action": "start"})
    assert missing["status"] == 404
    claude_id = agents_db.create_agent(
        persona="Cl", voice_id="v", cwd=str(tmp_path), session="cl", backend="claude")
    unsupported = server._dispatch_request("goal", {"agent_id": claude_id, "action": "start"})
    assert unsupported["status"] == 501 and "Claude" in unsupported["error"]
    codex_id = agents_db.create_agent(
        persona="Cx", voice_id="v", cwd=str(tmp_path), session="cx", backend="codex")
    monkeypatch.setattr(codex_app_server, "goal",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("objective required")))
    bad = server._dispatch_request("goal", {"agent_id": codex_id, "action": "start"})
    assert bad["status"] == 400
    monkeypatch.setattr(codex_app_server, "goal",
                        lambda *a, **k: {"objective": "x", "status": "active"})
    ok = server._dispatch_request("goal", {"agent_id": codex_id, "action": "start",
                                           "objective": "x"})
    assert ok == {"ok": True, "result": {"objective": "x", "status": "active"}}


def test_snapshot_carries_the_goal(tmp_path):
    from lib.audio_stream import AudioStream
    from lib.context import ServerContext, StubSTT
    from lib.snapshot import build_agent_snapshot
    from lib.tts_engine import FakeTTSEngine
    agent_id = _agent(tmp_path, session="snap", persona="Snap")
    agent_goals.upsert(agent_id, session="snap", backend="codex",
                       goal={"objective": "look busy", "status": "active"})
    ctx = ServerContext(
        root=tmp_path, static=tmp_path, audio_dir=tmp_path / "audio",
        agents_path=tmp_path / "agents.json", default_session="snap",
        tts=FakeTTSEngine(tmp_path / "audio"), stream=AudioStream(tmp_path / "audio"),
        stt=StubSTT(), roster_names=("Snap",))
    rows = {row["session"]: row for row in build_agent_snapshot(ctx)["agents"]}
    assert rows["snap"]["goal"]["objective"] == "look busy"
    assert rows["snap"]["goal"]["status"] == "active"
