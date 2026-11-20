"""Coordination between agents: which rows of a chat answer another agent.

When one agent sends another a message, the turn it starts in the
recipient's chat is coordination between agents: the prompt, the live rows,
the transcript rows the import brings in (thinking, tools, the reply) and the
final row. The user may keep it out of the chat (issue 478); it is never lost.

The Host decides, from structure it can check, never from what a sender
says: a message is coordination when another agent sent it (the sender
resolves on this Host and is not the recipient), or when it arrived as
another agent's with no sender this Host knows (an agent of a peer Host). No
flag of a sender hides anything. An agent shows something to the user only by
an explicit act: a note (``POST /agent-notes``), a decision card or an
artifact, all of which are shown whatever turn made them.

The mark belongs to the turn's trace, in ``coordination_traces``, and is read
with the rows (``audience``). It is never put on a message row: the transcript
import rewrites a row's origin on every pass from the latest user row, and
moves rows between positional ids, but it never rewrites a trace.

A trace's audience only ever loosens: 'agents' becomes 'mixed' when a message
that is not coordination (the user's, the Oracle's, the Host's) is steered
into the running turn, and a mixed turn is shown as any other. A turn the user
started is never marked, whatever arrives in it: an agent's message steered
into it is a row of its own trace and hides alone.
"""
from __future__ import annotations

from typing import Any

from . import settings_store
from .clock import now_ms

AGENTS = "agents"   # coordination: shown only when the user chooses to see it
MIXED = "mixed"     # coordination the user took part in: shown
USER = "user"       # every other row

SHOW = "show"
HIDE = "hide"
# A string enum from the start, stored and echoed as a string, so a later
# value adds no contract break.
VISIBILITIES = (SHOW, HIDE)
DEFAULT_VISIBILITY = SHOW
GLOBAL_KEY = "coordination_visibility"

NOTE_KIND = "note"
# A note's origin: a word no app draws as anything but an ordinary reply
# (issue 478, point 6: iOS, Android and the desktop all read it so).
NOTE_ORIGIN = "note"


def is_coordination(*, agent_id: str, origin: str | None, sender_agent_id: str | None) -> bool:
    """Whether a message to ``agent_id`` is another agent addressing it.

    The user's own word (origin 'user' or 'oracle') never is, whatever sender
    it names: a sender named in a request is a claim. A send made from an
    agent's own turn never arrives with the user's word: the /send boundary
    gives it that agent as its sender and origin 'agent' (lib.send_request).
    Otherwise a resolved sender that is another agent is coordination
    whatever the origin, and so is origin 'agent' with no sender this Host
    knows (an agent of a peer Host). An agent addressing itself is not."""
    from .origins import USER_WORD_ORIGINS
    if (origin or "").strip() in USER_WORD_ORIGINS:
        return False
    sender = (sender_agent_id or "").strip()
    if sender:
        return sender != agent_id
    return (origin or "").strip() == "agent"


def holds_user_word(database, agent_id: str, trace_id: str) -> bool:
    """Whether the trace holds the user's own word for this agent: a user row
    or a prompt admission with origin 'user' or 'oracle'. Such a turn is the
    user's and is never 'agents', whatever arrives in it."""
    from .origins import USER_WORD_ORIGINS
    words = tuple(sorted(USER_WORD_ORIGINS))
    marks = ",".join("?" for _ in words)
    return database.execute(
        f"""SELECT 1 FROM messages WHERE trace_id = ? AND agent_id = ? AND role = 'user'
               AND COALESCE(origin, 'user') IN ({marks}) LIMIT 1""",
        (trace_id, agent_id, *words)).fetchone() is not None or database.execute(
        f"""SELECT 1 FROM prompt_admissions WHERE trace_id = ? AND agent_id = ?
               AND origin IN ({marks}) LIMIT 1""",
        (trace_id, agent_id, *words)).fetchone() is not None


def mark(database, *, agent_id: str, trace_id: str, sender_agent_id: str | None,
         at: int | None = None) -> None:
    """Record that ``trace_id`` was started by another agent. Joins the
    caller's transaction; a trace already recorded keeps its audience, and a
    trace that holds the user's own word is never marked."""
    trace = (trace_id or "").strip()
    if not agent_id or not trace or holds_user_word(database, agent_id, trace):
        return
    stamp = int(at if at is not None else now_ms())
    database.execute(
        """INSERT INTO coordination_traces
               (agent_id, trace_id, sender_agent_id, audience, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(agent_id, trace_id) DO NOTHING""",
        (agent_id, trace, (sender_agent_id or "").strip(), AGENTS, stamp, stamp))


def carry(database, *, agent_id: str, from_trace: str | None, to_trace: str | None) -> None:
    """A message handed to a new attempt keeps its mark under the new trace."""
    old, new = (from_trace or "").strip(), (to_trace or "").strip()
    if not old or not new or old == new:
        return
    database.execute(
        """INSERT INTO coordination_traces
               (agent_id, trace_id, sender_agent_id, audience, created_at, updated_at)
           SELECT agent_id, ?, sender_agent_id, audience, created_at, ?
             FROM coordination_traces WHERE agent_id = ? AND trace_id = ?
           ON CONFLICT(agent_id, trace_id) DO NOTHING""",
        (new, now_ms(), agent_id, old))


