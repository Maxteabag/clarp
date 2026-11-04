"""AUT-07: an automation's acknowledgement token (HEARTBEAT_OK, DREAMING_OK,
LEADER_NOOP) is acted on only in that automation's own turns. In a user's
turn a reply that mentions it is kept verbatim, and nothing is recorded;
a reply that is nothing but the token stays hidden whatever the origin."""
from __future__ import annotations

import pytest

from lib import agents as agents_db
from lib import dreaming, heartbeat, message_store, team_leader

REPLIES = [
    "Sure, the heartbeat prompt asks the agent to reply with HEARTBEAT_OK",
    "The dream round ends with DREAMING_OK",
    "A quiet team leader answers LEADER_NOOP",
]


def _agent(tmp_path):
    return agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")


@pytest.mark.parametrize("reply", REPLIES)
def test_a_user_turn_reply_that_mentions_a_token_is_kept_when_imported(tmp_path, reply):
    agent_id = _agent(tmp_path)
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id="s1", source_file="/tmp/transcript.jsonl",
        turns=[{"role": "user", "text": "how do the automations acknowledge?", "timestamp": "1"},
               {"role": "assistant", "text": reply, "timestamp": "2"}])
    texts = [m["text"] for m in message_store.list_messages(agent_id=agent_id, backend_session_id="s1")]
    assert texts[-1] == reply


@pytest.mark.parametrize("reply", REPLIES)
def test_a_user_turn_reply_that_mentions_a_token_is_kept_live(tmp_path, reply):
    agent_id = _agent(tmp_path)
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-1")
    live = message_store.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id="s1", trace_id="trace-1", text=reply)
    assert live is not None and live["text"] == reply


def test_a_user_turn_never_ends_a_dream_or_records_a_leader_noop(tmp_path, monkeypatch):
    agent_id = _agent(tmp_path)
    recorded = []
    monkeypatch.setattr(dreaming, "mark_active_noop", lambda aid: recorded.append(("dream", aid)))
    monkeypatch.setattr(team_leader, "record_leader_noop", lambda aid: recorded.append(("leader", aid)))
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id="s1", source_file="/tmp/transcript.jsonl",
        turns=[{"role": "user", "text": "anything to add?", "timestamp": "1"},
               {"role": "assistant", "text": "Nothing. DREAMING_OK", "timestamp": "2"},
               {"role": "user", "text": "and the team?", "timestamp": "3"},
               {"role": "assistant", "text": "Nothing. LEADER_NOOP", "timestamp": "4"}])
    assert recorded == []


def test_the_heartbeats_own_turn_still_strips_its_token(tmp_path):
    agent_id = _agent(tmp_path)
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id="s1", source_file="/tmp/transcript.jsonl",
        turns=[{"role": "user", "text": heartbeat.HEARTBEAT_PROMPT, "timestamp": "1"},
               {"role": "assistant", "text": "All quiet. HEARTBEAT_OK", "timestamp": "2"}])
    texts = [m["text"] for m in message_store.list_messages(agent_id=agent_id, backend_session_id="s1")]
    assert texts[-1] == "Heartbeat check: no action needed."
