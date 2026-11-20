"""An agent's note to the user: ``POST /agent-notes`` (``clarp-admin tell-user``).

The one thing an agent can do to put words in front of the user from any
turn, also from a turn that answers another agent, whose rows the user may
keep out of the chat (lib.coordination). The note is a row of the calling
turn in the agent's own chat, always shown, and it notifies on its own
(reason ``agent-note``). It is never spoken in this version.

It must come from the agent's own running turn: a turn token that names
another agent is refused, like ``clarp-admin prompt --from``, and so is an
agent with no turn running. Without a token (the shared Codex app-server)
the agent's running turn is taken, as the named sender is for a prompt.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from . import agents as agents_db, db, identity, message_store, user_notifications

COMMAND = "clarp-admin tell-user"
MAX_TEXT = 8000
_NOTE_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")


class NoteRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Posted:
    note: dict[str, Any]
    notification: dict[str, Any]
    agent: dict[str, Any]
    backend_session_id: str


def _running_trace(agent_id: str) -> str:
    """The agent's running turn: its newest turn, neither ended nor settled.
    A turn that completed normally is settled (settled_at, outcome) and may
    keep ended_at empty: it is not running.

    TODO(#470): a private copy of "this turn is still running". At the rebase
    onto #470's fixed head this calls turn_identity.running_trace and the copy
    goes; no third spelling survives the rebase."""
    row = db.conn().execute(
        """SELECT trace_id, ended_at, settled_at FROM turns WHERE agent_id = ?
            ORDER BY started_at DESC, turn_id DESC LIMIT 1""", (agent_id,)).fetchone()
    if row is None or row["ended_at"] is not None or row["settled_at"] is not None:
        return ""
    return str(row["trace_id"] or "")


def _conversation_of(agent_id: str, trace_id: str) -> str:
    """The conversation the turn's rows are in: its prompt's, else the live one."""
    row = db.conn().execute(
        """SELECT backend_session_id FROM messages
            WHERE agent_id = ? AND trace_id = ? AND COALESCE(backend_session_id, '') != ''
            ORDER BY seq LIMIT 1""", (agent_id, trace_id)).fetchone()
    return (row["backend_session_id"] if row is not None else "") or agents_db.live_backend_session(agent_id)


def _note_id(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value and len(value) <= 64 and set(value) <= _NOTE_ID_CHARS:
        return value
    if value:
        raise NoteRefused(400, "note_id: up to 64 of a-z, 0-9, '-' and '_'")
    return secrets.token_hex(8)


def post(data: Any, *, caller_agent_id: str, caller_trace: str) -> Posted:
    """Write and notify one note. ``caller_*`` come from the request's turn
    token (X-Clarp-Turn): the agent and trace of the turn that made the call,
    or "" when it carried none."""
    if not isinstance(data, dict):
        raise NoteRefused(400, "bad json")
    session = str(data.get("session") or "").strip()
    text = str(data.get("text") or "").strip()
    if not session:
        raise NoteRefused(400, "session required")
    if not text:
        raise NoteRefused(400, f"a note needs words: {COMMAND} --from {session} --text '...'")
    if len(text) > MAX_TEXT:
        raise NoteRefused(413, f"a note is at most {MAX_TEXT} characters")
    note_id = _note_id(data.get("note_id"))
    agent = identity.lookup(session)
    if not agent:
        raise NoteRefused(404, "no such agent")
    agent_id = agent["agent_id"]
    if caller_agent_id and caller_agent_id != agent_id:
        own = (identity.lookup(caller_agent_id) or {}).get("session")
        if own:
            raise NoteRefused(403, f"this turn belongs to {own}, not to {agent.get('session')}; "
                                   f"run {COMMAND} --from {own}")
    running = _running_trace(agent_id)
    trace = caller_trace if caller_agent_id == agent_id and caller_trace else running
    if not running or trace != running:
        raise NoteRefused(409, f"{COMMAND} works only inside a running turn of {agent.get('session')}; "
                               "this call is not in one")
    backend_session_id = _conversation_of(agent_id, trace)
    note = message_store.record_note(agent_id=agent_id, backend_session_id=backend_session_id,
                                     note_id=note_id, text=text, trace_id=trace)
    if note is None:
        raise NoteRefused(409, f"{agent.get('session')} has no conversation to write a note into yet")
    notification = user_notifications.classify_note(
        agent_id=agent_id, session=str(agent.get("session") or ""),
        persona=str(agent.get("persona") or ""), note=note, backend_session_id=backend_session_id)
    return Posted(note=note, notification=notification, agent=agent,
                  backend_session_id=backend_session_id)
