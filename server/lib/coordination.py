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
    user's and is never 'agents', whatever arrives in it. It runs inside the
    write transaction of every message another agent sends, so it reads the
    turn's rows by trace and never the agent's whole conversation."""
    from .origins import USER_WORD_ORIGINS
    words = tuple(sorted(USER_WORD_ORIGINS))
    marks = ",".join("?" for _ in words)
    return database.execute(
        f"""SELECT 1 FROM messages INDEXED BY idx_messages_trace
             WHERE trace_id = ? AND agent_id = ? AND role = 'user'
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
    # A turn in which the agent could not leave a note is recorded but shown for good
    # ('mixed'): moving the agent to a backend with turn tokens later must not hide a turn
    # from which nothing could reach the user (the reviewer's F6B).
    audience = AGENTS if can_leave_note(agent_id) else MIXED
    database.execute(
        """INSERT INTO coordination_traces
               (agent_id, trace_id, sender_agent_id, audience, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(agent_id, trace_id) DO NOTHING""",
        (agent_id, trace, (sender_agent_id or "").strip(), audience, stamp, stamp))


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


def can_leave_note(agent_id: str) -> bool:
    """Whether the agent can leave a note for the user (POST /agent-notes needs its turn
    token): its backend gives every turn one (turn_identity.IDENTITY_BACKENDS). The one place
    this is decided. An agent that cannot is never hidden (F6-b): the failure mode of this
    feature is shown, never lost. Its turns are recorded as 'mixed' (shown for good), so a
    later move to a backend with turn tokens does not hide a turn that could leave no note."""
    if not agent_id:
        return False
    from . import agents
    return backend_leaves_notes((agents.get_by_agent_id(agent_id) or {}).get("backend"))


def backend_leaves_notes(backend: str | None) -> bool:
    """Whether an agent on ``backend`` can leave a note: the one rule can_leave_note and
    hiding_agents both read."""
    from . import backends
    from .turn_identity import IDENTITY_BACKENDS
    return backends.normalize(backend) in IDENTITY_BACKENDS


def audience_of(agent_id: str, trace_id: str | None, *, database=None) -> str:
    """'agents' while the turn of ``trace_id`` is coordination, else 'user'."""
    trace = (trace_id or "").strip()
    if not agent_id or not trace or not can_leave_note(agent_id):
        return USER
    if database is None:
        from .db import conn
        database = conn()
    row = database.execute(
        "SELECT audience FROM coordination_traces WHERE agent_id = ? AND trace_id = ?",
        (agent_id, trace)).fetchone()
    if row is None or row["audience"] != AGENTS:
        return USER
    # The read side of the wall, as /log reads it: the user's word makes it the user's.
    return USER if holds_user_word(database, agent_id, trace) else AGENTS


def sender_of(agent_id: str, trace_id: str | None, *, database=None) -> str | None:
    """The sender of a turn that is coordination now ('' when this Host does
    not know it: a peer Host's agent); None when the turn is the user's."""
    trace = (trace_id or "").strip()
    if not agent_id or not trace or not can_leave_note(agent_id):
        return None
    if database is None:
        from .db import conn
        database = conn()
    row = database.execute(
        "SELECT sender_agent_id, audience FROM coordination_traces WHERE agent_id = ? AND trace_id = ?",
        (agent_id, trace)).fetchone()
    if row is None or row["audience"] != AGENTS or holds_user_word(database, agent_id, trace):
        return None
    return row["sender_agent_id"] or ""


# The read side of "a turn the user started is never hidden": a trace marked
# 'agents' that nonetheless holds a user row with the user's word reads as the
# user's. Evaluated only for marked rows (a primary-key probe found a mark).
# The index is named on purpose: left to itself the planner searches ``u`` by
# agent (idx_messages_agent_timestamp) and walks the agent's whole conversation
# once per marked row; a real coordination trace holds no user row, so nothing
# ends that walk early. By trace it reads the turn's own rows only.
TRACE_AUDIENCE_SQL = """CASE WHEN ct.audience = 'agents' AND NOT EXISTS (
        SELECT 1 FROM messages u INDEXED BY idx_messages_trace
         WHERE u.trace_id = m.trace_id AND u.agent_id = m.agent_id
           AND u.role = 'user' AND COALESCE(u.origin, 'user') IN ('user', 'oracle'))
     THEN 'agents' WHEN ct.audience IS NULL THEN NULL ELSE 'mixed' END"""


