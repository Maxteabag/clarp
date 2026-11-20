"""A tail import reads its conversation's rows for the frontier OUTSIDE the write lock (R119).

The frontier was recorded in the import's final write transaction: every row of the conversation
read and digested while every other writer of the Host waited (live: 5.08 s on one conversation).
Now the head, the turn rows and the oracle delegations (small; the last two move no head) are read
there, and the message rows right after the commit, in one read snapshot that holds no lock: its
head equal to the commit's, its rows are the commit's. The record is the one main wrote; a row
written since is the next tail's to judge. One short write transaction writes the frontier, its
checkpoint still this one. A write between the commit and the snapshot's first read: no frontier,
one line."""
from __future__ import annotations

import re
import sqlite3
import threading
import time

import pytest

from lib import db, message_store, message_writes, turn_lifecycle
from lib import transcript_incremental as inc
from lib.lifecycle import transaction
from tests.unit.test_transcript_incremental import AGENT, SESSION, _append, _checkpointed, claude_lines, clock  # noqa: F401

WHOLE_CONVERSATION = re.compile(r"FROM messages WHERE agent_id=\? AND backend_session_id=\? ORDER BY message_id")
MESSAGE_TABLE = re.compile(r"\b(FROM|JOIN|INTO|UPDATE)\s+messages\b", re.I)


@pytest.fixture
def writes(monkeypatch):
    """Every write transaction of the store's connections, as the statements run inside it."""
    transactions: list[list[str]] = []
    open_: dict[int, list[str]] = {}
    plain = db._TrackedConnection.execute

    def execute(self, sql, parameters=(), /):
        text = " ".join(str(sql).split())
        if text.upper().startswith("BEGIN IMMEDIATE"):
            open_[id(self)] = []
        elif id(self) in open_:
            if re.match(r"^(COMMIT|ROLLBACK|END)\b", text, re.I):
                transactions.append(open_.pop(id(self)))
            else:
                open_[id(self)].append(text)
        return plain(self, sql, parameters)
    monkeypatch.setattr(db._TrackedConnection, "execute", execute)
    return transactions


def _tail(tmp_path, clock):  # noqa: F811
    lines = claude_lines(4)
    transcript, cp = _checkpointed(tmp_path, "claude", lines, 6, clock)
    _append(transcript, lines[6:])
    return transcript, cp


def _import(transcript):
    return inc.import_transcript(backend="claude", path=transcript, agent_id=AGENT, backend_session_id=SESSION)


def _frontier(transcript):
    cp = inc.load_checkpoint(db.conn(), AGENT, SESSION, str(transcript))
    return cp, (inc.load_frontier(db.conn(), cp) if cp else None)


def test_a_tail_reads_no_whole_conversation_inside_a_write_transaction(tmp_path, clock, writes):  # noqa: F811
    transcript, before = _tail(tmp_path, clock)
    writes.clear()
    result = _import(transcript)
    cp, frontier = _frontier(transcript)
    assert result.get("apply_mode") == "tail" and cp != before and frontier is not None
    inside = [sql for transaction in writes for sql in transaction if WHOLE_CONVERSATION.search(sql)]
    assert inside == [], inside
    last = writes[-1]                                     # the frontier's own transaction: no message row
    assert any("transcript_import_frontiers" in sql for sql in last)
    assert not [sql for sql in last if MESSAGE_TABLE.search(sql)], last


def _before_the_snapshot(monkeypatch, action):
    """Run ``action`` between the import's commit and the frontier's read snapshot."""
    real = inc.commit_snapshot

    def snapshot(*args, **kwargs):
        action()
        return real(*args, **kwargs)
    monkeypatch.setattr(inc, "commit_snapshot", snapshot)


def _during_the_snapshot(monkeypatch, action):
    """Run ``action`` on another connection once the snapshot has read its head, before its rows."""
    real_snapshot, real_rows = inc.commit_snapshot, message_writes._carry_message_rows
    state = {"in_snapshot": False, "done": False}

    def snapshot(*args, **kwargs):
        state["in_snapshot"] = True
        try:
            return real_snapshot(*args, **kwargs)
        finally:
            state["in_snapshot"] = False

    def rows(database, *args, **kwargs):
        if state["in_snapshot"] and not state["done"]:
            state["done"] = True
            worker = threading.Thread(target=action)
            worker.start()
            worker.join()
        return real_rows(database, *args, **kwargs)
    monkeypatch.setattr(inc, "commit_snapshot", snapshot)
    monkeypatch.setattr(message_writes, "_carry_message_rows", rows)


