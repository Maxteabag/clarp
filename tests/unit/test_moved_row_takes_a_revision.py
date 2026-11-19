"""A row whose seq changed is a changed row: it takes a new revision in the commit that moves it
(R119, the invariant the frontier's head check rests on).

_park_adopted_occupant moved an adopted reply out of the slot another turn takes (RM-N1b) without a
new revision. The conversation's head still moved, through the batch, but a client that asks for
what changed after its revision was not given the row: it learned of the move only by the reload
replace_revision asks for. Checked after EVERY commit of the import, through a second connection;
the shifted-up transcript of tests/readmodel's RM-N1b case, rebuilt for the day gates."""
from __future__ import annotations

import datetime as dt
import re
import sqlite3

from lib import db, message_store, message_writes
from tests.patching import patch_shared

AGENT, SESSION, SOURCE = "agent-park", "bs-park", "/redacted/transcript.jsonl"
BASE_MS = 1_791_706_800_000
_ENDS = re.compile(r"^\s*(COMMIT|END)\b", re.I)


def _iso(seconds):
    stamp = dt.datetime.fromtimestamp(BASE_MS / 1000 + seconds, tz=dt.timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _turn(role, text, at):
    return {"role": role, "text": text, "tools": [], "timestamp": _iso(at)}


def _path():
    return next(row[2] for row in db.conn().execute("PRAGMA database_list") if row[1] == "main")


def _committed():
    with sqlite3.connect(_path()) as other:
        rows = {row[0]: (row[1], row[2]) for row in other.execute(
            "SELECT message_id, seq, revision FROM messages WHERE agent_id=?", (AGENT,))}
        head = other.execute("SELECT revision, replace_revision FROM conversation_heads "
                             "WHERE agent_id=? AND backend_session_id=?", (AGENT, SESSION)).fetchone()
    return rows, tuple(head or (0, 0))


def test_a_reply_parked_out_of_its_slot_takes_a_new_revision_in_that_commit(monkeypatch):
    connection = db.conn()
    connection.execute("INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at, backend)"
                       " VALUES (?,?,?,?,?,?,'claude')", (AGENT, AGENT, "v", "/redacted", AGENT, 1))
    connection.commit()
    patch_shared(monkeypatch, message_writes, "now_ms", lambda: BASE_MS)
    message_store.record_user_message(agent_id=AGENT, backend_session_id=SESSION, client_msg_id="c0",
                                      text="ask", origin="user", sender_agent_id=None, trace_id="tr-0")
    connection.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id, source_file, seq,
                          role, timestamp, text, updated_at, revision, trace_id)
                          VALUES ('final-0', ?, ?, 'final:tr-0', -1000, 'assistant', ?, 'answer', 1, ?, 'tr-0')""",
                       (AGENT, SESSION, _iso(4), message_writes._next_revision(connection)))
    connection.commit()

    def store(turns):
        message_store.store_transcript_turns(agent_id=AGENT, backend_session_id=SESSION,
                                             source_file=SOURCE, turns=turns)
    store([_turn("user", "ask", 0.2), _turn("assistant", "answer", 5)])
    assert _committed()[0]["final-0"][0] == 1, "the fixture did not adopt the reply"

    seen = [_committed()]
    plain = db._TrackedConnection.execute

    def execute(self, sql, parameters=(), /):
        result = plain(self, sql, parameters)
        if _ENDS.match(str(sql)) and not self.in_transaction:
            seen.append(_committed())
        return result
    monkeypatch.setattr(message_store, "IMPORT_COMMIT_EVERY", 1)
    monkeypatch.setattr(message_store, "IMPORT_WRITE_BUDGET_SECONDS", 1e9)
    monkeypatch.setattr(db._TrackedConnection, "execute", execute)
    store([_turn("user", "ask", 0.2), _turn("user", "typed in the terminal", 1), _turn("assistant", "answer", 5)])

    moves = []
    for (before, head_before), (after, head_after) in zip(seen, seen[1:]):
        for message_id, (seq, revision) in after.items():
            if message_id in before and before[message_id][0] != seq:
                moves.append((message_id, before[message_id][0], seq))
                assert revision > before[message_id][1], (
                    f"{message_id} moved from seq {before[message_id][0]} to {seq} with no new revision")
                assert head_after[0] >= revision                      # never above its head
    assert any(message_id == "final-0" for message_id, _old, _new in moves), moves
    assert _committed()[0]["final-0"][0] == 2                         # adopted again with its turn
