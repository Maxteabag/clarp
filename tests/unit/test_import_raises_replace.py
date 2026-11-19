"""Every id the importer removes raises replace_revision in the commit that removes it (458's
follow-up, the reviewer's three survivors and two more): P1 and P2 in _resolve_reply_id, the batch
head (commit_batch), P3 (a reply moved back with its shifted turn, _delete_slot) and one of the
_delete_slot folds of the import loop.

The apps merge /log deltas by id and never remove a row; replace_revision is what tells them to
reload. A client may read between two commits of one import, so it is checked after EVERY commit,
by a SECOND connection that sees only what was committed: each id that was there before the import
and is gone now must come with its conversation's replace_revision above what a client held. The
import commits one row per batch here (IMPORT_COMMIT_EVERY = 1), so a removal reaches a commit by
the batch head before _finish_import's own raise. Real writers on the test's scratch store."""
from __future__ import annotations

import re
import sqlite3

import pytest

from lib import agents as agents_db
from lib import db, message_store, message_writes
from tests.patching import patch_shared

_ENDS = re.compile(r"^\s*(COMMIT|END)\b", re.I)


def _path():
    return next(row[2] for row in db.conn().execute("PRAGMA database_list") if row[1] == "main")


def _committed():
    """What a client would read now: every message id with its conversation, and each
    conversation's replace_revision, through a connection of its own."""
    with sqlite3.connect(_path()) as other:
        ids = {row[0]: (row[1], row[2]) for row in other.execute(
            "SELECT message_id, agent_id, backend_session_id FROM messages")}
        heads = {(row[0], row[1]): row[2] for row in other.execute(
            "SELECT agent_id, backend_session_id, replace_revision FROM conversation_heads")}
    return ids, heads


@pytest.fixture
def commits(monkeypatch):
    """Start watching: call it right before the import. Returns the list the observations fill:
    (ids, heads) after every statement of the store's connections that ends a transaction."""
    seen: list = []
    plain = db._TrackedConnection.execute

    def execute(self, sql, parameters=(), /):
        result = plain(self, sql, parameters)
        if _ENDS.match(str(sql)) and not self.in_transaction:
            seen.append(_committed())
        return result

    def start():
        monkeypatch.setattr(message_store, "IMPORT_COMMIT_EVERY", 1)   # one row per batch
        monkeypatch.setattr(message_store, "IMPORT_WRITE_BUDGET_SECONDS", 1e9)
        monkeypatch.setattr(db._TrackedConnection, "execute", execute)
        return seen
    return start


def _every_disappearance_raised(before, seen):
    """Each id of `before` that a commit no longer shows comes with its conversation's
    replace_revision above the one a client held before the import. Returns the ids gone."""
    ids, heads = before
    gone_at_all = set()
    assert seen, "no commit was observed"
    for number, (now_ids, now_heads) in enumerate(seen, 1):
        gone = {message_id: conversation for message_id, conversation in ids.items()
                if message_id not in now_ids}
        for message_id, conversation in gone.items():
            assert now_heads.get(conversation, 0) > heads.get(conversation, 0), (
                f"commit {number} of the import removed {message_id} without raising replace_revision "
                f"({heads.get(conversation, 0)} -> {now_heads.get(conversation, 0)})")
        gone_at_all |= set(gone)
    return gone_at_all


# P1: a reply already imported sheds an earlier import's twin (_resolve_reply_id, the twins
# of an occupied slot).

def test_a_reply_already_imported_sheds_its_finalized_twin_and_raises_replace(tmp_path, commits):
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    bsid = "backend-1"
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-1")
    final = agents_db.finalize_live_assistant_message(
        agent_id=agent_id, backend_session_id=bsid, trace_id="trace-1", text="Here is the table.")
    now = db.conn().execute("SELECT timestamp FROM messages WHERE message_id=?",
                            (final["id"],)).fetchone()["timestamp"]
    db.conn().execute(                                  # an earlier import stored the reply beside it
        """INSERT INTO messages (message_id, agent_id, backend_session_id, source_file, seq, role,
               timestamp, text, updated_at, revision)
           VALUES ('msg-old-import', ?, ?, '/tmp/rollout.jsonl', 1, 'assistant', ?, 'Here is the table.', 1, 1)""",
        (agent_id, bsid, now))
    db.conn().commit()
    before = _committed()
    seen = commits()
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=bsid, source_file="/tmp/rollout.jsonl",
        turns=[{"role": "user", "text": "summarize", "timestamp": now},
               {"role": "assistant", "text": "Here is the table.", "timestamp": now}])
    assert final["id"] in _every_disappearance_raised(before, seen)          # the twin is gone


# P2: a streamed reply takes the slot an earlier import's row holds (_resolve_reply_id).

def test_a_streamed_reply_takes_its_occupied_slot_and_raises_replace(tmp_path, monkeypatch, commits):
    from lib import message_turns
    from tests.unit.test_chat_rows import BSID, _import, _the_morning, _visible
    agent_id, _ran, live, turns = _the_morning(tmp_path)
    with monkeypatch.context() as old:
        old.setattr(message_turns.TurnIndex, "running_at", lambda self, timestamp: ("", None))
        _import(agent_id, turns)                                          # the reply stored twice
    durable = next(m for m in _visible(agent_id) if m["role"] == "assistant" and m["id"] != live["id"])
    assert agents_db.mark_live_message_final(agent_id=agent_id, backend_session_id=BSID, trace_id="trace-ran")
    before = _committed()
    seen = commits()
    _import(agent_id, turns)
    assert durable["id"] in _every_disappearance_raised(before, seen)
    assert live["id"] in _committed()[0]                                  # the streamed row stays


