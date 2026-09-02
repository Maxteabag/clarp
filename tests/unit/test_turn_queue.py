from __future__ import annotations

from lib import agents as agents_db
from lib import prompt_admissions, turn_queue


def test_durable_turn_queue_round_trip():
    turn_queue.enqueue(
        queue_id="u-1", agent_id="a-1", session="mike", text="later",
        trace_id="t-1", client_msg_id="u-1", synthesize_audio=True,
        origin="user", sender_agent_id="",
        prompt_admission_id="padm-1",
    )
    assert turn_queue.contains("u-1") is True
    assert turn_queue.pending()[0]["text"] == "later"
    assert turn_queue.pending()[0]["prompt_admission_id"] == "padm-1"
    assert turn_queue.pending_counts() == {"a-1": 1}
    assert turn_queue.pending_count("a-1") == 1
    assert turn_queue.state("a-1") == {"count": 1, "revision": 1, "paused": False}
    turn_queue.mark_started("u-1")
    assert turn_queue.status("u-1") == "started"
    assert turn_queue.pending() == []
    assert turn_queue.pending_counts() == {}
    assert turn_queue.pending_count("a-1") == 0
    row = turn_queue.db.conn().execute(
        "SELECT text,prompt_admission_id FROM queued_turns WHERE queue_id = 'u-1'"
    ).fetchone()
    assert row is not None
    assert row["text"] == ""
    assert row["prompt_admission_id"] == "padm-1"
    turn_queue.remove("u-1")
    assert turn_queue.pending() == []


def test_remove_for_agent_drops_only_that_agents_queue():
    for queue_id, agent_id in (("one", "a-1"), ("two", "a-2")):
        turn_queue.enqueue(
            queue_id=queue_id, agent_id=agent_id, session=agent_id, text="later",
            trace_id=queue_id, client_msg_id=queue_id, synthesize_audio=False,
            origin="user", sender_agent_id="")
    before = turn_queue.revision("a-1")
    assert turn_queue.remove_for_agent("a-1") == 1
    assert turn_queue.revision("a-1") == before + 1
    assert [row["queue_id"] for row in turn_queue.pending()] == ["two"]
    assert turn_queue.pending_counts() == {"a-2": 1}
    assert turn_queue.states()["a-1"] == {
        "count": 0, "revision": before + 1, "paused": False,
    }


def test_queue_edit_and_delete_update_unmaterialized_admission(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="voice", cwd=str(tmp_path), session="mike",
    )
    admission = prompt_admissions.create(
        authenticated_at_admission=True,
        origin="user",
        sender_agent_id="",
        channel="chat",
        observed_at=1,
        client_admission_id="u-edit",
        trace_id="trace-edit",
        original_text="original",
    )
    admission_id = prompt_admissions.record(
        admission, agent_id=agent_id, session="mike",
    )
    turn_queue.enqueue(
        queue_id="u-edit", agent_id=agent_id, session="mike", text="original",
        trace_id="trace-edit", client_msg_id="u-edit", synthesize_audio=False,
        origin="user", sender_agent_id="",
        prompt_admission_id=admission_id,
    )

    assert turn_queue.update_text("u-edit", "edited") is True
    row = turn_queue.db.conn().execute(
        "SELECT original_text FROM prompt_admissions WHERE admission_id = ?",
        (admission_id,),
    ).fetchone()
    assert row["original_text"] == "edited"
    assert turn_queue.remove("u-edit") is True
    assert turn_queue.db.conn().execute(
        "SELECT 1 FROM prompt_admissions WHERE admission_id = ?",
        (admission_id,),
    ).fetchone() is None


def test_queue_edit_updates_materialized_admission_and_message(tmp_path):
    from lib import message_store

    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="voice", cwd=str(tmp_path), session="mike",
    )
    admission = prompt_admissions.create(
        authenticated_at_admission=True,
        origin="user",
        sender_agent_id="",
        channel="chat",
        observed_at=1,
        client_admission_id="u-materialized",
        trace_id="trace-materialized",
        original_text="original",
    )
    admission_id = prompt_admissions.record(
        admission, agent_id=agent_id, session="mike",
    )
    message_store.record_user_message(
        agent_id=agent_id,
        backend_session_id="conversation",
        client_msg_id="u-materialized",
        text="original",
        prompt_admission_id=admission_id,
    )
    turn_queue.enqueue(
        queue_id="u-materialized", agent_id=agent_id, session="mike",
        text="original", trace_id="trace-materialized",
        client_msg_id="u-materialized", synthesize_audio=False,
        origin="user", sender_agent_id="",
        prompt_admission_id=admission_id,
    )

    assert turn_queue.update_text("u-materialized", "edited") is True
    row = turn_queue.db.conn().execute(
        """SELECT p.original_text,m.text
             FROM prompt_admissions p
             JOIN messages m ON m.prompt_admission_id = p.admission_id
            WHERE p.admission_id = ?""",
        (admission_id,),
    ).fetchone()
    assert tuple(row) == ("edited", "edited")


def test_queue_can_be_paused_edited_claimed_and_released():
    turn_queue.enqueue(
        queue_id="q-edit", agent_id="a-1", session="mike", text="original",
        trace_id="trace", client_msg_id="q-edit", synthesize_audio=False,
        origin="user", sender_agent_id="")
    assert turn_queue.set_paused("a-1", True) is True
    assert turn_queue.state("a-1")["paused"] is True
    assert turn_queue.update_text("q-edit", "updated") is True
    assert turn_queue.get("q-edit")["text"] == "updated"
    claimed = turn_queue.claim("q-edit")
    assert claimed and claimed["text"] == "updated"
    assert turn_queue.pending("a-1") == []
    turn_queue.db.conn().execute(
        "UPDATE queued_turns SET claimed_at = 0 WHERE queue_id = 'q-edit'")
    assert turn_queue.reset_stale_claims() == 1
    assert turn_queue.pending("a-1")[0]["queue_id"] == "q-edit"
    assert turn_queue.remove("q-edit") is True
    assert turn_queue.pending_count("a-1") == 0
    assert turn_queue.is_paused("a-1") is False
