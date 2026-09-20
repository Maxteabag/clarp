"""Regression coverage for the chat-list preview (`last_message_preview`).

The preview must show the genuinely-latest message. Message timestamps arrive
with different fractional precision, and updated_at is cache-maintenance time,
so ordering must use SQLite's parsed semantic timestamp with updated_at only as
a fallback for legacy rows that have no valid timestamp.
"""
from __future__ import annotations

from lib import agents as agents_db, db, message_store


def _agent(agent_id="a1", session="mike"):
    db.conn().execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at)"
        " VALUES (?,?,?,?,?,?)", (agent_id, "Mike", "v", "/tmp", session, db.now_ms()))


def _msg(mid, seq, text, timestamp, updated_at, role="assistant", agent_id="a1",
         origin="user"):
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, timestamp, text,"
        " tools_json, updated_at, origin) VALUES (?,?,?,?,?,?,?,?,?)",
        (mid, agent_id, seq, role, timestamp, text, "[]", updated_at, origin))


def test_preview_orders_by_parsed_timestamp_not_lexical_timestamp():
    """The bug: newer transcript reply (microsecond stamp) sorts lexically below
    the older message (millisecond stamp), so the string-ordered query returns the
    OLDER one. Must pick by recency instead."""
    _agent()
    # The newer microsecond timestamp sorts lexically below the older
    # millisecond timestamp, and its maintenance timestamp is deliberately old.
    _msg("m1", 1, "older reply", "2026-06-21T17:17:44.453Z", 9000)
    _msg("m2", 2, "newer reply", "2026-06-21T17:17:44.453123Z", 1000)
    assert message_store.last_message_preview(agent_id="a1") == "newer reply"


def test_message_activity_ignores_cache_refresh_time():
    _agent()
    _msg("m1", 1, "old reply", "2026-06-08T15:43:06.040Z",
         1_784_000_000_000)
    assert message_store.last_message_activity(agent_id="a1") == 1_780_933_386_040


def test_real_message_activity_ignores_automation_origins():
    _agent()
    _msg("m1", 1, "user reply", "", 1_000, origin="user")
    _msg("m2", 2, "heartbeat reply", "", 2_000, origin="heartbeat")
    _msg("m3", 3, "agent reply", "", 3_000, origin="agent")
    assert message_store.last_message_activity(agent_id="a1") == 3_000
    assert message_store.last_real_message_activity(agent_id="a1") == 1_000


def test_preview_strips_markup_and_picks_latest():
    _agent()
    _msg("m1", 1, "first", "2026-06-21T10:00:00.000Z", 1000)
    _msg("m2", 2, "<speak>All <vox>um</vox> done. <break time=\"350ms\"/>"
         "<speed ratio=\"0.85\">Ship it</speed>.</speak>",
         "2026-06-21T10:00:01.000Z", 2000)
    assert message_store.last_message_preview(agent_id="a1") == "All done. Ship it."


def test_preview_drops_team_blocks_wholesale():
    _agent()
    _msg("m1", 1, "first", "2026-06-21T10:00:00.000Z", 1000)
    _msg("m2", 2, "Done. <team>private coordination update</team> Next.",
         "2026-06-21T10:00:01.000Z", 2000)
    assert message_store.last_message_preview(agent_id="a1") == "Done. Next."


def test_preview_prefixes_user_messages():
    _agent()
    _msg("m1", 1, "agent reply", "2026-06-21T10:00:00.000Z", 1000)
    _msg("u1", -1, "my question", "2026-06-21T10:00:05.000Z", 5000, role="user")
    assert message_store.last_message_preview(agent_id="a1") == "You: my question"


def test_preview_ignores_newer_routine_automation_rows():
    _agent()
    _msg("m1", 1, "real reply", "2026-06-21T10:00:00.000Z", 1000)
    _msg("h1", 2, "HEARTBEAT_OK", "2026-06-21T10:01:00.000Z", 2000,
         origin="heartbeat")
    _msg("l1", 3, "LEADER_NOOP", "2026-06-21T10:02:00.000Z", 3000,
         origin="leader_tick")
    _msg("d1", 4, "DREAMING_OK", "2026-06-21T10:03:00.000Z", 4000,
         origin="dreaming")
    assert message_store.last_message_preview(agent_id="a1") == "real reply"


def test_preview_empty_when_only_routine_automation_rows():
    _agent()
    _msg("h1", 1, "HEARTBEAT_OK", "2026-06-21T10:01:00.000Z", 2000,
         origin="heartbeat")
    _msg("d1", 2, "DREAMING_OK", "2026-06-21T10:02:00.000Z", 3000,
         origin="dreaming")
    assert message_store.last_message_preview(agent_id="a1") == ""


def test_preview_empty_when_no_messages():
    _agent()
    assert message_store.last_message_preview(agent_id="a1") == ""


