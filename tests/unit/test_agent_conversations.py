"""Agent pairs share one conversation keyed by stable identities, never by names."""
from __future__ import annotations

from lib import agent_conversations, agents as agents_db, message_store


_TRANSCRIPTS: dict[str, list[dict]] = {}


def _pair(tmp_path):
    _TRANSCRIPTS.clear()
    hugo = agents_db.create_agent(persona="Hugo", voice_id="V", cwd=str(tmp_path), session="hugo")
    cpp = agents_db.create_agent(persona="C++ Agent", voice_id="V", cwd=str(tmp_path), session="cagent")
    return hugo, cpp


def _send(sender: str, recipient: str, text: str, *, client_id: str, timestamp: str = "2026-09-07T03:21:06Z"):
    rec = message_store.record_user_message(
        agent_id=recipient, backend_session_id="bs1", client_msg_id=client_id,
        text=text, origin="agent", sender_agent_id=sender)
    # /send stamps wall-clock time; pin display order for deterministic tests.
    agents_db.conn().execute("UPDATE messages SET timestamp = ? WHERE message_id = ?", (timestamp, rec["id"]))
    history = _TRANSCRIPTS.setdefault(recipient, [])
    if not history or history[-1] != {"role": "user", "text": text, "timestamp": timestamp}:
        history.append({"role": "user", "text": text, "timestamp": timestamp})
    return rec


def _private(agent: str, text: str, *, client_id: str, timestamp: str):
    rec = message_store.record_user_message(agent_id=agent, backend_session_id="bs1",
                                            client_msg_id=client_id, text=text)
    agents_db.conn().execute("UPDATE messages SET timestamp = ? WHERE message_id = ?", (timestamp, rec["id"]))
    _TRANSCRIPTS.setdefault(agent, []).append({"role": "user", "text": text, "timestamp": timestamp})
    return rec


def _answer(agent: str, text: str, timestamp: str, trace: str):
    """Stream, then import the whole backend transcript as the runners do."""
    agents_db.open_turn(agent_id=agent, source="pwa", trace_id=trace)
    message_store.upsert_live_assistant_message(
        agent_id=agent, backend_session_id="bs1", trace_id=trace, text=text)
    history = _TRANSCRIPTS.setdefault(agent, [])
    history.append({"role": "assistant", "text": text, "timestamp": timestamp})
    message_store.store_transcript_turns(
        agent_id=agent, backend_session_id="bs1", source_file="f", turns=list(history))


def test_conversation_id_is_direction_independent_and_parseable():
    assert agent_conversations.conversation_id("b", "a") == agent_conversations.conversation_id("a", "b") == "pair:a:b"
    assert agent_conversations.parse_conversation_id("pair:b:a") == ("a", "b")
    assert agent_conversations.parse_conversation_id("pair:a:a") is None
    assert agent_conversations.parse_conversation_id("hugo") is None
    assert agent_conversations.is_pair_session("pair:a:b") and not agent_conversations.is_pair_session("")


def test_both_directions_and_replies_share_one_room_and_private_chats_are_excluded(tmp_path):
    hugo, cpp = _pair(tmp_path)
    _private(hugo, "Peter's private note to Hugo", client_id="private-1", timestamp="2026-09-07T03:00:00Z")
    _send(cpp, hugo, "Status: survey done", client_id="c1", timestamp="2026-09-07T03:21:06Z")
    _answer(hugo, "Good — that matches the agreed scope.", "2026-09-07T03:21:13Z", "t1")
    _send(hugo, cpp, "Send the tested SHA when ready.", client_id="c2", timestamp="2026-09-07T03:25:00Z")
    _answer(cpp, "Will do.", "2026-09-07T03:25:20Z", "t2")

    rooms = agent_conversations.list_conversations()
    assert len(rooms) == 1
    room = rooms[0]
    assert room["conversation_id"] == agent_conversations.conversation_id(hugo, cpp)
    assert sorted(room["agent_ids"]) == sorted([hugo, cpp])
    assert [p["name"] for p in room["participants"]] == ["C++ Agent", "Hugo"]
    assert room["title"] == "C++ Agent & Hugo"
    assert room["message_count"] == 4
    assert room["latest_message"]["sender_name"] == "C++ Agent"
    assert room["latest_message"]["delivery"] == "private"

    log = agent_conversations.load_timeline(room["conversation_id"])
    assert log["conversation_id"] == room["conversation_id"] and not log["missing"]
    texts = [(t["sender_name"], t["text"], t["delivery"], t["reply_to_name"]) for t in log["turns"]]
    assert texts == [
        ("C++ Agent", "Status: survey done", "sent", ""),
        ("Hugo", "Good — that matches the agreed scope.", "private", "C++ Agent"),
        ("Hugo", "Send the tested SHA when ready.", "sent", ""),
        ("C++ Agent", "Will do.", "private", "Hugo"),
    ]
    assert all("private note" not in t["text"] for t in log["turns"])
    sent = log["turns"][0]
    assert sent["role"] == "user" and sent["sender_agent_id"] == cpp and sent["recipient_agent_id"] == hugo
    reply = log["turns"][1]
    assert reply["role"] == "assistant" and reply["sender_agent_id"] == hugo
    assert reply["reply_to_agent_id"] == cpp and reply["reply_to_session"] == "cagent"
    assert log["latest_revision"] == max(t["revision"] for t in log["turns"])
    assert log["replace_required"] is False and log["has_more"] is False