def audience_of(agent_id: str, trace_id: str | None, *, database=None) -> str:
    """'agents' while the turn of ``trace_id`` is coordination, else 'user'."""
    trace = (trace_id or "").strip()
    if not agent_id or not trace:
        return USER
    if database is None:
        from .db import conn
        database = conn()
    row = database.execute(
        "SELECT audience FROM coordination_traces WHERE agent_id = ? AND trace_id = ?",
        (agent_id, trace)).fetchone()
    return AGENTS if row is not None and row["audience"] == AGENTS else USER


def sender_of(agent_id: str, trace_id: str | None, *, database=None) -> str | None:
    """The sender of a turn that is coordination now ('' when this Host does
    not know it: a peer Host's agent); None when the turn is the user's."""
    trace = (trace_id or "").strip()
    if not agent_id or not trace:
        return None
    if database is None:
        from .db import conn
        database = conn()
    row = database.execute(
        "SELECT sender_agent_id, audience FROM coordination_traces WHERE agent_id = ? AND trace_id = ?",
        (agent_id, trace)).fetchone()
    if row is None or row["audience"] != AGENTS:
        return None
    return row["sender_agent_id"] or ""


# The read side of "a turn the user started is never hidden": a trace marked
# 'agents' that nonetheless holds a user row with the user's word reads as the
# user's. Evaluated only for marked rows (a primary-key probe found a mark).
TRACE_AUDIENCE_SQL = """CASE WHEN ct.audience = 'agents' AND NOT EXISTS (
        SELECT 1 FROM messages u WHERE u.trace_id = m.trace_id AND u.agent_id = m.agent_id
           AND u.role = 'user' AND COALESCE(u.origin, 'user') IN ('user', 'oracle'))
     THEN 'agents' WHEN ct.audience IS NULL THEN NULL ELSE 'mixed' END"""


def row_audience(*, kind: str | None, trace_audience: str | None) -> str:
    """A row's audience from its trace's (None: no mark). A note is always the user's."""
    if (kind or "") == NOTE_KIND:
        return USER
    return AGENTS if trace_audience == AGENTS else USER


def loosen(agent_id: str, trace_id: str | None) -> list[str]:
    """Show a coordination turn from now on ('agents' -> 'mixed').

    Every row of the trace gets a new revision, and its conversation head
    rises with it (message_writes.restamp_trace), so a client that left those
    rows out finds them in its next delta. Answers the conversations whose
    rows moved ([] when nothing changed: no mark, or already mixed)."""
    trace = (trace_id or "").strip()
    if not agent_id or not trace:
        return []
    from .db import conn
    from .message_writes import _row_with_its_head, restamp_trace
    database = conn()
    with _row_with_its_head(database):
        changed = database.execute(
            """UPDATE coordination_traces SET audience = ?, updated_at = ?
                WHERE agent_id = ? AND trace_id = ? AND audience = ?""",
            (MIXED, now_ms(), agent_id, trace, AGENTS)).rowcount
        if not changed:
            return []
        return restamp_trace(database, agent_id, trace)

def normalize_visibility(value: Any) -> str:
    """'show' or 'hide' as stored; '' for anything else (follow the Host)."""
    text = str(value or "").strip().lower()
    return text if text in VISIBILITIES else ""


def global_visibility() -> str:
    return normalize_visibility(settings_store.get_text(GLOBAL_KEY, default=DEFAULT_VISIBILITY)) \
        or DEFAULT_VISIBILITY


def chat_visibility(agent_id: str, *, database=None) -> str:
    """The chat's own choice ('show' or 'hide'), or '' to follow the Host."""
    if not agent_id:
        return ""
    if database is None:
        from .db import conn
        database = conn()
    row = database.execute("SELECT coordination_visibility FROM agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
    return normalize_visibility(row["coordination_visibility"]) if row is not None else ""


def effective_visibility(agent_id: str) -> str:
    """The chat's own choice, else the Host's: 'show' or 'hide'."""
    return chat_visibility(agent_id) or global_visibility()


def reminder(agent: dict | None, *, origin: str | None, sender_agent_id: str | None) -> str:
    """The line a coordination turn's prompt carries when this chat hides
    coordination ('' otherwise): it must say the truth for the chat at the
    moment the turn starts. Under 'show' the user reads the turn as before,
    so nothing is said."""
    if not agent or not is_coordination(agent_id=agent.get("agent_id") or "", origin=origin,
                                        sender_agent_id=sender_agent_id):
        return ""
    if effective_visibility(agent.get("agent_id") or "") != HIDE:
        return ""
    sender = None
    if sender_agent_id:
        from . import identity
        sender = identity.lookup(sender_agent_id)
    who = f" ({sender.get('persona') or sender.get('session')})" if sender else ""
    session = agent.get("session") or "<your session>"
    return (f"Coordination: this turn answers a message from another agent{who}. The user has "
            "chosen not to see such turns in this chat, so nothing you write in it, your final "
            "reply included, reaches the user. To tell the user something, run "
            f"`clarp-admin tell-user --from {session} --text '...'`: it appears in this chat "
            "and notifies the user. Decision cards and artifacts are shown as always.")

