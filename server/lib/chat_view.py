"""Conversation and session views of a chat (issue 702).

Every /log row says which view it belongs to (``view``):

* ``conversation``: what the user reads as the conversation, in a turn the user
  can see (audience ``user``):
  - the user's own word: a user row of origin ``user`` or ``oracle``;
  - an agent's note to the user (``kind`` note: POST /agent-notes, tell-user);
  - a row the Host writes for the user (origin ``system``: an interruption marker,
    a notice);
  - a turn's final reply: the last assistant text row (words, no tool name, not a
    note or marker) of a turn that has ended (its ``turns`` row settled, outcome not
    ``replaced``). While the turn runs it has no final reply: all its assistant text
    is ``session`` until it ends;
  - an assistant text row no known turn wrote (``trace_id`` empty) whose ``phase`` is
    ``final``.
* ``session``: everything else: assistant text before the turn's end (commentary),
  tool rows, prompts the Host or another agent sent (schedules, wakes, delivered
  answers, another agent's message), and every row of a turn between agents
  (``audience`` ``agents``).

The final reply is stored, not guessed at read time: when a turn settles, the
turn's restamp (message_writes.stamp_turn) writes ``messages.session_rows`` on
its anchor row, the final reply (else, when the turn wrote no text, its last
note or the user's prompt), and NULL on every other row of the turn. The value is
the number of the turn's rows the conversation view leaves out, which the client
offers as "Show work". The write moves the row's revision, so a delta
(``after_revision``) brings the re-tag to a client that already holds the row:
the reply that was ``session`` while the turn ran reads ``conversation`` with its
new revision. A later import that moves the final reply clears the old anchor and
sets the new one, both with new revisions.
"""
from __future__ import annotations

from typing import Any

from . import html_forms

CONVERSATION = "conversation"
SESSION = "session"
VIEWS = (CONVERSATION, SESSION)
DEFAULT_VIEW = SESSION
# The agent-roster event's kind when an agent's chat_view changes (POST /agent-chat-view).
ROSTER_KIND = "chat_view"

# The rows of a turn that are the conversation whoever reads it, before the audience
# (lib.coordination) is applied (alias m: messages). A form answer's prompt row is
# never in /log (issue 428) and is neither view's.
STRUCTURAL_SQL = f"""(
      (m.role = 'user' AND COALESCE(m.origin, 'user') IN ('user', 'oracle')
       AND NOT {html_forms.ANSWER_ROW_SQL})
   OR COALESCE(m.kind, '') = 'note'
   OR COALESCE(m.origin, '') = 'system'
   OR (m.role = 'assistant' AND m.session_rows IS NOT NULL)
   OR (m.role = 'assistant' AND COALESCE(m.trace_id, '') = '' AND COALESCE(m.phase, '') = 'final'
       AND COALESCE(m.tool_name, '') = ''))"""


def structural(row: Any) -> bool:
    """STRUCTURAL_SQL for one row (a mapping with the messages columns)."""
    role = row["role"]
    origin = row["origin"] or "user"
    if role == "user":
        return origin in ("user", "oracle") and not str(row["message_id"]).startswith(
            html_forms.ANSWER_ROW_PREFIX)
    if (row["kind"] or "") == "note" or (row["origin"] or "") == "system":
        return True
    if role != "assistant":
        return False
    if row["session_rows"] is not None:
        return True
    return (not (row["trace_id"] or "") and (row["phase"] or "") == "final"
            and not (row["tool_name"] or ""))


def view_of(row: Any, audience: str) -> str:
    """A /log row's view: its structural rule, in a turn the user can see."""
    if audience == "agents":
        return SESSION
    return CONVERSATION if structural(row) else SESSION


def _reply_candidate(row: Any) -> bool:
    return (row["role"] == "assistant" and not (row["tool_name"] or "")
            and bool(str(row["text"] or "").strip())
            and (row["kind"] or "") != "note" and (row["origin"] or "") != "system")


def anchor(rows: list[Any], *, settled: bool) -> tuple[str | None, int | None]:
    """The turn's anchor row and the count it carries, from the turn's rows in display
    order: (message_id, session_rows), or (None, None) while the turn runs, for a turn a
    follow-up replaced, or for one with nothing the user reads."""
    if not settled or not rows:
        return None, None
    chosen = next((row for row in reversed(rows) if _reply_candidate(row)), None)
    if chosen is None:
        chosen = next((row for row in reversed(rows)
                       if (row["kind"] or "") == "note"
                       or (row["role"] == "user" and structural({**dict(row), "session_rows": None}))),
                      None)
    if chosen is None:
        return None, None
    left_out = 0
    for row in rows:
        if row["message_id"] == chosen["message_id"]:
            continue
        if str(row["message_id"]).startswith(html_forms.ANSWER_ROW_PREFIX):
            continue
        if not structural({**dict(row), "session_rows": None}):
            left_out += 1
    return chosen["message_id"], left_out


# --- the agent setting -----------------------------------------------------------

def normalize(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in VIEWS else ""


def agent_view(agent_id: str, *, database=None) -> str:
    """The agent's chat view ('conversation' or 'session'; default session)."""
    if not agent_id:
        return DEFAULT_VIEW
    if database is None:
        from .db import conn
        database = conn()
    row = database.execute("SELECT chat_view FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    return (normalize(row["chat_view"]) if row is not None else "") or DEFAULT_VIEW


def set_agent_view(agent_id: str, value: Any) -> str:
    """Store the agent's chat view; answers it. ValueError for anything else."""
    stored = normalize(value)
    if not stored:
        raise ValueError(f"chat_view must be one of: {', '.join(VIEWS)}")
    from .db import conn
    conn().execute("UPDATE agents SET chat_view = ? WHERE agent_id = ?", (stored, agent_id))
    return stored


def reminder(agent: dict | None) -> str:
    """The per-turn line an agent whose chat the user reads in conversation view gets
    ('' in session view): what reaches the user and how to speak mid-turn."""
    if not agent or agent_view(agent.get("agent_id") or "") != CONVERSATION:
        return ""
    session = agent.get("session") or "<your session>"
    return ("Conversation view: the user reads this chat in Conversation view. Your final reply "
            "reaches them; text you write in between, and your tool work, does not. To tell them "
            f"something mid-turn, run `clarp-admin tell-user --from {session} --text '...'`.")
