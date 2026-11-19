"""A conversation carried to the next one leaves rows behind no more: its head rises, and a client
holding it is told to reload (R119, the invariant the import's head check rests on).

carry_conversation (agy prunes a conversation; Clarp continues it in a new one) moved every row
to the new conversation and raised only the new conversation's head. The old one's head stood
while its rows left it: a client asking the old conversation for what changed after its revision
was told nothing, and kept rows that are no longer there. The per-commit check of both
conversations is test_heads_at_every_commit ("a conversation carried to the next")."""
from __future__ import annotations

from lib import agents as agents_db
from lib import message_previews, message_store, message_writes


def test_the_old_conversation_reads_empty_and_its_delta_says_reload(tmp_path):
    agent = agents_db.create_agent(persona="Ines", voice_id="v", cwd=str(tmp_path), session="ines")
    for n in range(3):
        message_store.record_user_message(agent_id=agent, backend_session_id="bs-old", client_msg_id=f"old-{n}",
                                          text=f"said in the old conversation {n}")
    held = message_previews.latest_revision(agent_id=agent, backend_session_id="bs-old")
    assert not message_previews.requires_replace(agent_id=agent, backend_session_id="bs-old", after_revision=held)

    assert message_writes.carry_conversation(agent_id=agent, from_session_id="bs-old", to_session_id="bs-new") == 3

    assert message_previews.list_messages(agent_id=agent, backend_session_id="bs-old") == []
    assert message_previews.list_messages(agent_id=agent, backend_session_id="bs-old", after_revision=held) == []
    assert message_previews.latest_revision(agent_id=agent, backend_session_id="bs-old") > held
    assert message_previews.requires_replace(agent_id=agent, backend_session_id="bs-old", after_revision=held)
    moved = message_previews.list_messages(agent_id=agent, backend_session_id="bs-new")
    assert len(moved) == 3


def test_a_carry_of_nothing_moves_no_head(tmp_path):
    agent = agents_db.create_agent(persona="Juno", voice_id="v", cwd=str(tmp_path), session="juno")
    assert message_writes.carry_conversation(agent_id=agent, from_session_id="bs-none", to_session_id="bs-new") == 0
    assert message_previews.latest_revision(agent_id=agent, backend_session_id="bs-none") == 0
