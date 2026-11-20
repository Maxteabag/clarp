"""The import's locked pass does not read the conversation again when its head has not moved (R119).

The read-only pass reads the head FIRST, then the carry rows. Under the writer lock the locked pass
reads the head once more: equal, the rows read are the committed rows and are used as they are;
moved, they are read again, inside, and one line says so. The head is the conversation's
(revision, replace_revision) when the rows are the conversation's, and the agent's heads (COUNT,
MAX(revision), MAX(replace_revision)) when they are the agent's."""
from __future__ import annotations

import pytest

from lib import agents as agents_db
from lib import db, message_store, message_writes

SESSION = "bs-head"


@pytest.fixture
def agent(tmp_path):
    agent_id = agents_db.create_agent(persona="Hana", voice_id="v", cwd=str(tmp_path), session="hana")
    message_store.record_user_message(agent_id=agent_id, backend_session_id=SESSION, client_msg_id="first",
                                      text="the conversation's first words")
    return agent_id


@pytest.fixture
def rereads(monkeypatch):
    """The carry rows read under the writer lock (the locked pass's re-read)."""
    seen = []
    real = message_writes._carry_message_rows

    def counted(database, *args, **kwargs):
        if database.in_transaction:
            seen.append(args)
        return real(database, *args, **kwargs)
    monkeypatch.setattr(message_writes, "_carry_message_rows", counted)
    return seen


def _prepared(agent_id, session_rows=True):
    return message_writes._prepare_import_prelude(
        db.conn(), agent_id, SESSION, [], guard_carry=True, session_rows=session_rows).for_pass()


def _locked(agent_id, prepared):
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        return message_writes._revalidate_import_prelude(con, agent_id, SESSION, prepared)
    finally:
        con.execute("ROLLBACK")


def _ids(rows):
    return sorted(row["message_id"] for row in rows)


def test_an_unchanged_head_reads_no_row_again_under_the_lock(agent, rereads):
    prepared = _prepared(agent)
    again = _locked(agent, prepared)
    assert rereads == [] and again.carry_rows is prepared.carry_rows


def test_a_message_written_after_the_read_is_read_again_inside(agent, rereads):
    prepared = _prepared(agent)
    message_store.record_user_message(agent_id=agent, backend_session_id=SESSION, client_msg_id="later",
                                      text="written after the read")
    again = _locked(agent, prepared)
    assert len(rereads) == 1 and len(_ids(again.carry_rows)) == len(_ids(prepared.carry_rows)) + 1


def test_the_head_is_read_before_the_rows(agent, monkeypatch):
    """A write that lands between the two reads must not be missed: with the head read first it is in
    a later head and the locked pass reads again; read after the rows, the stale rows would stand."""
    real = message_writes._carry_message_rows
    once = []

    def rows_then_a_write(database, *args, **kwargs):
        rows = real(database, *args, **kwargs)
        if not once and not database.in_transaction:
            once.append(1)
            message_store.record_user_message(agent_id=agent, backend_session_id=SESSION,
                                              client_msg_id="between", text="between the reads")
        return rows
    monkeypatch.setattr(message_writes, "_carry_message_rows", rows_then_a_write)
    prepared = _prepared(agent)
    again = _locked(agent, prepared)
    assert any("between" in str(row["message_id"]) or row["text"] == "between the reads"
               for row in again.carry_rows)


def test_a_replace_without_a_new_revision_is_a_moved_head(agent, rereads):
    """The check compares replace_revision as well as revision."""
    prepared = _prepared(agent)
    db.conn().execute("UPDATE conversation_heads SET replace_revision = replace_revision + 1 "
                      "WHERE agent_id=? AND backend_session_id=?", (agent, SESSION))
    db.conn().commit()
    _locked(agent, prepared)
    assert len(rereads) == 1


def test_the_agents_rows_are_checked_by_every_head_of_the_agent(agent, rereads):
    prepared = _prepared(agent, session_rows=False)
    message_store.record_user_message(agent_id=agent, backend_session_id="bs-another", client_msg_id="x",
                                      text="in another conversation of the agent")
    _locked(agent, prepared)
    assert len(rereads) == 1


def test_the_agent_wide_head_is_one_statement_on_the_primary_key(agent):
    plan = " ".join(str(tuple(row)) for row in db.conn().execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*), COALESCE(MAX(revision), 0), COALESCE(MAX(replace_revision), 0) "
        "FROM conversation_heads WHERE agent_id=?", (agent,)))
    assert "SEARCH" in plan and ("autoindex" in plan or "PRIMARY KEY" in plan or "INDEX" in plan), plan


def test_a_moved_head_is_one_line(agent, monkeypatch):
    lines = []
    from lib import log as log_module
    monkeypatch.setattr(log_module, "log", lambda event, detail="", *a, **k: lines.append((event, detail)))
    prepared = _prepared(agent)
    message_store.record_user_message(agent_id=agent, backend_session_id=SESSION, client_msg_id="later2",
                                      text="moved")
    _locked(agent, prepared)
    assert [line for line in lines if line[0] == "importCarryReread"] == [("importCarryReread", "reason=head_moved")]
