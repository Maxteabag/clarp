"""Every message id that disappears raises conversation_heads.replace_revision in the same
transaction (448's follow-up A). Android and iOS merge /log deltas by id and never remove a row: an
id gone without the raise leaves the old row on screen and in the app's cache, so a reply shows
twice. The importer's paths are pinned by their own tests; these pin the three live-row writers in
message_live, which held it by reading only. Each fails when its raise is removed."""
from __future__ import annotations

from lib import agents as agents_db
from lib import db

SESSION = "b-1"


def _agent(tmp_path, name="lena"):
    agent_id = agents_db.create_agent(persona=name.title(), voice_id="v", cwd=str(tmp_path), session=name,
                                      backend="claude")
    agents_db.start_runtime(agent_id, name)
    agents_db.bind_backend_session(agent_id, SESSION)
    return agent_id


def _head(agent_id):
    row = db.conn().execute(
        "SELECT revision, replace_revision FROM conversation_heads WHERE agent_id=? AND backend_session_id=?",
        (agent_id, SESSION)).fetchone()
    return (row["revision"], row["replace_revision"]) if row else (0, 0)


def _ids(agent_id):
    return {row["message_id"] for row in db.conn().execute(
        "SELECT message_id FROM messages WHERE agent_id=? AND backend_session_id=?", (agent_id, SESSION))}


def _live(agent_id, trace, text):
    return agents_db.upsert_live_assistant_message(agent_id=agent_id, backend_session_id=SESSION,
                                                    trace_id=trace, text=text)


def test_a_new_turns_live_row_that_removes_an_earlier_turns_unfinished_one_raises_replace(tmp_path):
    agent_id = _agent(tmp_path)
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-a")
    _live(agent_id, "turn-a", "half a reply, the turn then failed")
    gone = _ids(agent_id)
    held, _ = _head(agent_id)                                   # what a client has seen
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-b")
    _live(agent_id, "turn-b", "the next turn's first words")
    assert gone and not gone & _ids(agent_id)                   # the earlier turn's id is gone
    revision, replace = _head(agent_id)
    assert replace > held and replace == revision                # raised in the write that removed it


def test_a_retracted_live_row_raises_replace(tmp_path):
    agent_id = _agent(tmp_path)
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-a")
    _live(agent_id, "turn-a", "words the provider then withdrew")
    held, _ = _head(agent_id)
    assert agents_db.delete_live_assistant_message(agent_id=agent_id, backend_session_id=SESSION,
                                                   trace_id="turn-a")
    assert _ids(agent_id) == set()
    revision, replace = _head(agent_id)
    assert replace > held and replace == revision


def test_restoring_the_assistant_rows_removes_the_turns_rows_and_raises_replace(tmp_path):
    agent_id = _agent(tmp_path)
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="turn-a")
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=SESSION, source_file="fixture",
        turns=[{"id": "stable", "role": "assistant", "text": "before", "timestamp": "t1"}])
    snapshot = agents_db.capture_assistant_state(agent_id=agent_id, backend_session_id=SESSION)
    _live(agent_id, "turn-a", "this turn's words, to be undone")
    added = _ids(agent_id) - {"stable"}
    held, _ = _head(agent_id)
    assert agents_db.restore_assistant_state(agent_id=agent_id, backend_session_id=SESSION,
                                             trace_id="turn-a", snapshot=snapshot)
    assert added and _ids(agent_id) == {"stable"}
    revision, replace = _head(agent_id)
    assert replace > held and replace == revision