# A row the user does not see under 'hide' (alias m: messages): a row of a trace that is still
# coordination, not a note, in a trace that holds no user row with the user's word. Applied
# in SQL before the page limit, so a page of N is N visible rows. The caller applies it only
# for an agent that can leave a note (can_leave_note): another agent is never hidden. The
# user-word lookup names idx_messages_trace, as TRACE_AUDIENCE_SQL does: by agent it would walk
# the whole conversation once per row it judges.
HIDDEN_ROW_SQL = """(EXISTS (SELECT 1 FROM coordination_traces hc
                            WHERE hc.agent_id = m.agent_id AND hc.trace_id = m.trace_id
                              AND hc.audience = 'agents')
      AND COALESCE(m.kind, '') != 'note'
      AND NOT EXISTS (SELECT 1 FROM messages hu INDEXED BY idx_messages_trace
                       WHERE hu.trace_id = m.trace_id AND hu.agent_id = m.agent_id
                         AND hu.role = 'user' AND COALESCE(hu.origin, 'user') IN ('user', 'oracle')))"""

# A counted message among the rows HIDDEN_ROW_SQL leaves out (a user or assistant row with words).
COUNTED_SQL = """(m.role IN ('user', 'assistant') AND COALESCE(m.tool_name, '') = ''
      AND TRIM(COALESCE(m.text, '')) != '')"""

# A count of what was left out stops at COUNT_CAP messages, or at COUNT_ROW_CAP rows of any kind,
# nearest the visible row it belongs to, and then says ``capped``: the apps say "100+". A user
# needs no exact number of a thousand hidden messages, and a page must never wait on counting
# them (Arnold, 20 Nov). 100 is a page: counting costs at most what reading a page does.
COUNT_CAP = 100
COUNT_ROW_CAP = 400


MODES = ("auto", "hide", "show")


def read_policy(agent_id: str, mode: str) -> dict[str, Any]:
    """The policy one /log answer is read under, as it echoes it: ``visibility`` (show | hide),
    ``hidden`` (whether coordination is left out) and ``source`` (request | chat | global)."""
    if mode in (HIDE, SHOW):
        visibility, source = mode, "request"
    else:
        own = chat_visibility(agent_id)
        visibility, source = (own, "chat") if own else (global_visibility(), "global")
    return {"visibility": visibility, "hidden": visibility == HIDE, "source": source}


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


def hiding_agents(*, database=None) -> set[str]:
    """The agents whose chat leaves coordination out under 'auto' (their effective visibility is
    'hide' and they can leave a note, as /log decides it), read for the whole roster at once: the
    snapshot's previews and the working state follow the same rule as GET /log."""
    if database is None:
        from .db import conn
        database = conn()
    host = global_visibility()
    return {row["agent_id"] for row in database.execute(
                "SELECT agent_id, backend, coordination_visibility FROM agents WHERE deleted_at IS NULL")
            if (normalize_visibility(row["coordination_visibility"]) or host) == HIDE
            and backend_leaves_notes(row["backend"])}


def working_with(agent_id: str, trace_id: str | None) -> dict[str, Any]:
    """Who a running turn is for: {"audience": "user"} or {"audience": "agents"} with
    ``sender_agent_id`` and ``sender_name`` when another agent of this Host started it, so an
    app can say who the agent is working with. {} when there is no turn to judge."""
    if not agent_id or not (trace_id or "").strip():
        return {}
    sender = sender_of(agent_id, trace_id)
    if sender is None:
        return {"audience": USER}
    out: dict[str, Any] = {"audience": AGENTS}
    if sender:
        from . import agents
        agent = agents.get_by_agent_id(sender)
        if agent:
            out.update(sender_agent_id=sender, sender_name=agent.get("persona") or "")
    return out


