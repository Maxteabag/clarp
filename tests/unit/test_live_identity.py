"""The row that streamed is the row that persists (docs/live-items.md §2):
importing the durable transcript adopts the live row in place instead of
deleting it and inserting a twin, so clients never reload the tail; and each
provider message streams into its own row instead of one row concatenating
the whole turn."""
from __future__ import annotations

from lib import agents as agents_db
from lib import message_store


def _agent(session="nia"):
    agent_id = agents_db.create_agent(
        persona=session.title(), voice_id="v", cwd="/tmp", session=session, backend="claude")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="t-1")
    return agent_id


def _replace_revision(agent_id):
    row = agents_db.conn().execute(
        "SELECT replace_revision FROM conversation_heads WHERE agent_id = ?",
        (agent_id,)).fetchone()
    return int(row["replace_revision"] or 0) if row else 0


def _rows(agent_id):
    return [dict(r) for r in agents_db.conn().execute(
        """SELECT message_id, text, kind, source_file FROM messages
            WHERE agent_id = ? AND role = 'assistant' ORDER BY timestamp, seq""",
        (agent_id,)).fetchall()]


def test_import_adopts_the_live_row_instead_of_swapping_it():
    agent_id = _agent()
    live = message_store.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id="b-1", trace_id="t-1",
        text="The answer is 42.")
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id="b-1", source_file="/x/t.jsonl",
        turns=[{"role": "user", "text": "question", "timestamp": "2026-10-03T00:00:00Z"},
               {"role": "assistant", "text": "The answer is 42.", "timestamp": "2026-10-03T00:00:01Z"}])
    rows = _rows(agent_id)
    assert [r["message_id"] for r in rows] == [live["id"]]
    assert rows[0]["kind"] is None
    assert _replace_revision(agent_id) == 0


def test_each_provider_message_streams_into_its_own_row():
    agent_id = _agent()
    first = message_store.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id="b-1", trace_id="t-1",
        text="Looking at it.", item_key="msg_1")
    second = message_store.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id="b-1", trace_id="t-1",
        text="Found it.", item_key="msg_2")
    assert first["id"] != second["id"]
    assert [r["text"] for r in _rows(agent_id)] == ["Looking at it.", "Found it."]
    assert _replace_revision(agent_id) == 0
    # The first message lands durably while the second still streams.
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id="b-1", source_file="/x/t.jsonl",
        turns=[{"role": "user", "text": "q", "timestamp": "2026-10-03T00:00:00Z"},
               {"role": "assistant", "text": "Looking at it.", "timestamp": "2026-10-03T00:00:01Z"}])
    message_store.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id="b-1", trace_id="t-1",
        text="Found it. Fixing.", item_key="msg_2")
    rows = _rows(agent_id)
    assert [(r["message_id"], r["text"]) for r in rows] == [
        (first["id"], "Looking at it."), (second["id"], "Found it. Fixing.")]
    assert _replace_revision(agent_id) == 0


def test_a_finished_message_is_marked_final_in_place():
    agent_id = _agent()
    live = message_store.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id="b-1", trace_id="t-1",
        text="Done.", item_key="msg_1")
    assert message_store.mark_live_message_final(
        agent_id=agent_id, backend_session_id="b-1", trace_id="t-1", item_key="msg_1")
    [row] = _rows(agent_id)
    assert (row["message_id"], row["kind"]) == (live["id"], None)
    # A late duplicate delta for the settled message does not reopen it.
    message_store.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id="b-1", trace_id="t-1",
        text="Done.", item_key="msg_1")
    assert _rows(agent_id)[0]["kind"] is None


def test_oracle_finalization_settles_the_streamed_row_not_a_new_one():
    agent_id = _agent()
    live = message_store.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id="b-1", trace_id="t-1",
        text="Partial", item_key="msg_1")
    final = message_store.finalize_live_assistant_message(
        agent_id=agent_id, backend_session_id="b-1", trace_id="t-1",
        text="Partial and complete.")
    assert final["id"] == live["id"]
    assert [r["text"] for r in _rows(agent_id)] == ["Partial and complete."]


def test_codex_agent_messages_stream_into_their_own_rows():
    import threading

    from lib import codex_app_server
    from lib.backend.codex import TurnState

    class _Handle:
        def __init__(self):
            self._done = threading.Event()

    agent_id = agents_db.create_agent(
        persona="Caleb", voice_id="v", cwd="/tmp", session="caleb", backend="codex")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-1")
    client = object.__new__(codex_app_server._Client)
    client.agent_id = agent_id
    client.active = codex_app_server._ActiveTurn(
        turn_id="turn-1", thread_id="thread-1", agent_id=agent_id, session="caleb",
        trace_id="trace-1", state=TurnState(live_backend_session_id="thread-1"),
        handle=_Handle(), on_result=None, on_error=None, stream=None,
        enqueue=lambda **_kwargs: 0)
    client._notification("item/agentMessage/delta", {"itemId": "msg_a", "delta": "Checking."})
    client._notification("item/completed", {"item": {
        "type": "agentMessage", "id": "msg_a", "text": "Checking."}})
    client._notification("item/agentMessage/delta", {"itemId": "msg_b", "delta": "Fixed it."})
    client._notification("turn/completed", {"turn": {"status": "completed"}})
    rows = _rows(agent_id)
    assert [r["text"] for r in rows] == ["Checking.", "Fixed it."]
    assert rows[0]["kind"] is None
    assert _replace_revision(agent_id) == 0
