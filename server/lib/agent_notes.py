"""An agent's note to the user: ``POST /agent-notes`` (``clarp-admin tell-user``).

The one thing an agent can do to put words in front of the user from any
turn, also from a turn that answers another agent, whose rows the user may
keep out of the chat (lib.coordination). The note is a row of the calling
turn in the agent's own chat, always shown, and it notifies on its own
(reason ``agent-note``). It is never spoken in this version.

It must come from the agent's own running turn, proven by its Host-issued
turn token (X-Clarp-Turn): a call without one is refused for every
principal (a device, a peer Host, the Host's own token, which every agent
holds), and so is another agent's token and an agent with no turn running.
An agent on the shared Codex app-server has no turn token and so cannot
leave a note in this version.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from . import agents as agents_db, db, identity, message_store, turn_identity, user_notifications

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


def _newest_trace(agent_id: str) -> str:
    """The trace of the agent's newest turn, whatever its state ('' when it has none);
    whether it is running is turn_identity.running_trace's to say."""
    row = db.conn().execute(
        """SELECT trace_id FROM turns WHERE agent_id = ?
            ORDER BY started_at DESC, turn_id DESC LIMIT 1""", (agent_id,)).fetchone()
    return str(row["trace_id"] or "") if row is not None else ""


def _conversation_of(agent_id: str, trace_id: str) -> str:
    """The conversation the turn's rows are in: its prompt's, else the live one."""
    row = db.conn().execute(
        """SELECT backend_session_id FROM messages INDEXED BY idx_messages_trace
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
    if not caller_agent_id:
        # Only the agent's own turn may speak for it: not a device, not a peer
        # Host, not the Host's own token, which every agent holds (F6).
        raise NoteRefused(403, f"{COMMAND} works only from a turn of {agent.get('session')}: "
                               "this call carries no turn of an agent")
    if caller_agent_id != agent_id:
        own = (identity.lookup(caller_agent_id) or {}).get("session")
        if own:
            raise NoteRefused(403, f"this turn belongs to {own}, not to {agent.get('session')}; "
                                   f"run {COMMAND} --from {own}")
        raise NoteRefused(403, f"this turn belongs to an agent that no longer exists, not to "
                               f"{agent.get('session')}; {COMMAND} works only from its own turn")
    # The Host's one reading of "this turn is still running" (PR 470).
    trace = caller_trace or _newest_trace(agent_id)
    if not turn_identity.running_trace(agent_id, trace):
        raise NoteRefused(409, f"{COMMAND} works only inside a running turn of {agent.get('session')}; "
                               "this call is not in one")
    backend_session_id = _conversation_of(agent_id, trace)
    held = db.conn().execute(
        "SELECT text FROM messages WHERE message_id = ?",
        (message_store.note_message_id(agent_id, note_id),)).fetchone()
    if held is not None and held["text"] != text:
        raise NoteRefused(409, f"note_id {note_id} is already a note of {agent.get('session')} with "
                               "other words; send the new words with a new note_id")
    note = message_store.record_note(agent_id=agent_id, backend_session_id=backend_session_id,
                                     note_id=note_id, text=text, trace_id=trace)
    if note is None:
        raise NoteRefused(409, f"{agent.get('session')} has no conversation to write a note into yet")
    notification = user_notifications.classify_note(
        agent_id=agent_id, session=str(agent.get("session") or ""),
        persona=str(agent.get("persona") or ""), note=note, backend_session_id=backend_session_id)
    return Posted(note=note, notification=notification, agent=agent,
                  backend_session_id=backend_session_id)