def test_answers_to_the_user_never_enter_the_pair_room(tmp_path):
    hugo, cpp = _pair(tmp_path)
    _send(cpp, hugo, "ping", client_id="c1", timestamp="2026-09-07T03:21:06Z")
    _answer(hugo, "pong for C++", "2026-09-07T03:21:13Z", "t1")
    _private(hugo, "Peter asks something", client_id="u1", timestamp="2026-09-07T03:30:00Z")
    _answer(hugo, "answer for Peter", "2026-09-07T03:30:10Z", "t2")
    log = agent_conversations.load_timeline(agent_conversations.conversation_id(hugo, cpp))
    assert [t["text"] for t in log["turns"]] == ["ping", "pong for C++"]


def test_retries_and_renames_do_not_create_duplicate_rooms_or_rows(tmp_path):
    hugo, cpp = _pair(tmp_path)
    first = _send(cpp, hugo, "same message", client_id="retry-1")
    second = _send(cpp, hugo, "same message", client_id="retry-1")
    assert first["id"] == second["id"]
    before = agent_conversations.list_conversations()
    agents_db.update_agent(hugo, persona="Hugo Renamed")
    after = agent_conversations.list_conversations()
    assert len(before) == len(after) == 1
    assert before[0]["conversation_id"] == after[0]["conversation_id"]
    assert after[0]["title"] == "C++ Agent & Hugo Renamed"
    log = agent_conversations.load_timeline(after[0]["conversation_id"])
    assert len(log["turns"]) == 1


def test_delta_and_older_paging_follow_the_shared_revision_clock(tmp_path):
    hugo, cpp = _pair(tmp_path)
    for index in range(5):
        _send(cpp, hugo, f"message {index}", client_id=f"m{index}", timestamp=f"2026-09-07T03:2{index}:00Z")
    room = agent_conversations.conversation_id(hugo, cpp)
    tail = agent_conversations.load_timeline(room, limit=2)
    assert [t["text"] for t in tail["turns"]] == ["message 3", "message 4"] and tail["has_more"]
    older = agent_conversations.load_timeline(room, before_message_id=tail["turns"][0]["id"], limit=2)
    assert [t["text"] for t in older["turns"]] == ["message 1", "message 2"] and older["has_more"]
    head = tail["latest_revision"]
    _send(hugo, cpp, "new reply", client_id="m5", timestamp="2026-09-07T03:30:00Z")
    delta = agent_conversations.load_timeline(room, after_revision=head)
    assert [t["text"] for t in delta["turns"]] == ["new reply"]
    assert delta["latest_revision"] > head
    assert agent_conversations.load_timeline(room, after_revision=delta["latest_revision"])["turns"] == []


def test_unknown_or_deleted_participants_are_missing(tmp_path):
    hugo, cpp = _pair(tmp_path)
    _send(cpp, hugo, "hello", client_id="c1")
    assert agent_conversations.load_timeline("pair:nope:nada")["missing"] is True
    agents_db.soft_delete(cpp)
    assert agent_conversations.list_conversations() == []
    assert agent_conversations.load_timeline(agent_conversations.conversation_id(hugo, cpp))["missing"] is True


def test_pair_list_uses_partial_index_instead_of_scanning_private_history(tmp_path):
    """Frequent sidebar refreshes must not walk every private transcript row."""
    hugo, cpp = _pair(tmp_path)
    _send(cpp, hugo, 'hello', client_id='pair-index')
    statements = []
    con = agents_db.conn()
    con.set_trace_callback(statements.append)
    try:
        rooms = agent_conversations.list_conversations()
    finally:
        con.set_trace_callback(None)
    assert len(rooms) == 1
    query = next(sql for sql in statements if 'WITH pair_rows' in sql)
    plan = '\n'.join(row[3] for row in con.execute('EXPLAIN QUERY PLAN ' + query))
    assert 'idx_messages_pair_projection' in plan, plan


def test_pair_index_upgrade_preserves_messages_and_is_idempotent(tmp_path):
    from lib import db
    hugo, cpp = _pair(tmp_path)
    _send(cpp, hugo, 'retained pair', client_id='pair-migration')
    _private(hugo, 'retained private', client_id='private-migration', timestamp='2026-09-07T03:30:00Z')
    con = db.conn()
    before = [tuple(row) for row in con.execute('SELECT * FROM messages ORDER BY message_id')]
    expected = agent_conversations.list_conversations()
    con.execute('DROP INDEX IF EXISTS idx_messages_pair_projection')
    con.execute('PRAGMA user_version = 80')
    db._migrate(con)
    db._migrate_to_v81(con)
    assert [tuple(row) for row in con.execute('SELECT * FROM messages ORDER BY message_id')] == before
    assert agent_conversations.list_conversations() == expected
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name='idx_messages_pair_projection'").fetchone()