def test_live_stream_drops_claude_codes_meta_reply_but_keeps_real_text():
    # Recorded 2026-09-19: every account-recovery resume streamed
    # "No response requested." into the chat as a real bubble.
    _agent()
    assert message_store.upsert_live_assistant_message(
        agent_id="a1", backend_session_id="bs1", text="No response requested.") is None
    assert message_store.upsert_live_assistant_message(
        agent_id="a1", backend_session_id="bs1", text="  No response requested. ") is None
    row = message_store.upsert_live_assistant_message(
        agent_id="a1", backend_session_id="bs1", text="On it.")
    assert row is not None


def test_transcript_import_commits_in_bounded_chunks(tmp_path, monkeypatch):
    # A 19 MB transcript held the write lock for 6-7 s per import; every
    # other writer then hit the 5 s busy timeout (2026-09-20).
    from lib import message_store as ms
    agent_id = agents_db.create_agent(
        persona="Cipher", voice_id="V", cwd=str(tmp_path), session="cipher",
        backend="codex")
    monkeypatch.setattr(ms, "IMPORT_COMMIT_EVERY", 100)
    real_conn = ms.conn
    commits = []

    class _Counting:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().upper() == "COMMIT":
                commits.append(1)
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(ms, "conn", lambda: _Counting(real_conn()))
    turns = []
    for i in range(350):
        turns.append({"role": "user" if i % 2 == 0 else "assistant",
                      "text": f"turn {i}", "timestamp": f"2026-09-20T10:{i // 60:02d}:{i % 60:02d}Z"})
    ms.store_transcript_turns(agent_id=agent_id, backend_session_id="bs",
                              source_file="f", turns=turns)
    stored = db.conn().execute(
        "SELECT count(*) AS n FROM messages WHERE agent_id = ? AND seq >= 0",
        (agent_id,)).fetchone()["n"]
    assert stored == 350
    assert len(commits) == 4, f"350 turns commit at 100, 200, 300 and the end: {len(commits)}"
    # Re-import of the same file is idempotent.
    ms.store_transcript_turns(agent_id=agent_id, backend_session_id="bs",
                              source_file="f", turns=turns)
    assert db.conn().execute(
        "SELECT count(*) AS n FROM messages WHERE agent_id = ? AND seq >= 0",
        (agent_id,)).fetchone()["n"] == 350


def test_failed_next_import_chunk_keeps_committed_revision_visible(monkeypatch):
    import sqlite3
    import pytest
    _agent()
    monkeypatch.setattr(message_store, 'IMPORT_COMMIT_EVERY', 2)
    inner = db.conn()
    class Interrupted:
        begins = 0
        def execute(self, sql, *args):
            if sql == 'BEGIN IMMEDIATE':
                self.begins += 1
                if self.begins == 2:
                    raise sqlite3.OperationalError('database is locked')
            return inner.execute(sql, *args)
        def __getattr__(self, name):
            return getattr(inner, name)
    wrapper = Interrupted()
    monkeypatch.setattr(message_store, 'conn', lambda: wrapper)
    turns = [{'role':'assistant', 'text':f'answer {i}', 'timestamp':f'2026-09-20T10:00:0{i}Z'} for i in range(3)]
    with pytest.raises(sqlite3.OperationalError, match='database is locked'):
        message_store.store_transcript_turns(agent_id='a1', backend_session_id='bs', source_file='f', turns=turns)
    head = inner.execute('SELECT revision FROM conversation_heads WHERE agent_id=? AND backend_session_id=?', ('a1','bs')).fetchone()
    highest = inner.execute('SELECT MAX(revision) FROM messages WHERE agent_id=?', ('a1',)).fetchone()[0]
    assert head is not None and head['revision'] == highest


def test_other_writer_can_commit_before_transcript_import_finishes(monkeypatch):
    import sqlite3
    import threading
    _agent()
    monkeypatch.setattr(message_store, 'IMPORT_COMMIT_EVERY', 2)
    inner = db.conn()
    released = threading.Event()
    wrote = threading.Event()
    failures = []
    def writer():
        try:
            with sqlite3.connect(str(db.DB_PATH), timeout=1) as other:
                assert released.wait(3)
                other.execute("UPDATE agents SET persona='Writer progressed' WHERE agent_id='a1'")
                other.commit()
                wrote.set()
        except BaseException as exc:
            failures.append(exc)
            wrote.set()
    thread = threading.Thread(target=writer)
    thread.start()
    class Concurrent:
        commits = 0
        def execute(self, sql, *args):
            result = inner.execute(sql, *args)
            if sql == 'COMMIT':
                self.commits += 1
                if self.commits == 1:
                    released.set()
                    assert wrote.wait(3), 'writer remained blocked between import chunks'
                    assert not failures
                    assert inner.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2
            return result
        def __getattr__(self, name):
            return getattr(inner, name)
    monkeypatch.setattr(message_store, 'conn', lambda: Concurrent())
    try:
        message_store.store_transcript_turns(agent_id='a1', backend_session_id='bs', source_file='f',
            turns=[{'role':'assistant','text':f'answer {i}'} for i in range(5)])
    finally:
        released.set()
        thread.join(4)
    assert not failures
    assert inner.execute("SELECT persona FROM agents WHERE agent_id='a1'").fetchone()[0] == 'Writer progressed'
