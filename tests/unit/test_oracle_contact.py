"""The Host-owned Oracle contact is always resolvable or empty.

Recorded 2026-09-19: the phone kept sending `oracle_session=diego-0cd1`, a
session deleted long before. Every WebRTC create answered 400 "Invalid Oracle
call" with no log line, and Oracle told the user she had no contact.
"""
from __future__ import annotations

import pytest

from lib import db, oracle_contact


def _agent(agent_id, persona, session, *, archived=False, janitor=False, deleted=False):
    db.conn().execute(
        "INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at,"
        " archived_at, is_janitor, deleted_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (agent_id, persona, "v", "/tmp", session, db.now_ms(),
         db.now_ms() if archived else None, 1 if janitor else 0,
         db.now_ms() if deleted else None))


def test_set_accepts_session_or_persona_and_stores_the_session():
    _agent("a1", "Mike", "mike-cb43")
    assert oracle_contact.set("Mike") == {"session": "mike-cb43", "persona": "Mike", "stale": ""}
    assert oracle_contact.get()["session"] == "mike-cb43"
    assert oracle_contact.set("MIKE-CB43")["session"] == "mike-cb43"


def test_set_refuses_an_unknown_ambiguous_archived_or_janitor_agent():
    _agent("a1", "Mike", "mike-1")
    _agent("a2", "Mike", "mike-2")
    _agent("a3", "Lena", "lena-old", archived=True)
    _agent("a4", "Feedback Janitor", "feedbackjanitor-1", janitor=True)
    for name in ("diego-0cd1", "Mike", "lena-old", "feedbackjanitor-1"):
        with pytest.raises(ValueError, match="not a live agent"):
            oracle_contact.set(name)
    assert oracle_contact.get()["session"] == ""


def test_empty_clears_and_a_deleted_contact_is_cleared_and_reported_stale():
    _agent("a1", "Diego", "diego-0cd1")
    oracle_contact.set("diego-0cd1")
    db.conn().execute("UPDATE agents SET deleted_at=? WHERE agent_id='a1'", (db.now_ms(),))
    assert oracle_contact.get() == {"session": "", "persona": "", "stale": "diego-0cd1"}
    # Cleared, so the next read no longer reports it.
    assert oracle_contact.get() == {"session": "", "persona": "", "stale": ""}
    _agent("a2", "Mike", "mike-cb43")
    oracle_contact.set("mike-cb43")
    assert oracle_contact.set("")["session"] == ""
    assert oracle_contact.get()["session"] == ""


def test_effective_prefers_the_host_then_a_live_client_value_then_nothing():
    _agent("a1", "Mike", "mike-cb43")
    _agent("a2", "Lena", "lena-74d2")
    # No Host contact: a client value that resolves is honoured, by persona too.
    assert oracle_contact.effective("lena-74d2") == "lena-74d2"
    assert oracle_contact.effective("Lena") == "lena-74d2"
    # A stale client value degrades to "no contact" instead of raising.
    assert oracle_contact.effective("diego-0cd1") == ""
    assert oracle_contact.effective("") == ""
    assert oracle_contact.effective(None) == ""
    # Once the Host has a contact, it wins over whatever the client sends.
    oracle_contact.set("mike-cb43")
    assert oracle_contact.effective("lena-74d2") == "mike-cb43"
    assert oracle_contact.effective("diego-0cd1") == "mike-cb43"
