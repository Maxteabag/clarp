"""Two writers to one conversation at the same moment each get a sequence
number of their own.

A user message, a notice, an interruption marker and a dream's digest take
the next sequence number below the conversation's lowest. They read it before
their transaction began, so two writers could read the same one, and the
second then failed on the unique index: for a user message a 503 and a retry
(review of #374, finding 2). The number is read inside the transaction that
writes the row, where no other writer can come between.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from lib import agents, db, message_store


def _agent(name="Writer"):
    return agents.create_agent(persona=name, voice_id="", cwd="/tmp", session=name.lower())


def _rows(aid):
    return db.conn().execute(
        "SELECT message_id, seq FROM messages WHERE agent_id=? AND backend_session_id='bs' ORDER BY seq",
        (aid,)).fetchall()


def _another_writer_first(monkeypatch, write):
    """Let ``write`` run to its end on a connection of its own, in the moment
    this thread's next writer is about to begin its transaction."""
    plain = db._TrackedConnection.execute
    done: list = []
    here = threading.get_ident()

    def execute(self, sql, parameters=(), /):
        if str(sql).startswith("BEGIN IMMEDIATE") and threading.get_ident() == here and not done:
            done.append(None)
            failed: list = []

            def run():
                try:
                    write()
                except BaseException as exc:  # noqa: BLE001 - reported to the test
                    failed.append(exc)
                finally:
                    db.close_local()
            other = threading.Thread(target=run)
            other.start()
            other.join(20)
            done[0] = failed[0] if failed else "written"
        return plain(self, sql, parameters)

    monkeypatch.setattr(db._TrackedConnection, "execute", execute)
    return done


WRITERS = {
    "a user message": lambda aid: message_store.record_user_message(
        agent_id=aid, backend_session_id="bs", client_msg_id="mine", text="Mine"),
    "a notice": lambda aid: message_store.record_system_notice(
        agent_id=aid, backend_session_id="bs", notice_id="mine", text="A notice"),
    "an interruption marker": lambda aid: message_store.record_interruption_marker(
        agent_id=aid, backend_session_id="bs", cause_message_id="u-first", text="The turn was cut short"),
    "a dream's digest": lambda aid: message_store.record_dream_digest(
        agent_id=aid, backend_session_id="bs", run_id="mine", text="A digest"),
}


@pytest.mark.parametrize("writer", list(WRITERS))
def test_a_writer_that_another_overtakes_takes_the_number_after_it(monkeypatch, writer):
    aid = _agent()
    message_store.record_user_message(agent_id=aid, backend_session_id="bs", client_msg_id="first", text="First")
    other = _another_writer_first(monkeypatch, lambda: message_store.record_user_message(
        agent_id=aid, backend_session_id="bs", client_msg_id="theirs", text="Theirs"))

    written = WRITERS[writer](aid)

    assert other == ["written"], other
    assert written is not None
    rows = _rows(aid)
    assert len(rows) == 3 and len({row["seq"] for row in rows}) == 3
    assert "u-theirs" in {row["message_id"] for row in rows}
    behind = db.conn().execute(
        """SELECT COUNT(*) FROM conversation_heads h WHERE h.agent_id=? AND h.revision <
           (SELECT MAX(revision) FROM messages m WHERE m.agent_id=h.agent_id
               AND m.backend_session_id=h.backend_session_id)""", (aid,)).fetchone()[0]
    assert behind == 0


def test_two_threads_writing_to_one_conversation_lose_no_message():
    aid = _agent()
    failures: list = []

    def write(who):
        try:
            for number in range(40):
                message_store.record_user_message(
                    agent_id=aid, backend_session_id="bs", client_msg_id=f"{who}-{number}", text=f"{who} {number}")
                if number % 4 == 0:
                    message_store.record_system_notice(
                        agent_id=aid, backend_session_id="bs", notice_id=f"{who}-{number}", text="A notice")
        except BaseException as exc:  # noqa: BLE001 - reported below
            failures.append(exc)
        finally:
            db.close_local()

    threads = [threading.Thread(target=write, args=(who,)) for who in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert failures == []
    rows = _rows(aid)
    assert len(rows) == 100 and len({row["seq"] for row in rows}) == 100


def test_the_number_is_not_read_when_the_row_is_already_there():
    """A retry of the same client id answers from the stored row, as before."""
    aid = _agent()
    first = message_store.record_user_message(agent_id=aid, backend_session_id="bs", client_msg_id="same", text="Once")
    again = message_store.record_user_message(agent_id=aid, backend_session_id="bs", client_msg_id="same", text="Once")
    assert first["created"] is True and again["created"] is False
    assert len(_rows(aid)) == 1
