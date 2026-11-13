"""Which database rows a tail import may find changed.

A tail import leaves the turns before its boundary alone. That is the full
replay's result only when a full replay would leave them alone as well and
reach the boundary with the same carried state. A replay reads database rows.
The import that published the frontier recorded them (five groups, as
``message_writes._prepare_import_prelude`` returns them with ``raw_only``),
and a tail is refused when they no longer stand.

What is kept of those rows is a record of constant size (``record``), not
the rows: the highest revision among the message rows (the mark), digests
per kind of message row over every column of every row of that kind, three
digests of the turn rows, one of the latest user row and one of the Oracle
rows. Kept in the process and stored beside the checkpoint alike, it weighs
about a kilobyte for a conversation of any size.

At a tail the rows are read again and split by the mark. The rows at or
below it must hash to the recorded digests: that proves every old row is
there and unchanged in every column. The revision is not trusted for it. An
old row edited without a new revision changes its digest; one edited with a
new revision, or removed, is missing from it; a row that appears with an old
revision lands in a digest and breaks it. The rows above the mark are then
exactly the new ones.

The rows at or below the mark are hashed in two parts, split at a second,
lower revision (the base): the rows up to it, which are nearly all of a long
conversation, and the few written since. A tail that leaves the first part
untouched publishes its record with that part's digests as they are and
hashes only the second, so each tail hashes the conversation once, not
twice. "Untouched" is not assumed: the rows the guard hashed are compared,
value for value, with the rows read at publication.

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

import hashlib
import json
import operator
import pickle

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
# A live row of this session (the row a reply streams into) may be new,
# rewritten or gone. It is a different kind of rule: such a row is rewritten
# in place with every delta, so what it lets through is a CHANGED old row,
# not only a new one. What a turn before the boundary reads of a live row is
# one thing (message_writes._finish_import): a leftover live row is removed
# when a reply of ITS OWN trace with related words is in the import's batch,
# and a full import's batch holds every reply where a tail's holds those
# from the boundary. (A reply adopts a live row only when its slot is empty,
# message_import_rows._resolve_reply_id, and the slot of a reply before the
# boundary is not: its row stands.) So a live row is let through unless a
# reply before the boundary carries its trace, whatever its words: the
# running turn then spans the boundary. A live row that is as it was when
# the frontier was published is not asked: that publication left it there.
# Off unless switched on.
LIVE_ROWS = "carry_live_rows"
RULES = (SESSION_ROWS, LATER_TURNS, NEW_SENDS, LIVE_ROWS)

# The kinds of message row, by where the row is now: one digest each, and the
# word a refusal names it by.
KINDS = ("row_send", "row_live", "row_final", "row_transcript", "row_other", "row_other_session")
_SOURCE_KINDS = (("client:", "row_send"), ("live:", "row_live"), ("final:", "row_final"))
_TURN_COLUMNS = ("trace_id", "started_at", "settled_at", "outcome")
_VERSION = 2
_ABSENT = "\x00absent"


def _digest(value) -> str:
    """One digest of plain values (text, numbers, None, tuples, lists).

    Different values never pickle to the same bytes, and that is all a
    record rests on. The other direction does not hold and must not be built
    on: values that compare equal can pickle differently (5 and 5.0, 1 and
    True, a str subclass, one string object shared by two cells against two
    equal strings). Such a difference only makes a digest differ where the
    rows are the same, and a digest that differs costs a full import; it
    never lets a changed row pass. The rows SQLite returns for unchanged
    data pickle the same in every process (protocol 4 is fixed for that;
    checked across Python 3.12 to 3.14 and two SQLite versions). SHA-256,
    which the processors this runs on compute in hardware; 128 bits kept."""
    return hashlib.sha256(pickle.dumps(value, protocol=4)).hexdigest()[:32]


def _kind_of(source, session, source_file: str, backend_session_id: str) -> str:
    if session != backend_session_id:
        return "row_other_session"
    if source == source_file:
        return "row_transcript"
    source = str(source or "")
    for prefix, name in _SOURCE_KINDS:
        if source.startswith(prefix):
            return name
    return "row_other"


class _Rows:
    """Rows as plain tuples, in an order SQLite has no say in: the columns by
    name, the rows by ``key`` (the table's primary key)."""

    def __init__(self, rows, key: str = "message_id"):
        first = rows[0] if rows else {}
        names = list(first.keys())
        self.names = tuple(sorted(names))
        if isinstance(first, dict):
            values = [tuple(row[name] for name in self.names) for row in rows]
        else:       # sqlite3.Row: by position, which is what is fast
            order = sorted(range(len(names)), key=names.__getitem__)
            pick = operator.itemgetter(*order) if len(order) > 1 else (lambda row: (row[order[0]],))
            values = [pick(row) for row in rows]
        at = {name: position for position, name in enumerate(self.names)}
        self.at = at
        if values:
            values.sort(key=operator.itemgetter(at[key]))
        self.values = values

    def split(self, *marks: int) -> list[list]:
        """The rows by revision: at or below the first mark, above it up to
        the second, and so on; last, those above every mark."""
        revision = self.at.get("revision")
        parts: list[list] = [[] for _ in range(len(marks) + 1)]
        if revision is None:
            parts[0] = self.values
            return parts
        for row in self.values:
            number = int(row[revision] or 0)
            for index, mark in enumerate(marks):
                if number <= mark:
                    parts[index].append(row)
                    break
            else:
                parts[-1].append(row)
        return parts

    def kind(self, row, source_file, backend_session_id) -> str:
        return _kind_of(row[self.at["source_file"]], row[self.at["backend_session_id"]],
                        source_file, backend_session_id)

    def live(self, backend_session_id) -> list:
        """The rows of this session a reply streams into, as they are now."""
        source, session = self.at.get("source_file"), self.at.get("backend_session_id")
        if source is None:
            return []
        return [row for row in self.values
                if row[session] == backend_session_id and str(row[source] or "").startswith("live:")]

    def digests(self, values, source_file, backend_session_id) -> dict[str, str]:
        """One digest per kind, over every column of every row of that kind."""
        by_kind: dict[str, list] = {kind: [] for kind in KINDS}
        if values:
            source, session = self.at["source_file"], self.at["backend_session_id"]
            transcript = by_kind["row_transcript"].append
            for row in values:
                if row[session] == backend_session_id and row[source] == source_file:
                    transcript(row)     # nearly every row of a long conversation
                else:
                    by_kind[_kind_of(row[source], row[session], source_file,
                                     backend_session_id)].append(row)
        return {kind: _digest((self.names, found)) for kind, found in by_kind.items()}


def _turn_values(turn_rows) -> list[tuple]:
    # In the order the turn index reads them (by start): among turns that
    # began in the same millisecond that order decides which one was running.
    return [tuple(row[name] for name in _TURN_COLUMNS) for row in turn_rows]


def _early(turns: list[tuple], carry) -> str | None:
    """The turn rows that can have been running at an old turn's time: those
    that began up to _START_SLACK_MS after the last turn before the boundary
    (message_turns.TurnIndex.running_at). None when the checkpoint did not
    record that time."""
    latest = (carry or {}).get("prefix_latest_ms")
    if latest is None:
        return None
    limit = int(latest) + _START_SLACK_MS
    return _digest([row for row in turns if int(row[1]) <= limit])


def _ends(turns: list[tuple], traces) -> str:
    """How the turn of each trace ended, and whether the trace is a turn at
    all: asked for the trace an old row carries or was linked by."""
    outcomes = {str(row[0]): row[3] for row in turns if row[0]}
    return _digest(sorted((trace, outcomes.get(trace, _ABSENT)) for trace in traces))


def _oracle(oracle_rows) -> str:
    """'' for an agent with no Oracle history (a frontier is kept for no other)."""
    if not oracle_rows:
        return ""
    table = _Rows(oracle_rows, key="delegation_id")
    return _digest((table.names, table.values))


def record(raw_inputs, *, carry=None, source_file: str = "", backend_session_id: str = "",
           session_rows: bool = False, witness=None) -> dict | None:
    """What a frontier keeps of the rows it is published with (the five
    groups of ``_prepare_import_prelude`` with ``raw_only``). None when the
    message rows were not read.

    ``witness`` is what ``changed`` left when it hashed the rows up to the
    recorded base and found them standing: that base, those rows and their
    digests. When the rows up to it are still those rows, value for value,
    their digests are kept and only the rows above it are hashed."""
    turns, _sends, user, rows, oracle = raw_inputs
    if rows is None:
        return None
    table = _Rows(rows)
    revision = table.at.get("revision")
    mark = max((int(row[revision] or 0) for row in table.values), default=0) \
        if revision is not None else 0
    base, digests = mark, None
    if witness is not None and witness[0] <= mark:
        kept, since = table.split(witness[0])
        if kept == witness[1]:
            base, digests = witness[0], {
                "base": witness[2], "since": table.digests(since, source_file, backend_session_id)}
    if digests is None:
        digests = {"base": table.digests(table.values, source_file, backend_session_id),
                   "since": table.digests((), source_file, backend_session_id)}
    turn_values = _turn_values(turns)
    trace = table.at.get("trace_id")
    ident = table.at.get("message_id")
    return {
        "version": _VERSION,
        "scope": "session" if session_rows else "agent",
        "mark": mark,
        "base": base,
        "rows": digests,
        # Each live row of this session by itself (there are few): LIVE_ROWS
        # asks only those that are not as they were.
        "live": {str(row[ident]): _digest((table.names, row))
                 for row in table.live(backend_session_id)},
        "turns": _digest(turn_values),
        "early": _early(turn_values, carry),
        "ends": _ends(turn_values, {row[trace] for row in table.values if row[trace]}
                      if trace is not None else ()),
        "user": _digest(tuple(user)),
        "oracle": _oracle(oracle),
    }


def dumps(recorded: dict) -> str:
    return json.dumps(recorded, sort_keys=True, separators=(",", ":"))


def loads(text) -> dict | None:
    """The record a frontier row stores, or None when the text is not one
    (a row written before records existed holds a bare digest there)."""
    try:
        recorded = json.loads(text)
    except (TypeError, ValueError):
        return None
    if (not isinstance(recorded, dict) or recorded.get("version") != _VERSION
            or not isinstance(recorded.get("mark"), int)
            or not isinstance(recorded.get("base"), int)
            or not isinstance(recorded.get("rows"), dict)
            or not isinstance(recorded.get("live"), dict)
            or set(recorded["rows"]) != {"base", "since"}
            or any(not isinstance(part, dict) or set(part) != set(KINDS)
                   for part in recorded["rows"].values())):
        return None
    return recorded


def changed(recorded, current, *, rules=frozenset(), carry=None, boundary_seq: int = 0,
            source_file: str = "", backend_session_id: str = "",
            session_rows: bool = False, witness: dict | None = None) -> str:
    """'' when the turns before the boundary would be replayed as they stand.
    Otherwise what stands in the way: fixed words, comma separated, for the
    log (no ids, no text).

    ``recorded`` is the record the frontier was published with, ``current``
    the rows now; ``carry`` is the checkpoint's store carry (the state at the
    boundary), ``boundary_seq`` the first turn the tail imports. ``witness``,
    when given, receives under "base" what ``record`` can build on."""
    if recorded is None or current[3] is None:
        return "not_recorded"
    if recorded["scope"] != ("session" if session_rows else "agent"):
        return "rows_scope"       # carry_session_rows was switched since
    carry = carry or {}
    turns, _sends, user, rows, oracle = current
    found: set[str] = set()
    table = _Rows(rows)
    base, since, new = table.split(recorded["base"], recorded["mark"])
    # Every row the frontier was published with is at or below the mark. They
    # are all there and unchanged exactly when each kind hashes as recorded,
    # in both parts: up to the base, and above it.
    live_rows = LIVE_ROWS in rules
    for part, values in (("base", base), ("since", since)):
        now = table.digests(values, source_file, backend_session_id)
        differing = [kind for kind in KINDS if now[kind] != recorded["rows"][part][kind]]
        # Under LIVE_ROWS the live rows are asked one by one below.
        found.update(kind for kind in differing if not (live_rows and kind == "row_live"))
        if part == "base" and not differing and witness is not None:
            witness["base"] = (recorded["base"], base, recorded["rows"]["base"])
    rows_stand = not found
    # The old rows are both parts. No turn before the boundary has a row in
    # the second part, but the traces the record was published with are the
    # traces of both: asked of the first part alone, a turn row that changed
    # would be refused for every conversation that has rows since its base.
    old = base + since if since else base
    said = None
    for row in new:
        kind = table.kind(row, source_file, backend_session_id)
        if kind == "row_live" and live_rows:
            continue
        if kind != "row_send" or NEW_SENDS not in rules:
            found.add(kind)
            continue
        if said is None:
            said = _said_before(table, old, carry, boundary_seq, source_file, backend_session_id)
        text = str(row[table.at["text"]] or "")
        # A row that begins with the delivery notice decides whether an old
        # copy of a prompt is stripped of its notice: said before, always.
        if text.lstrip().startswith(DELIVERY_CONTEXT_OPEN) or _norm_text(text) in said:
            found.add("send_said_before")
    if live_rows:
        replied = None
        trace, ident = table.at.get("trace_id"), table.at.get("message_id")
        for row in table.live(backend_session_id):
            if recorded["live"].get(str(row[ident])) == _digest((table.names, row)):
                continue        # as it was when the frontier was published
            if not row[trace]:
                continue        # no reply's batch can name it
            if replied is None:
                replied = _reply_traces(table, old, boundary_seq, source_file, backend_session_id)
            if row[trace] in replied:
                found.add("live_row_of_old_reply")
    turn_values = _turn_values(turns)
    if _digest(turn_values) != recorded["turns"]:
        if LATER_TURNS not in rules or recorded["early"] is None:
            found.add("turn_rows")      # no rule, or a checkpoint that recorded no time
        else:
            if _early(turn_values, carry) != recorded["early"]:
                found.add("turn_before_boundary")
            # Every trace an old turn asks about is on a row the session held
            # when the frontier was published: the rows at or below the mark,
            # once they are known to be those rows.
            trace = table.at.get("trace_id")
            if rows_stand and trace is not None and _ends(
                    turn_values, {row[trace] for row in old if row[trace]}) != recorded["ends"]:
                found.add("turn_of_old_row")
    if _digest(tuple(user)) != recorded["user"] and not (
            NEW_SENDS in rules and carry.get("initial_provenance_used") is False):
        # The conversation's latest user row is read by replies that come
        # before any user turn of the transcript (a call opening first). The
        # checkpoint records whether there are such replies before the
        # boundary; when it says there are none, the row is never read.
        found.add("latest_user_row")
    if _oracle(oracle) != recorded["oracle"]:
        found.add("oracle_rows")
    return ",".join(sorted(found))


def _reply_traces(table, old, boundary_seq, source_file, backend_session_id) -> set[str]:
    """The traces of the replies stored before the boundary: the turns a full
    replay's batch names and a tail's does not."""
    at = table.at
    session, source, role, seq, trace = (at[name] for name in (
        "backend_session_id", "source_file", "role", "seq", "trace_id"))
    return {row[trace] for row in old
            if row[trace] and row[session] == backend_session_id and row[source] == source_file
            and row[role] == "assistant" and int(row[seq]) < boundary_seq}


def _said_before(table, old, carry, boundary_seq, source_file, backend_session_id) -> set[str]:
    """The words of every user turn before the boundary, as the linker keys
    them: a turn either was linked to a /send row (the carry lists those) or
    is stored as a transcript row with the text it was looked up by."""
    linked = set(carry.get("consumed_client_ids") or ())
    at = table.at
    ident, session, source, role, seq, text = (at[name] for name in (
        "message_id", "backend_session_id", "source_file", "role", "seq", "text"))
    return {_norm_text(row[text]) for row in old
            if row[session] == backend_session_id and (
                row[ident] in linked
                or (row[source] == source_file and row[role] == "user"
                    and int(row[seq]) < boundary_seq))}