def veiled(working: dict[str, Any]) -> str:
    """The words a working state shows for a coordination turn its chat hides: who the agent
    works with, never what it does in that turn (a tool's label, a reasoning title)."""
    name = (working or {}).get("sender_name") or ""
    return f"Working with {name}" if name else "Working with another agent"


def veil_activity(activity: dict[str, Any], veil: str) -> dict[str, Any]:
    """An agent-activity (lib.activity.state_activity_event) of a turn its chat hides: who the
    agent works with (``veil``), no tool, action, summary or path of that turn."""
    if not veil:
        return activity
    if activity.get("phase") == "account_recovery":
        # Waiting for an account says so: the words are about the account, not the turn's work.
        return {**activity, "tool": "", "file_path": "", "call_id": None}
    return {**activity, "tool": "", "action": "working", "summary": veil, "file_path": "",
            "call_id": None}


def veil_live(activity: Any, veil: str) -> Any:
    """A live status (docs/live-items.md §1.3) of a turn its chat hides."""
    if not veil or not isinstance(activity, dict):
        return activity
    return {**activity, "headline": veil, "tool": None}


def veil_detail(detail: Any, veil: str) -> Any:
    """An agent-state's detail of a turn its chat hides: its trace and the veil, nothing the
    turn did (a tool, its input, a message)."""
    if not veil or not isinstance(detail, dict):
        return detail
    kept = {k: detail[k] for k in ("trace_id", "phase", "account_recovery") if k in detail}
    return {**kept, "summary": veil}


ROSTER_KIND = "coordination-changed"
DEFAULT_CHOICE = "default"   # POST /agent-coordination: follow the Host's setting again


def set_global(value: Any) -> str:
    """Store the Host's setting ('show' | 'hide'); ValueError for anything else."""
    visibility = normalize_visibility(value)
    if not visibility:
        raise ValueError(f"coordination_visibility must be one of: {', '.join(VISIBILITIES)}")
    settings_store.set_text(GLOBAL_KEY, visibility)
    return visibility


def set_chat(agent_id: str, value: Any) -> str:
    """Store one chat's own choice ('show' | 'hide', or 'default' to follow the
    Host); answers what is stored ('' for default). ValueError otherwise."""
    text = str(value or "").strip().lower()
    stored = "" if text == DEFAULT_CHOICE else normalize_visibility(text)
    if not stored and text != DEFAULT_CHOICE:
        raise ValueError(f"visibility must be one of: {', '.join(VISIBILITIES + (DEFAULT_CHOICE,))}")
    from . import agents
    agents.update_agent(agent_id, coordination_visibility=stored)
    return stored


def overrides(*, database=None) -> dict[str, str]:
    """Every chat's own choice that is set: {agent_id: 'show' | 'hide'}."""
    if database is None:
        from .db import conn
        database = conn()
    return {row["agent_id"]: normalize_visibility(row["coordination_visibility"])
            for row in database.execute(
                "SELECT agent_id, coordination_visibility FROM agents"
                " WHERE deleted_at IS NULL AND coordination_visibility != ''")
            if normalize_visibility(row["coordination_visibility"])}


def snapshot_fields(agent_id: str, chosen: dict[str, str], host: str) -> dict[str, Any]:
    """What a snapshot agent row says: the effective visibility and the chat's own choice."""
    own = chosen.get(agent_id) or ""
    return {"coordination_visibility": own or host, "coordination_override": own or None}


def reminder(agent: dict | None, *, origin: str | None, sender_agent_id: str | None) -> str:
    """The line a coordination turn's prompt carries when this chat hides
    coordination ('' otherwise): it must say the truth for the chat at the
    moment the turn starts. Under 'show' the user reads the turn as before,
    so nothing is said."""
    if not agent or not is_coordination(agent_id=agent.get("agent_id") or "", origin=origin,
                                        sender_agent_id=sender_agent_id):
        return ""
    if not can_leave_note(agent.get("agent_id") or ""):
        return ""   # its turns are shown (F6-b); it would be told to run a command that refuses it
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