# A _delete_slot fold of the import loop (a linked client turn's slot, P4d), and the batch head:
# the slot's removal is committed by commit_batch, before _finish_import's own raise.

def test_a_client_rows_slot_is_cleared_and_the_batch_head_raises_replace(tmp_path, monkeypatch, commits):
    from lib.clock import iso_from_ms, now_ms
    from lib.turn_command import _with_delivery_context
    from tests.unit.test_chat_rows import BSID, WORDS, _agent, _import, _the_old_import_stored_the_wrapper
    agent_id = _agent(tmp_path)
    agents_db.record_user_message(agent_id=agent_id, backend_session_id=BSID, text=WORDS,
                                  client_msg_id="barge-1", trace_id="trace-1")
    turns = [{"role": "user", "text": _with_delivery_context(WORDS, unheard_audio=True),
              "timestamp": iso_from_ms(now_ms())},
             {"role": "assistant", "text": "Okay, stopping here.", "timestamp": iso_from_ms(now_ms() + 1)}]
    _the_old_import_stored_the_wrapper(monkeypatch, agent_id, turns)
    before = _committed()
    seen = commits()
    _import(agent_id, turns)
    gone = _every_disappearance_raised(before, seen)
    assert len(gone) == 1 and before[0][next(iter(gone))] == (agent_id, BSID)    # the wrapper row
    first_gone = next(n for n, (ids, _h) in enumerate(seen) if gone - set(ids))
    assert first_gone < len(seen) - 1, "the removal reached a commit only at the import's end"


# P3: an adopted reply moves back with its shifted turn; its old slot is cleared (_delete_slot in
# _resolve_reply_id). As tests/readmodel's RM-N1 case, rebuilt for the day gates.

AGENT, SESSION, SOURCE = "agent-shift", "bs-shift", "/redacted/transcript.jsonl"
BASE_MS = 1_791_706_800_000


def _iso(seconds):
    import datetime as dt
    stamp = dt.datetime.fromtimestamp(BASE_MS / 1000 + seconds, tz=dt.timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _u(text, at):
    return {"role": "user", "text": text, "tools": [], "timestamp": _iso(at)}


def _a(text, at):
    return {"role": "assistant", "text": text, "tools": [], "timestamp": _iso(at)}


def test_an_adopted_reply_moved_back_with_its_shifted_turn_raises_replace(monkeypatch, commits):
    """Two requests; the first one's pair leaves the transcript. The second request's adopted reply
    moves back to seq 1, where the first reply's own row stood: that row goes by P3's _delete_slot
    and by nothing else (the second request's user turn lands on an empty slot, its client row's)."""
    connection = db.conn()
    connection.execute("INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at, backend)"
                       " VALUES (?,?,?,?,?,?,'claude')", (AGENT, AGENT, "v", "/redacted", AGENT, 1))
    connection.commit()
    patch_shared(monkeypatch, message_writes, "now_ms", lambda: BASE_MS)

    def client(client_id, text, at, trace):
        patch_shared(monkeypatch, message_writes, "now_ms", lambda: BASE_MS + int(at * 1000))
        message_store.record_user_message(agent_id=AGENT, backend_session_id=SESSION, client_msg_id=client_id,
                                          text=text, origin="user", sender_agent_id=None, trace_id=trace)
        patch_shared(monkeypatch, message_writes, "now_ms", lambda: BASE_MS)

    client("c1", "first ask", -20, "tr-1")
    client("c0", "second ask", 0, "tr-0")
    connection.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id, source_file, seq,
                          role, timestamp, text, updated_at, revision, trace_id)
                          VALUES ('final-0', ?, ?, 'final:tr-0', -1000, 'assistant', ?, 'second answer', 1, ?, 'tr-0')""",
                       (AGENT, SESSION, _iso(4), message_writes._next_revision(connection)))
    connection.commit()

    def store(turns):
        message_store.store_transcript_turns(agent_id=AGENT, backend_session_id=SESSION,
                                             source_file=SOURCE, turns=turns)

    def seqs():
        return {row["message_id"]: (row["seq"], row["text"]) for row in connection.execute(
            "SELECT message_id, seq, text FROM messages WHERE agent_id=? AND seq >= 0", (AGENT,))}
    store([_u("first ask", -19.8), _a("first answer", -18), _u("second ask", 0.2), _a("second answer", 5)])
    first = seqs()
    assert first["final-0"][0] == 3, f"the fixture did not adopt the second reply: {first}"
    own = message_writes._message_id(AGENT, SESSION, SOURCE, 1)
    assert first.get(own) == (1, "first answer"), f"the first reply is not on its own slot row: {first}"
    before = _committed()
    seen = commits()
    store([_u("second ask", 0.2), _a("second answer", 5)])
    assert own in _every_disappearance_raised(before, seen)
    assert seqs()["final-0"][0] == 1                                   # moved back with its turn
