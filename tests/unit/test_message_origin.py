"""Phase 2: message origin + sender identity.

A message is no longer always "from the user". When one agent prompts another the
row carries origin='agent' and the sender's agent_id, so the client can render
it distinctly instead of as a plain user bubble.
"""
from __future__ import annotations

from lib import agents as agents_db
from lib import dreaming, heartbeat, message_store, team_leader


def test_agent_prompt_records_origin_and_sender(tmp_path):
    target = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar")
    sender = agents_db.create_agent(
        persona="Lena", voice_id="V", cwd=str(tmp_path), session="lena")

    rec = message_store.record_user_message(
        agent_id=target, backend_session_id="bs1",
        client_msg_id="c1", text="please rebase onto main",
        origin="agent", sender_agent_id=sender)
    assert rec["origin"] == "agent"
    assert rec["sender_agent_id"] == sender

    msgs = message_store.list_messages(agent_id=target, backend_session_id="bs1")
    row = next(m for m in msgs if m["text"] == "please rebase onto main")
    assert row["origin"] == "agent"
    assert row["sender_agent_id"] == sender
    assert row["sender_name"] == "Lena"
    assert row["sender_session"] == "lena"

    head = agents_db.latest_message_revision(
        agent_id=target, backend_session_id="bs1")
    delta = message_store.list_messages(
        agent_id=target, backend_session_id="bs1", after_revision=head - 1)
    row = next(m for m in delta if m["text"] == "please rebase onto main")
    assert row["sender_name"] == "Lena"
    assert row["sender_session"] == "lena"


def test_agent_prompt_provenance_carries_to_assistant_rows(tmp_path):
    target = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar")
    sender = agents_db.create_agent(
        persona="Lena", voice_id="V", cwd=str(tmp_path), session="lena")
    bs = "bs1"
    message_store.record_user_message(
        agent_id=target, backend_session_id=bs,
        client_msg_id="c1", text="please check this",
        origin="agent", sender_agent_id=sender)
    agents_db.open_turn(agent_id=target, source="pwa", trace_id="t1")

    live = message_store.upsert_live_assistant_message(
        agent_id=target, backend_session_id=bs, trace_id="t1",
        text="Checking now.")
    assert live["origin"] == "agent"
    assert live["sender_agent_id"] == sender

    message_store.store_transcript_turns(
        agent_id=target, backend_session_id=bs, source_file="f",
        turns=[
            {"role": "user", "text": "please check this"},
            {"role": "assistant", "text": "Done."},
        ])

    assistant = next(m for m in message_store.list_messages(
        agent_id=target, backend_session_id=bs) if m["text"] == "Done.")
    assert assistant["origin"] == "agent"
    assert assistant["sender_agent_id"] == sender


def test_strip_injected_team_context():
    raw = ("do the thing\n\n--- Clarp team context ---\n"
           "blah blah\n--- End Clarp team context ---")
    assert message_store.strip_injected_team_context(raw) == "do the thing"
    prefixed = ("--- Clarp team context ---\n"
                "blah blah\n--- End Clarp team context ---\n\n"
                "do the thing")
    assert message_store.strip_injected_team_context(prefixed) == "do the thing"
    assert message_store.strip_injected_team_context("plain") == "plain"


def test_transcript_import_does_not_leak_team_context(tmp_path):
    """The backend transcript copies the augmented prompt (with the team-context
    block). On import it must strip to the clean text and dedup against the
    durable client row — no duplicate, no leak into the chat."""
    target = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar")
    bs = "bs1"
    message_store.record_user_message(
        agent_id=target, backend_session_id=bs,
        client_msg_id="c1", text="do the thing")
    augmented = ("--- Clarp team context ---\n"
                 "teammate stuff\n--- End Clarp team context ---\n\n"
                 "do the thing")
    message_store.store_transcript_turns(
        agent_id=target, backend_session_id=bs, source_file="f",
        turns=[{"role": "user", "text": augmented},
               {"role": "assistant", "text": "done"}])

    user_msgs = [m for m in message_store.list_messages(
        agent_id=target, backend_session_id=bs) if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["text"] == "do the thing"
    assert "Clarp team context" not in user_msgs[0]["text"]


def test_plain_user_message_defaults_to_user(tmp_path):
    target = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar")
    rec = message_store.record_user_message(
        agent_id=target, backend_session_id="bs1",
        client_msg_id="c2", text="hello")
    assert rec["origin"] == "user"
    assert rec["sender_agent_id"] == ""

    row = message_store.list_messages(
        agent_id=target, backend_session_id="bs1")[0]
    assert row["origin"] == "user"
    assert row["sender_agent_id"] == ""


def test_human_read_tags_system_automation_prompts(tmp_path):
    target = agents_db.create_agent(
        persona="Arnold", voice_id="V", cwd=str(tmp_path), session="arnold")
    sender = agents_db.create_agent(
        persona="Domi", voice_id="V", cwd=str(tmp_path), session="domi")
    bs = "bs1"

    message_store.record_user_message(
        agent_id=target, backend_session_id=bs,
        client_msg_id="u1", text="the user's real request", origin="user")
    message_store.record_user_message(
        agent_id=target, backend_session_id=bs,
        client_msg_id="u2", text="Domi needs a review",
        origin="agent", sender_agent_id=sender)
    message_store.record_user_message(
        agent_id=target, backend_session_id=bs,
        client_msg_id="u3", text="Reminder User scheduled",
        origin="schedule")
    message_store.record_user_message(
        agent_id=target, backend_session_id=bs,
        client_msg_id="u4", text=heartbeat.HEARTBEAT_PROMPT,
        origin="heartbeat")
    message_store.record_user_message(
        agent_id=target, backend_session_id=bs,
        client_msg_id="u5", text=team_leader.TICK_PROMPT,
        origin="leader_tick")
    message_store.record_user_message(
        agent_id=target, backend_session_id=bs,
        client_msg_id="u6", text=team_leader.TICK_PROMPT,
        origin="schedule")
    message_store.record_user_message(
        agent_id=target, backend_session_id=bs,
        client_msg_id="u7", text=dreaming.DREAMING_PROMPT,
        origin="dreaming")
    message_store.record_user_message(
        agent_id=target, backend_session_id=bs,
        client_msg_id="u8", text="A watched WhatsApp reply arrived",
        origin="watcher")

    visible = message_store.list_messages(agent_id=target, backend_session_id=bs)
    assert sorted(m["text"] for m in visible) == sorted([
        "the user's real request",
        "Domi needs a review",
        "Reminder User scheduled",
        "Automated heartbeat check",
        "Automated leader check",
        "Automated leader check",
        "Automated dreaming run",
        "Automated watcher event",
    ])
    automated = [m for m in visible if m["automated"]]
    assert sorted(m["automation_kind"] for m in automated) == [
        "dreaming", "heartbeat", "leader_tick", "leader_tick", "watcher"]
    assert sorted(m["origin"] for m in visible if not m["automated"]) == [
        "agent", "schedule", "user"]