def _record(frontier):
    from lib import message_carry
    return message_carry.loads(frontier["inputs_digest"])


def _now(cp):
    return inc.inputs_record(message_writes._prepare_import_prelude(
        db.conn(), AGENT, SESSION, [], raw_only=True, session_rows=True), cp, True)


def test_a_message_written_after_the_snapshot_began_is_not_in_the_record(tmp_path, clock, monkeypatch):  # noqa: F811
    """ONE SNAPSHOT: the head and the rows are read in one read transaction. The message lands
    between the head read and the rows read: it is not in the rows, as main recorded them in the
    import's transaction, and the next tail judges it. Read in two snapshots (autocommit between),
    the rows would hold a write the equal head never saw, and the record would certify it."""
    transcript, _before = _tail(tmp_path, clock)
    _during_the_snapshot(monkeypatch, lambda: message_store.record_user_message(
        agent_id=AGENT, backend_session_id=SESSION, client_msg_id="meanwhile", text="sent meanwhile"))
    result = _import(transcript)
    cp, frontier = _frontier(transcript)
    assert result.get("apply_mode") == "tail" and frontier is not None
    written = db.conn().execute("SELECT revision FROM messages WHERE text='sent meanwhile'").fetchone()[0]
    assert _record(frontier)["mark"] < written <= _now(cp)["mark"]


def test_a_message_written_before_the_snapshot_means_no_frontier_and_one_line(tmp_path, clock, monkeypatch):  # noqa: F811
    """Between the commit and the snapshot's first read the rows of the commit are gone: nothing
    is certified. The window is the time from COMMIT to the next statement on the same thread."""
    lines = []
    from lib import log as log_module
    real_log = log_module.log
    monkeypatch.setattr(log_module, "log", lambda event, detail="", *a, **k: (
        lines.append((event, detail)), real_log(event, detail, *a, **k)))
    transcript, _before = _tail(tmp_path, clock)
    _before_the_snapshot(monkeypatch, lambda: message_store.record_user_message(
        agent_id=AGENT, backend_session_id=SESSION, client_msg_id="between", text="sent between"))
    result = _import(transcript)
    cp, frontier = _frontier(transcript)
    assert result.get("apply_mode") == "tail" and cp is not None and frontier is None
    assert [line for line in lines if "frontier_not_published" in line[1]] == [
        ("transcriptTailRetention", "code=frontier_not_published_head_moved count=1")]


def test_a_turn_written_after_the_commit_is_not_in_the_record(tmp_path, clock, monkeypatch):  # noqa: F811
    """Turn rows move no head: they are read in the import's own transaction, as on main, so a
    turn opened after the commit is the next tail's to judge, not certified by this one."""
    transcript, _before = _tail(tmp_path, clock)
    _before_the_snapshot(monkeypatch, lambda: turn_lifecycle.open_turn(
        agent_id=AGENT, source="pwa", trace_id="t-meanwhile"))
    _import(transcript)
    cp, frontier = _frontier(transcript)
    assert frontier is not None
    assert _record(frontier)["turns"] != _now(cp)["turns"]


def test_a_failed_snapshot_read_leaves_no_transaction_open(tmp_path, clock, monkeypatch):  # noqa: F811
    """An open read would pin the WAL and hold back checkpointing; the import has committed and
    keeps its result; there is no frontier and one line says why."""
    transcript, _before = _tail(tmp_path, clock)
    real_snapshot, real_rows = inc.commit_snapshot, message_writes._carry_message_rows
    state = {"in": False}

    def snapshot(*args, **kwargs):
        state["in"] = True
        try:
            return real_snapshot(*args, **kwargs)
        finally:
            state["in"] = False

    def rows(database, *args, **kwargs):
        if state["in"]:
            raise sqlite3.OperationalError("disk I/O error")
        return real_rows(database, *args, **kwargs)
    monkeypatch.setattr(inc, "commit_snapshot", snapshot)
    monkeypatch.setattr(message_writes, "_carry_message_rows", rows)
    codes = []
    tail_log = inc._tail_log
    monkeypatch.setattr(inc, "_tail_log", lambda code, **kw: (codes.append(code), tail_log(code, **kw))[1])
    result = _import(transcript)
    cp, frontier = _frontier(transcript)
    assert result.get("apply_mode") == "tail" and cp is not None and frontier is None
    assert not db.conn().in_transaction
    assert codes.count("frontier_not_published_snapshot_failed") == 1


