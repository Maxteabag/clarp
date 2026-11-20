"""A note to the user is never removed or rewritten by any backend's import (issue 478, F3).

The note is the only thing the user is given from a turn they may keep hidden, and it is the
Host's own row (source_file note:<id>, a negative seq). Every backend's transcript import runs
through the same writer; each is driven here under its own backend name: a tail import, a later
import, a full re-import, and a positional shift of every transcript row before the note. AGY's
own commit (the snapshot restore at the end of its turn) and the Codex app-server's finalize are
driven too. Afterwards the note's row is there with the same id, text, trace and audience."""
from __future__ import annotations

import pathlib

import pytest

from lib import agents, clock, coordination, db, message_store, transcript_incremental, turn_lifecycle

BACKENDS = ("claude", "codex", "agy", "grok", "muse", "opencode", "deepseek")
BASE = 1_795_000_000_000


def _turn(role, text, at):
    return {"role": role, "text": text, "timestamp": clock.iso_from_ms(BASE + at), "tools": []}


def _conversation(extra_before=0):
    turns = [_turn("user", f"older request {i}", 100 + i * 10) for i in range(extra_before)]
    return turns + [_turn("user", "first request", 1_000), _turn("assistant", "first answer", 2_000),
                    _turn("user", "second request", 5_000), _turn("assistant", "second answer", 6_000)]


def _note_row(agent_id):
    return db.conn().execute(
        "SELECT message_id, text, trace_id, kind, origin, source_file FROM messages WHERE kind='note'"
        " AND agent_id=?", (agent_id,)).fetchall()


def _on_log(agent_id, session):
    return [row for row in agents.list_messages(agent_id=agent_id, backend_session_id=session, limit=500)
            if row["kind"] == "note"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_backends_import_keeps_a_note(tmp_path, backend, monkeypatch):
    now = [BASE]
    monkeypatch.setattr(clock, "now_ms", lambda: now[0])
    agent_id = agents.create_agent(persona="Ivy", session="ivy", voice_id="v", cwd=str(tmp_path), backend=backend)
    session = "native"
    agents.bind_backend_session(agent_id, session)
    path = pathlib.Path(tmp_path, "transcript.jsonl")
    path.write_text("")
    current = {"turns": _conversation()[:2]}

    def imported():
        now[0] += 10
        transcript_incremental.import_transcript(backend=backend, path=path, agent_id=agent_id,
                                                 backend_session_id=session,
                                                 parse=lambda _p: list(current["turns"]))
        db.close_local()

    imported()                                                # the tail import
    agents.open_turn(agent_id=agent_id, source="pwa", trace_id="t-note")
    now[0] = BASE + 3_000
    note = message_store.record_note(agent_id=agent_id, backend_session_id=session, note_id="n-1",
                                     text="For you.", trace_id="t-note")
    want = [("note-n-1", "For you.", "t-note", "note", "note", "note:n-1")]
    assert [tuple(row) for row in _note_row(agent_id)] == want
    current["turns"] = _conversation()
    imported()                                                # a later import
    assert [tuple(row) for row in _note_row(agent_id)] == want
    message_store.store_transcript_turns(agent_id=agent_id, backend_session_id=session,
                                         source_file=str(path), turns=_conversation())   # a full re-import
    db.close_local()
    assert [tuple(row) for row in _note_row(agent_id)] == want
    current["turns"] = _conversation(extra_before=3)          # every row before the note shifts
    imported()
    message_store.store_transcript_turns(agent_id=agent_id, backend_session_id=session,
                                         source_file=str(path), turns=_conversation(extra_before=3))
    db.close_local()
    assert [tuple(row) for row in _note_row(agent_id)] == want
    shown = _on_log(agent_id, session)
    assert [(row["id"], row["text"], row["trace_id"], row["audience"]) for row in shown] == [
        ("note-n-1", "For you.", "t-note", "user")]
    assert note["created"] is True


def test_agys_commit_at_the_end_of_its_turn_keeps_a_note_written_during_it(tmp_path):
    """AGY restores its assistant rows from a snapshot taken before the turn; a note written
    while the turn ran was deleted with them (the reviewer's L8)."""
    agent_id = agents.create_agent(persona="Ivy", session="ivy", voice_id="v", cwd=str(tmp_path), backend="agy")
    agents.bind_backend_session(agent_id, "native")
    message_store.store_transcript_turns(agent_id=agent_id, backend_session_id="native", source_file="t.jsonl",
                                         turns=_conversation())
    earlier = message_store.record_note(agent_id=agent_id, backend_session_id="native", note_id="n-before",
                                        text="Said before.", trace_id="t-before")
    agents.open_turn(agent_id=agent_id, source="pwa", trace_id="t-agy")
    snapshot = message_store.capture_assistant_state(agent_id=agent_id, backend_session_id="native")
    message_store.record_note(agent_id=agent_id, backend_session_id="native", note_id="n-during",
                              text="Said during.", trace_id="t-agy")
    agents.commit_agy_assistant_turn(agent_id=agent_id, backend_session_id="native", trace_id="t-agy",
                                     snapshot=snapshot, terminal_status="success", text="Done.")
    rows = {row["message_id"]: (row["text"], row["trace_id"]) for row in _note_row(agent_id)}
    assert rows == {"note-n-before": ("Said before.", "t-before"), "note-n-during": ("Said during.", "t-agy")}
    assert earlier["created"] is True


def test_the_codex_app_servers_finalize_keeps_a_note(tmp_path):
    agent_id = agents.create_agent(persona="Ivy", session="ivy", voice_id="v", cwd=str(tmp_path), backend="codex")
    agents.bind_backend_session(agent_id, "native")
    agents.open_turn(agent_id=agent_id, source="pwa", trace_id="t-cx")
    turn_lifecycle.transition(agent_id, turn_lifecycle.TurnEvent.PROMPT_ADMITTED, {"trace_id": "t-cx"})
    message_store.upsert_live_assistant_message(agent_id=agent_id, backend_session_id="native",
                                                trace_id="t-cx", item_key="it-1", text="Work")
    message_store.record_note(agent_id=agent_id, backend_session_id="native", note_id="n-cx",
                              text="For you.", trace_id="t-cx")
    message_store.finalize_live_assistant_message(agent_id=agent_id, backend_session_id="native",
                                                  trace_id="t-cx", text="Work done.")
    assert [(row["message_id"], row["trace_id"]) for row in _note_row(agent_id)] == [("note-n-cx", "t-cx")]
    assert coordination.audience_of(agent_id, "t-cx") == "user"
