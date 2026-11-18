"""A turn's own id never reaches a row of another agent (R62, issue 360)."""
from __future__ import annotations

import pytest

from lib import agents as agents_db, log as log_module, message_writes

SOURCE = "/tmp/fixture.jsonl"


def _agent(tmp_path, name: str, conversation: str) -> str:
    agent_id = agents_db.create_agent(persona=name.title(), voice_id="v", cwd=str(tmp_path), session=name)
    agents_db.start_runtime(agent_id, "codex")
    agents_db.bind_backend_session(agent_id, conversation)
    return agent_id


def _store(agent_id: str, conversation: str, *turns: dict) -> None:
    agents_db.store_transcript_turns(agent_id=agent_id, backend_session_id=conversation,
                                     source_file=SOURCE, turns=list(turns))


def _rows(agent_id: str) -> list[tuple]:
    return [tuple(row) for row in agents_db.conn().execute(
        "SELECT * FROM messages WHERE agent_id = ? ORDER BY message_id", (agent_id,)).fetchall()]


def _head(agent_id: str) -> list[tuple]:
    return [tuple(row) for row in agents_db.conn().execute(
        "SELECT * FROM conversation_heads WHERE agent_id = ? ORDER BY backend_session_id", (agent_id,)).fetchall()]


@pytest.fixture
def logged(monkeypatch):
    lines: list[tuple[str, str]] = []
    monkeypatch.setattr(log_module, "log", lambda event, detail="": lines.append((event, detail)))
    monkeypatch.setitem(message_writes._FOREIGN_TURN_IDS, "count", 0)
    monkeypatch.setitem(message_writes._FOREIGN_TURN_IDS, "logged_at", None)
    return lines


def _warnings(lines):
    return [detail for event, detail in lines if event == "messageTurnIdOfAnotherAgent"]


def test_an_id_held_by_another_agent_is_stored_under_the_writers_own_position(tmp_path, logged):
    first = _agent(tmp_path, "first", "conv-a")
    second = _agent(tmp_path, "second", "conv-b")
    _store(first, "conv-a", {"id": "g", "role": "assistant", "text": "First's answer.", "timestamp": "t1"})
    rows, head = _rows(first), _head(first)
    second_head = _head(second)

    _store(second, "conv-b", {"id": "g", "role": "assistant", "text": "Second's final answer.", "timestamp": "t2"})

    assert _rows(first) == rows and _head(first) == head
    [(message_id, text)] = [(r["message_id"], r["text"]) for r in agents_db.conn().execute(
        "SELECT message_id, text FROM messages WHERE agent_id = ?", (second,))]
    assert (message_id, text) == (message_writes._message_id(second, "conv-b", SOURCE, 0), "Second's final answer.")
    assert _head(second) != second_head
    assert _warnings(logged) == ["count=1"]
    assert [detail for _, detail in logged if "g" in detail.split("=", 1)[-1] or "answer" in detail] == []


def test_the_warning_is_one_line_a_minute_with_a_count(tmp_path, logged, monkeypatch):
    first = _agent(tmp_path, "first", "conv-a")
    second = _agent(tmp_path, "second", "conv-b")
    _store(first, "conv-a", *[{"id": f"g{i}", "role": "assistant", "text": "x", "timestamp": f"t{i}"} for i in range(4)])
    clock = [1000.0]
    monkeypatch.setattr(message_writes.time, "monotonic", lambda: clock[0])
    _store(second, "conv-b", {"id": "g0", "role": "assistant", "text": "y", "timestamp": "u0"})
    clock[0] += 59
    _store(second, "conv-b", {"id": "g1", "role": "assistant", "text": "y", "timestamp": "u1"},
           {"id": "g2", "role": "assistant", "text": "y", "timestamp": "u2"})
    assert _warnings(logged) == ["count=1"]
    clock[0] += 1
    _store(second, "conv-b", {"id": "g3", "role": "assistant", "text": "y", "timestamp": "u3"})
    assert _warnings(logged) == ["count=1", "count=3"]


def test_the_same_agents_id_is_kept_also_from_another_session(tmp_path, logged):
    """carry_conversation moves an agent's rows to another session with their ids."""
    first = _agent(tmp_path, "first", "conv-a")
    _store(first, "conv-a", {"id": "g", "role": "assistant", "text": "Before.", "timestamp": "t1"})
    _store(first, "conv-b", {"id": "g", "role": "assistant", "text": "After.", "timestamp": "t2"})
    texts = {r["message_id"]: r["text"] for r in agents_db.conn().execute(
        "SELECT message_id, text FROM messages WHERE agent_id = ?", (first,))}
    assert texts == {"g": "After."}
    assert _warnings(logged) == []


def test_a_free_id_is_kept_as_given(tmp_path, logged):
    second = _agent(tmp_path, "second", "conv-b")
    _store(second, "conv-b", {"id": "fresh", "role": "assistant", "text": "Hello.", "timestamp": "t1"})
    assert [r["message_id"] for r in agents_db.conn().execute(
        "SELECT message_id FROM messages WHERE agent_id = ?", (second,))] == ["fresh"]
    assert _warnings(logged) == []
