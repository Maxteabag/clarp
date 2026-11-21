"""GET /log's cursor never passes a row the answer did not see.

The answer reads the conversation's head revision BEFORE its rows. Read after
them, in a statement of its own, a row committed in between (steered messages
land milliseconds apart) had a revision at or below the returned cursor, and the
client's next delta (after_revision) never sent it. A row committed in between
now comes in the next delta (once more if this answer already held it; clients
merge by id).
"""
from __future__ import annotations

import pytest

from lib import agents as agents_db
from lib import conversation

from test_conversation_sync import _agent, _load, _no_transcript_on_disk, _store  # noqa: F401


def _commit_row(agent_id, text):
    agents_db.record_user_message(
        agent_id=agent_id, backend_session_id="conversation-1",
        client_msg_id=f"c-{text}", text=text, trace_id=f"t-{text}")


@pytest.mark.parametrize("when", ["before_rows", "after_rows"])
@pytest.mark.parametrize("delta", [False, True], ids=["snapshot", "delta"])
def test_a_row_committed_between_the_reads_comes_in_the_next_delta(
        tmp_path, monkeypatch, when, delta):
    agent_id = _agent(tmp_path)
    _store(agent_id, [{"role": "user", "text": f"message {i}",
                       "timestamp": f"2026-01-01T00:00:{i:02d}Z"} for i in range(3)])
    start = _load("reliable", include_automated=False)["latest_revision"]
    real = agents_db.list_messages
    fired = []

    def rows_with_a_commit(**kwargs):
        # The rows read: a row commits just before it, or just after it, while
        # the answer is being built (the window the head read used to leave open).
        if not fired and when == "before_rows":
            fired.append(1)
            _commit_row(agent_id, "steered")
        rows = real(**kwargs)
        if not fired and when == "after_rows":
            fired.append(1)
            _commit_row(agent_id, "steered")
        return rows

    monkeypatch.setattr(conversation.agents_db, "list_messages", rows_with_a_commit)
    first = _load("reliable", include_automated=False,
                  **({"after_revision": start} if delta else {}))
    monkeypatch.setattr(conversation.agents_db, "list_messages", real)
    assert fired
    steered = agents_db.conn().execute(
        "SELECT revision FROM messages WHERE agent_id = ? AND text = 'steered'",
        (agent_id,)).fetchone()[0]
    # The cursor never passes the row unless the answer delivered it ...
    if not any(turn["text"] == "steered" for turn in first["turns"]):
        assert first["latest_revision"] < steered
    # ... and the next delta always sends it.
    nxt = _load("reliable", include_automated=False, after_revision=first["latest_revision"])
    assert "steered" in [turn["text"] for turn in nxt["turns"]]


def test_a_quiet_conversation_keeps_its_cursor(tmp_path):
    agent_id = _agent(tmp_path)
    _store(agent_id, [{"role": "user", "text": "only", "timestamp": "2026-01-01T00:00:00Z"}])
    first = _load("reliable", include_automated=False)
    again = _load("reliable", include_automated=False, after_revision=first["latest_revision"])
    assert again["turns"] == []
    assert again["latest_revision"] == first["latest_revision"]


def test_the_agent_to_agent_timeline_reads_its_head_before_its_rows():
    """agent_conversations.load_timeline: the same order (rows, then head, left
    the same window open)."""
    import inspect
    from lib import agent_conversations
    source = inspect.getsource(agent_conversations.load_timeline)
    assert source.index("SELECT MAX(m.revision)") < source.index("rows = conn().execute(sql")
