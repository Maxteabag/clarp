"""A seeded Host database for the read-model goldens, and its normalized rows.

The seed holds the rows other paths write before an import sees the
transcript: client sends (one from another agent, two with equal text), a
finalized reply to adopt by its request's trace, a live streamed row to adopt
as its twin, turns with outcomes (one interrupted), and for agy a turn whose
text the stream owns (``agy_turn_authority``).
"""
from __future__ import annotations

import json
import pathlib

from lib import db, message_store, message_writes

from tests.readmodel import corpus
from tests.readmodel.corpus import AGENT, SENDER, SESSION, TRACES


class Clock:
    """message_writes.now_ms, moved by hand."""

    def __init__(self, ms: int = corpus.BASE_S * 1000):
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


def prepare(path: pathlib.Path, backend: str) -> None:
    db.reset_for_tests(path)
    c = db.conn()
    for agent in (AGENT, SENDER):
        c.execute("INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at, backend)"
                  " VALUES (?,?,?,?,?,?,?)", (agent, agent, "v", "/redacted", agent, 1, backend))
    c.commit()


def seed(backend: str, clock: Clock) -> None:
    for r, client_id, origin, sender in ((0, "c0", "user", None),
                                         (1, "c1", "agent", SENDER),
                                         (3, "c3", "agent", SENDER)):
        clock.ms = int(corpus.epoch(r, 0) * 1000) - 500   # just before the CLI records it
        message_store.record_user_message(
            agent_id=AGENT, backend_session_id=SESSION, client_msg_id=client_id,
            text=corpus.request_text(r), origin=origin, sender_agent_id=sender,
            trace_id=TRACES.get(r, ""))
    c = db.conn()
    for r, trace in TRACES.items():
        outcome = "interrupted" if r == 4 else "completed"
        start = int(corpus.epoch(r, 0) * 1000)
        c.execute("""INSERT INTO turns(agent_id, source, trace_id, started_at, ended_at,
                     settled_at, outcome) VALUES (?, 'pwa', ?, ?, ?, ?, ?)""",
                  (AGENT, trace, start, start + 6000, start + 6000, outcome))
    c.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id, source_file,
                 seq, role, timestamp, text, updated_at, revision, trace_id)
                 VALUES ('final-1', ?, ?, 'final:tr-1', -2000, 'assistant', ?, ?, 1, ?, 'tr-1')""",
              (AGENT, SESSION, corpus.iso(1, 5), corpus.answer_text(1),
               message_writes._next_revision(c)))
    c.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id, source_file,
                 seq, role, timestamp, text, updated_at, revision, trace_id)
                 VALUES ('live-3', ?, ?, 'live:tr-3', -1000, 'assistant', ?, ?, 1, ?, 'tr-3')""",
              (AGENT, SESSION, corpus.iso(3, 4), "Done with 3.", message_writes._next_revision(c)))
    if backend == "agy":
        # Request 1's assistant turns are ordinals 2 and 3 (two per request).
        c.execute("""INSERT INTO agy_turn_authority(agent_id, backend_session_id, trace_id,
                     assistant_start_ordinal, assistant_end_ordinal, terminal_status,
                     authoritative_message_id, updated_at)
                     VALUES (?, ?, 'tr-1', 2, 4, 'done', 'final-1', 1)""", (AGENT, SESSION))
    c.commit()


def _json(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return value


def snapshot(transcript: pathlib.Path) -> dict:
    """The rows an import is responsible for, normalized: the transcript path
    reads ``<transcript>``, JSON columns are decoded, ``updated_at`` (wall
    clock) is left out."""
    c = db.conn()
    rows = []
    for r in c.execute("""SELECT message_id, source_file, seq, role, timestamp, text, kind,
                                 tool_name, tools_json, display_cells_json, revision, origin,
                                 sender_agent_id, prompt_admission_id, trace_id, phase, turn_json
                            FROM messages WHERE agent_id = ? ORDER BY seq, message_id""",
                          (AGENT,)):
        row = dict(r)
        if row["source_file"] == str(transcript):
            row["source_file"] = "<transcript>"
        for name in ("tools_json", "display_cells_json", "turn_json"):
            row[name] = _json(row[name])
        rows.append(row)
    heads = [dict(r) for r in c.execute(
        "SELECT * FROM conversation_heads WHERE agent_id = ? ORDER BY 1, 2", (AGENT,))]
    authority = [dict(r) for r in c.execute(
        """SELECT trace_id, assistant_start_ordinal, assistant_end_ordinal, terminal_status,
                  authoritative_message_id FROM agy_turn_authority ORDER BY trace_id""")]
    return {"messages": rows, "heads": heads, "authority": authority,
            "clock": c.execute("SELECT revision FROM message_clock").fetchone()[0]}
