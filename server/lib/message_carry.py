"""Which database rows a tail import may find changed.

A tail import leaves the turns before its boundary alone. That is the full
replay's result only when a full replay would leave them alone as well and
reach the boundary with the same carried state. A replay reads database rows.
The import that published the frontier recorded them (five groups, as
``message_writes._prepare_import_prelude`` returns them with ``raw_only``),
and a tail is refused when they no longer stand.

Rows that are identical always stand. Each rule here names rows that the
turns before the boundary cannot read, with the reason, so that their change
no longer costs a full import. A rule that is not given is not applied; with
none, every row must be identical, as before the rules existed. The reference
lane (``tests/readmodel/test_rm_n14_carry.py``) runs every rule in pairs: the
change it lets through is a tail and equals the full replay, the nearest
change it does not let through is a full import and equals it too.

What stays a full import on purpose: any change to a row of this session
that is not a new /send row (a streamed reply's live row, a finalized row, a
turn's rows re-stamped when it settles), a turn row that began before the
boundary or belongs to a row the session already holds, and a new /send row
whose words were said before.
"""
from __future__ import annotations

from .message_import_common import _norm_text
from .message_text import DELIVERY_CONTEXT_OPEN
from .message_turns import _START_SLACK_MS

# Message rows of the agent's other sessions do not count. The replay of one
# transcript reads message rows by this session, by a row id of this session,
# or by the trace of a row it has just changed; never another session's rows
# for a turn it leaves unchanged. The turn rows stay agent-wide: a reply's
# trace goes by them.
SESSION_ROWS = "carry_session_rows"
# Turn rows that began after the last turn before the boundary do not count.
# An old turn asks the turn index two things: which turn was running at its
# own time, and how the turn of a given trace ended.
LATER_TURNS = "carry_later_turns"
# A new /send row does not count when no turn before the boundary can be
# linked to it: its words were not said before.
NEW_SENDS = "carry_new_sends"
RULES = (SESSION_ROWS, LATER_TURNS, NEW_SENDS)

_ABSENT = object()


def changed(expected, current, *, rules=frozenset(), carry=None, boundary_seq: int = 0,
            source_file: str = "", backend_session_id: str = "") -> str:
    """'' when the turns before the boundary would be replayed as they stand.
    Otherwise what stands in the way: fixed words, comma separated, for the
    log (no ids, no text).

    ``expected`` are the rows the frontier was published with, ``current``
    the rows now; ``carry`` is the checkpoint's store carry (the state at the
    boundary), ``boundary_seq`` the first turn the tail imports."""
    if expected[3] is None or current[3] is None:
        return "not_recorded"
    if tuple(current) == tuple(expected):
        return ""
    carry = carry or {}
    old_turns, _old_sends, old_user, old_rows, old_oracle = expected
    turns, _sends, user, rows, oracle = current
    found: set[str] = set()
    if rows != old_rows:
        # The /send rows (the second group) are among these rows, with the
        # columns their order goes by: judged here, once.
        found.update(_rows_changed(old_rows, rows, rules=rules, carry=carry,
                                   boundary_seq=boundary_seq, source_file=source_file,
                                   backend_session_id=backend_session_id))
    if turns != old_turns:
        found.update(_turns_changed(old_turns, turns, old_rows, carry)
                     if LATER_TURNS in rules else ("turn_rows",))
    if user != old_user and not (
            NEW_SENDS in rules and carry.get("initial_provenance_used") is False):
        # The conversation's latest user row is read by replies that come
        # before any user turn of the transcript (a call opening first). The
        # checkpoint records whether there are such replies before the
        # boundary; when it says there are none, the row is never read.
        found.add("latest_user_row")
    if oracle != old_oracle:
        found.add("oracle_rows")
    return ",".join(sorted(found))


def _kind(row, source_file: str, backend_session_id: str) -> str:
    if row["backend_session_id"] != backend_session_id:
        return "row_other_session"
    source = str(row["source_file"] or "")
    for prefix, name in (("client:", "row_send"), ("live:", "row_live"), ("final:", "row_final")):
        if source.startswith(prefix):
            return name
    return "row_transcript" if source == source_file else "row_other"


def _rows_changed(old_rows, rows, *, rules, carry, boundary_seq, source_file,
                  backend_session_id) -> set[str]:
    found: set[str] = set()
    old_by_id = {row["message_id"]: row for row in old_rows}
    now_by_id = {row["message_id"]: row for row in rows}
    for message_id, row in old_by_id.items():
        if now_by_id.get(message_id) != row:        # edited or gone
            found.add(_kind(row, source_file, backend_session_id))
    said = None
    for message_id, row in now_by_id.items():
        if message_id in old_by_id:
            continue
        kind = _kind(row, source_file, backend_session_id)
        if kind != "row_send" or NEW_SENDS not in rules:
            found.add(kind)
            continue
        if said is None:
            said = _said_before(old_rows, carry, boundary_seq, source_file, backend_session_id)
        text = str(row["text"] or "")
        # A row that begins with the delivery notice decides whether an old
        # copy of a prompt is stripped of its notice: said before, always.
        if text.lstrip().startswith(DELIVERY_CONTEXT_OPEN) or _norm_text(text) in said:
            found.add("send_said_before")
    return found


def _said_before(old_rows, carry, boundary_seq, source_file, backend_session_id) -> set[str]:
    """The words of every user turn before the boundary, as the linker keys
    them: a turn either was linked to a /send row (the carry lists those) or
    is stored as a transcript row with the text it was looked up by."""
    linked = set(carry.get("consumed_client_ids") or ())
    return {_norm_text(row["text"]) for row in old_rows
            if row["backend_session_id"] == backend_session_id and (
                row["message_id"] in linked
                or (row["source_file"] == source_file and row["role"] == "user"
                    and int(row["seq"]) < boundary_seq))}


def _outcomes(turn_rows) -> dict:
    return {str(row["trace_id"]): row["outcome"] for row in turn_rows if row["trace_id"]}


def _turns_changed(old_turns, turns, old_rows, carry) -> set[str]:
    latest = carry.get("prefix_latest_ms")
    if latest is None:
        return {"turn_rows"}          # a checkpoint that did not record it
    found: set[str] = set()
    # Which turn was running at an old turn's time: the latest that began up
    # to _START_SLACK_MS after it (message_turns.TurnIndex.running_at), so
    # only turn rows that began by then can be the answer.
    limit = int(latest) + _START_SLACK_MS
    if ([row for row in turns if int(row["started_at"]) <= limit]
            != [row for row in old_turns if int(row["started_at"]) <= limit]):
        found.add("turn_before_boundary")
    # How a trace's turn ended, and whether the trace is a turn at all: asked
    # for the trace an old row carries or was linked by. Every such trace is
    # on a row the session held when the frontier was published.
    before, now = _outcomes(old_turns), _outcomes(turns)
    if any(before.get(trace, _ABSENT) != now.get(trace, _ABSENT)
           for trace in {row["trace_id"] for row in old_rows if row["trace_id"]}):
        found.add("turn_of_old_row")
    return found
