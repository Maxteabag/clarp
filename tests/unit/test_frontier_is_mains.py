"""The frontier a tail publishes is the record main published, read without the write lock (R119).

Main read the message rows for the frontier in the import's own final transaction, before its
COMMIT. That read is kept here beside the new path, as main's code made it
(``_prepare_import_prelude(..., raw_only=True)`` in the transaction that writes the checkpoint), so
each test compares the two in one run: the record stored, and what the next tail decides."""
from __future__ import annotations

import json
import re
import threading

import pytest

from lib import db, message_carry, message_store, message_writes
from lib import message_store as store
from lib import transcript_incremental as inc
from tests.unit.test_transcript_incremental import (  # noqa: F401
    AGENT, SESSION, _append, _checkpointed, claude_lines, clock, request_text)


def _mains_read(monkeypatch):
    """Main's read, made where main made it: in the import's final transaction. Returns what it
    read, one entry per publication: the five raw groups and the session_rows flag."""
    reads = []
    real = inc.commit_inputs

    def inputs(database, agent_id, backend_session_id, session_rows):
        reads.append((message_writes._prepare_import_prelude(
            database, agent_id, backend_session_id, [], raw_only=True, session_rows=session_rows),
            session_rows))
        return real(database, agent_id, backend_session_id, session_rows)
    monkeypatch.setattr(inc, "commit_inputs", inputs)
    return reads


def _as_main(monkeypatch):
    """Publish main's way: the rows read in the final transaction stand in for the snapshot."""
    reads = _mains_read(monkeypatch)
    monkeypatch.setattr(inc, "commit_snapshot", lambda *a, **k: tuple(reads[-1][0][1:4]))


def _publications(monkeypatch):
    seen = []
    real = inc.frontier_after_commit

    def publish(checkpoint, file_hash, under, rows, witness=None, *, session_rows=True):
        seen.append((checkpoint, witness, session_rows))
        return real(checkpoint, file_hash, under, rows, witness, session_rows=session_rows)
    monkeypatch.setattr(inc, "frontier_after_commit", publish)
    return seen


def _import(transcript):
    return inc.import_transcript(backend="claude", path=transcript, agent_id=AGENT, backend_session_id=SESSION)


def _stored(transcript):
    cp = inc.load_checkpoint(db.conn(), AGENT, SESSION, str(transcript))
    frontier = inc.load_frontier(db.conn(), cp) if cp else None
    return cp, frontier


def _user_turns(lines):
    """Indexes of the records that open a user turn (a boundary a checkpoint can sit at)."""
    return [i for i, line in enumerate(lines)
            if json.loads(line).get("type") == "user"
            and isinstance(json.loads(line)["message"]["content"], str)]


@pytest.mark.parametrize("scope", ["session", "agent"])
@pytest.mark.parametrize("batches", ["one", "yielding"])
def test_the_record_is_mains_byte_for_byte(tmp_path, clock, monkeypatch, scope, batches):  # noqa: F811
    """No second writer: the frontier stored is the one main's read gives, to the byte, for the
    session's rows and the agent's, and for an import that yields between its batches."""
    if scope == "agent":
        conf = dict(inc._DEFAULTS, carry_session_rows=False)
        monkeypatch.setattr(inc, "settings", lambda: dict(conf))
    lines = claude_lines(6)
    cuts = _user_turns(lines)
    transcript, _cp = _checkpointed(tmp_path, "claude", lines, cuts[2], clock)
    # Another conversation of the agent has a row (the agent's rows are read when session_rows is
    # off); a first tail takes it in, so the tail compared below is a tail with it in its inputs.
    message_store.record_user_message(agent_id=AGENT, backend_session_id="bs-other", client_msg_id="o1",
                                      text="said in another conversation of the agent")
    _append(transcript, lines[cuts[2]:cuts[4]])
    _import(transcript)
    _append(transcript, lines[cuts[4]:])
    yields = []
    if batches == "yielding":
        from lib import message_import_writer as miw
        plain = miw._ImportWriter.yield_writer
        monkeypatch.setattr(miw._ImportWriter, "yield_writer", lambda self: (yields.append(1), plain(self))[1])
        monkeypatch.setattr(store, "IMPORT_COMMIT_EVERY", 1)
        monkeypatch.setattr(store, "IMPORT_WRITE_BUDGET_SECONDS", 1e9)
    reads, published = _mains_read(monkeypatch), _publications(monkeypatch)

    result = _import(transcript)

    cp, frontier = _stored(transcript)
    assert result.get("apply_mode") == "tail" and frontier is not None
    assert len(reads) == len(published) == 1
    (raw, flag), (checkpoint, witness, published_flag) = reads[0], published[0]
    assert flag == published_flag == (scope == "session") and checkpoint == cp
    assert frontier["inputs_digest"] == message_carry.dumps(inc.inputs_record(raw, checkpoint, flag, witness))
    assert (len(yields) > 0) == (batches == "yielding")


