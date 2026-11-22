"""A submitted form's answer reaches its agent, not the user's chat (issue 428).

The delivery turn's prompt row stays stored (the agent's turn and the import link
to it), but /log, the chat-list preview and GET /turn-queue leave it out: the
answered card is the user's record of what they submitted.
"""
from __future__ import annotations

from lib import agents, db, html_forms, message_store


def _chat():
    aid = agents.create_agent(persona="Mike", voice_id="", cwd="/tmp", session="mike")
    message_store.record_user_message(
        agent_id=aid, backend_session_id="c1", client_msg_id="typed-1", text="make me a form")
    message_store.record_user_message(
        agent_id=aid, backend_session_id="c1",
        client_msg_id=html_forms.delivery_id("sub-0000000000000001"),
        text='The user answered "Trip".\nAnswers:\n{"state":{}}')
    return aid


def test_delivery_row_id_is_the_hidden_prefix():
    assert ("u-" + html_forms.delivery_id("abc")).startswith(html_forms.ANSWER_ROW_PREFIX)
    assert html_forms.is_answer_delivery(html_forms.delivery_id("abc"))
    assert html_forms.is_answer_delivery("u-html-form-abc")
    assert not html_forms.is_answer_delivery("typed-1")
    assert not html_forms.is_answer_delivery(None)


def test_log_leaves_the_answer_out_of_snapshot_and_delta():
    aid = _chat()
    rows = message_store.list_messages(agent_id=aid, backend_session_id="c1", hide_form_answers=True)
    assert [r["text"] for r in rows] == ["make me a form"]
    delta = message_store.list_messages(agent_id=aid, backend_session_id="c1", after_revision=1,
                                        hide_form_answers=True)
    assert all(not r["id"].startswith(html_forms.ANSWER_ROW_PREFIX) for r in delta)
    hidden = message_store.list_messages(agent_id=aid, backend_session_id="c1", hide_coordination=True,
                                         hide_form_answers=True)
    assert [r["text"] for r in hidden] == ["make me a form"]


def test_the_log_read_hides_it(monkeypatch):
    from lib import conversation
    _chat()
    seen = {}
    real = agents.list_messages
    monkeypatch.setattr(agents, "list_messages", lambda **kw: seen.update(kw) or real(**kw))
    monkeypatch.setattr(conversation, "_live_backend_session", lambda agent_id: "c1")
    monkeypatch.setattr(conversation.backends, "find_session_jsonl", lambda backend, sid: None)
    turns = conversation.load_conversation(session="mike")["turns"]
    assert seen["hide_form_answers"] is True
    assert not any(t["id"].startswith(html_forms.ANSWER_ROW_PREFIX) for t in turns)


def test_the_hosts_own_readers_keep_the_users_answer():
    """A Janitor's recent context, a dream's snapshot and a label's evidence read
    list_messages without the flag: the user's answer is there."""
    aid = _chat()
    rows = agents.list_messages(agent_id=aid, backend_session_id="c1", limit=6)
    assert any(r["id"].startswith(html_forms.ANSWER_ROW_PREFIX) for r in rows)


def test_preview_skips_the_answer():
    aid = _chat()
    assert message_store.last_message_preview(agent_id=aid) == "You: make me a form"
    assert message_store.dashboard_messages()[aid]["head"]["preview"] == "You: make me a form"


def test_answer_row_is_still_stored_for_the_agents_turn():
    aid = _chat()
    row = db.conn().execute("SELECT origin FROM messages WHERE agent_id=? AND message_id LIKE ?",
                            (aid, html_forms.ANSWER_ROW_PREFIX + "%")).fetchone()
    assert row is not None and row["origin"] == "user"


def test_queue_depth_an_app_is_told_leaves_the_waiting_answer_out():
    from lib import turn_queue
    aid = agents.create_agent(persona="Rae", voice_id="", cwd="/tmp", session="rae")
    for qid, cid in (("q-1", "typed-2"), ("q-2", html_forms.delivery_id("sub-0000000000000002"))):
        turn_queue.enqueue(queue_id=qid, agent_id=aid, session="rae", text="t", trace_id=qid,
                           client_msg_id=cid, synthesize_audio=False, origin="user", sender_agent_id="")
    state = turn_queue.state(aid)
    assert (state["count"], state["listed"]) == (2, 1)
    assert (turn_queue.states()[aid]["count"], turn_queue.states()[aid]["listed"]) == (2, 1)
