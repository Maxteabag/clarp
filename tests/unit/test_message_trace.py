"""A user message remembers the trace of the turn that carried it."""
from __future__ import annotations

from lib import message_store


def _agent(seed_agents):
    from lib import agents as agents_db
    seed_agents({"rachel": {"name": "Rachel"}})
    return agents_db.get_by_session("rachel")["agent_id"]


def test_user_message_carries_its_trace_and_lists_it(seed_agents):
    agent_id = _agent(seed_agents)
    row = message_store.record_user_message(
        agent_id=agent_id, backend_session_id="b1", client_msg_id="c1",
        text="hello Clarp", trace_id="abc123")
    assert row["created"] is True and row["trace_id"] == "abc123"
    again = message_store.record_user_message(
        agent_id=agent_id, backend_session_id="b1", client_msg_id="c1",
        text="hello Clarp", trace_id="ignored-on-retry")
    assert again["created"] is False and again["trace_id"] == "abc123"
    listed = message_store.list_messages(agent_id=agent_id, backend_session_id="b1")
    mine = [m for m in listed if m["id"] == row["id"]]
    assert mine and mine[0]["trace_id"] == "abc123"


def test_messages_without_a_trace_list_an_empty_string(seed_agents):
    agent_id = _agent(seed_agents)
    row = message_store.record_user_message(
        agent_id=agent_id, backend_session_id="b1", client_msg_id="c2", text="typed")
    assert row["trace_id"] == ""
    listed = message_store.list_messages(agent_id=agent_id, backend_session_id="b1")
    assert [m["trace_id"] for m in listed if m["id"] == row["id"]] == [""]