def _next_tail(root, clock, monkeypatch, *, main, words):  # noqa: F811
    """Checkpoint, a tail with a send of ``words`` landing after its COMMIT and before its frontier
    is published, then the next tail. Returns the next tail's decision codes."""
    root.mkdir()
    with monkeypatch.context() as patch:
        lines = claude_lines(6)
        cuts = _user_turns(lines)
        transcript, _cp = _checkpointed(root, "claude", lines, cuts[2], clock)
        _append(transcript, lines[cuts[2]:cuts[4]])

        def send():
            message_store.record_user_message(agent_id=AGENT, backend_session_id=SESSION,
                                              client_msg_id="meanwhile", text=words)
        if main:
            _as_main(patch)
            real = inc.commit_snapshot
            patch.setattr(inc, "commit_snapshot", lambda *a, **k: (send(), real(*a, **k))[1])
        else:
            _during_the_snapshot(patch, send)
        first = _import(transcript)
        assert first.get("apply_mode") == "tail" and _stored(transcript)[1] is not None
    _append(transcript, lines[cuts[4]:])
    codes = []
    with monkeypatch.context() as patch:
        tail_log = inc._tail_log
        patch.setattr(inc, "_tail_log", lambda code, **kw: (codes.append(code), tail_log(code, **kw))[1])
        _import(transcript)
    return [code for code in codes if code.startswith(("tail_", "full_"))]


def _during_the_snapshot(patch, action):
    """``action`` on another connection once the snapshot has read its head, before its rows."""
    real_snapshot, real_rows = inc.commit_snapshot, message_writes._carry_message_rows
    state = {"in": False, "done": False}

    def snapshot(*args, **kwargs):
        state["in"] = True
        try:
            return real_snapshot(*args, **kwargs)
        finally:
            state["in"] = False

    def rows(database, *args, **kwargs):
        if state["in"] and not state["done"]:
            state["done"] = True
            worker = threading.Thread(target=action)
            worker.start()
            worker.join()
        return real_rows(database, *args, **kwargs)
    patch.setattr(inc, "commit_snapshot", snapshot)
    patch.setattr(message_writes, "_carry_message_rows", rows)


@pytest.mark.parametrize("words,decided", [
    (request_text("claude", 0), "full_carry_inputs_changed"),   # words a turn before the boundary said
    ("words nobody said before", "tail_applied"),               # a new send: the carry rules pass it
])
def test_a_write_after_the_commit_is_judged_by_the_next_tail_as_main_judges_it(
        tmp_path, clock, monkeypatch, words, decided):  # noqa: F811
    on_main = _next_tail(tmp_path / "main", clock, monkeypatch, main=True, words=words)
    here = _next_tail(tmp_path / "head", clock, monkeypatch, main=False, words=words)
    assert here == on_main and decided in here, (on_main, here)


def test_nothing_else_runs_on_the_connection_from_the_commit_to_the_snapshots_end(tmp_path, clock, monkeypatch):  # noqa: F811
    """The importer's connection is this thread's (lib.db.conn): another caller can use it only
    after the import returns. In the import, the snapshot is the very next statement after the
    COMMIT, it reads the head and then the rows and nothing else, and it is ended before the
    frontier's BEGIN IMMEDIATE."""
    lines = claude_lines(4)
    transcript, _cp = _checkpointed(tmp_path, "claude", lines, 6, clock)
    _append(transcript, lines[6:])
    own, statements = db.conn(), []
    plain = db._TrackedConnection.execute

    def execute(self, sql, parameters=(), /):
        if self is own:
            statements.append(" ".join(str(sql).split()))
        return plain(self, sql, parameters)
    monkeypatch.setattr(db._TrackedConnection, "execute", execute)

    _import(transcript)

    frontier = max(i for i, sql in enumerate(statements) if sql == "BEGIN IMMEDIATE")
    begin = max(i for i, sql in enumerate(statements[:frontier]) if sql == "BEGIN")   # the snapshot's
    end = begin + statements[begin:].index("COMMIT")
    assert re.match(r"^(COMMIT|END)\b", statements[begin - 1]), statements[begin - 3:begin + 1]
    inside = statements[begin + 1:end]
    assert len(inside) == 2 and "conversation_heads" in inside[0] and "FROM messages" in inside[1], inside
    assert end < frontier and any("transcript_import_frontiers" in sql for sql in statements[frontier:])