def test_the_snapshot_is_ended_before_the_frontiers_write_with_a_writer_between(tmp_path, clock, monkeypatch):  # noqa: F811
    """A read transaction that writes after another connection committed is refused at once
    (SQLITE_BUSY_SNAPSHOT, no busy wait). The snapshot is ended first, so the frontier's own
    BEGIN IMMEDIATE starts fresh: with a writer committing during the snapshot, it is written."""
    transcript, _before = _tail(tmp_path, clock)
    _during_the_snapshot(monkeypatch, lambda: message_store.record_user_message(
        agent_id=AGENT, backend_session_id=SESSION, client_msg_id="between", text="committed meanwhile"))
    _import(transcript)
    assert _frontier(transcript)[1] is not None and not db.conn().in_transaction


def test_why_the_snapshot_must_end_first(tmp_path):
    """What the order prevents: a read transaction asked to write after another connection's commit."""
    path = tmp_path / "w.sqlite"
    first = sqlite3.connect(path, timeout=5, isolation_level=None)
    first.execute("PRAGMA journal_mode=WAL")
    first.execute("CREATE TABLE t(x)")
    first.execute("BEGIN")
    first.execute("SELECT COUNT(*) FROM t").fetchone()
    other = sqlite3.connect(path, timeout=5, isolation_level=None)
    other.execute("INSERT INTO t VALUES (1)")
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        first.execute("INSERT INTO t VALUES (2)")
    assert time.monotonic() - started < 1.0          # at once, not after the 5 s busy wait
    first.execute("ROLLBACK")


def test_a_checkpoint_that_moved_before_the_frontiers_write_gets_no_frontier(tmp_path, clock, monkeypatch):  # noqa: F811
    """The frontier's own transaction writes the record only if the checkpoint row is still the
    one the import wrote. Another import's checkpoint and frontier, written meanwhile, keep their
    frontier: this import's late record does not replace it (one frontier row per transcript).
    The reviewer of 464: no test moved the checkpoint there."""
    transcript, _before = _tail(tmp_path, clock)
    real = inc.frontier_after_commit
    moved = []

    def frontier(checkpoint, *args, **kwargs):
        con = db.conn()
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE transcript_import_checkpoints SET updated_at = updated_at + 1 "
                    "WHERE agent_id=? AND backend_session_id=? AND source_file=?",
                    (checkpoint.agent_id, checkpoint.backend_session_id, checkpoint.source_file))
        con.execute("COMMIT")
        moved.append(inc.load_checkpoint(con, checkpoint.agent_id, checkpoint.backend_session_id,
                                         checkpoint.source_file))
        with transaction(con):                                       # the other import's own frontier
            record = inc.inputs_record(message_writes._prepare_import_prelude(
                con, AGENT, SESSION, [], raw_only=True, session_rows=True), moved[0], True)
            inc.write_frontier(con, moved[0], "the other import's hash", record)
        return real(checkpoint, *args, **kwargs)
    monkeypatch.setattr(inc, "frontier_after_commit", frontier)
    codes = []
    tail_log = inc._tail_log
    monkeypatch.setattr(inc, "_tail_log", lambda code, **kw: (codes.append(code), tail_log(code, **kw))[1])
    result = _import(transcript)
    assert result.get("apply_mode") == "tail" and len(moved) == 1
    other = inc.load_frontier(db.conn(), moved[0])                  # not overwritten by this one's
    assert other is not None and other["file_hash"] == "the other import's hash"
    assert codes.count("frontier_not_published_checkpoint_moved") == 1
