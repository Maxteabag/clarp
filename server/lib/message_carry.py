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

A coordination turn the user's word made visible (lib.coordination.loosen)
gives every row of its trace a new revision and changes nothing else
(message_writes.restamp_trace), so that delta clients fetch the rows they had
left out. Such a row is above the mark and missing from the old digests. The
record keeps the revision of every row of the newest turns (``restamp``); a
row of those that has moved above the mark is put back at its recorded
revision before anything is compared. If anything but the revision changed,
the digests still say so. Nothing a replay decides reads an old row's
revision: the latest user row breaks ties by it and is checked by itself.

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

from .message_import_common import _PARKED_SEQ_BASE, _norm_text
from .message_text import DELIVERY_CONTEXT_OPEN, _related, _strip_voice_markup
from .message_turns import _SETTLED_REACH_MS, _START_SLACK_MS, _UNSETTLED_REACH_MS, iso_ms

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
# linked to it: its words were not said before, or they were and the row
# cannot be any old turn's. The linker (message_import_rows._own_turns_row)
# takes a row for a copy of a prompt only when it was sent for the turn
# running at the copy's time, or no later than _START_SLACK_MS after the copy;
# a retry's echo (_is_retry_echo) decides alike with or without a row sent
# more than a second after it. So a row sent more than _START_SLACK_MS after
# every turn before the boundary (``prefix_latest_ms``), for a trace no old
# row carries and no turn that began by then has, is never an old turn's.
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
# A turn that was running when the frontier was published may have settled.
# A settle changes two things. The turn row: settled_at and outcome are set,
# once (turn_lifecycle). And message rows: the turn's summary is stamped on
# its last row, and an interrupted turn's unfinished tools read
# 'interrupted' (message_writes.stamp_turn), under new revisions.
#
# What a turn before the boundary reads of a turn row (message_writes,
# message_import_rows, message_turns), all of it:
# - by time, which turn was running at its timestamp: the latest turn that
#   began up to _START_SLACK_MS after it (a reply: by it), if the timestamp
#   is within that turn's reach, which is its end + _SETTLED_REACH_MS once it
#   has ended and started_at + _UNSETTLED_REACH_MS before; and, for a
#   prompt's copy linked to a client row by its words, whether the turn a row
#   was sent for had ended before the copy. A turn's end is one definition,
#   TurnIndex.ended_at: the latest settle among its rows, else the start of
#   the agent's next turn of another trace. A settle moves the reach and ends
#   the turn, and a later turn's start ends one that never settled: those are
#   the only ways it can change which turn an old row belongs to. A later
#   turn begins after every old row; a settle cannot change one when every
#   old row lies within both reaches and before the settle, or when the turn
#   began after them all;
# - whether a trace is a turn at all, which a settle does not change;
# - the outcome, when it writes a row: the unfinished tools of an
#   interrupted turn are written as 'interrupted'. A full import writes the
#   rows before the boundary too, a tail does not: so an interrupted turn
#   with a transcript row before the boundary is not let through. (An import
#   stamps a turn's summary only when it changed a row of the turn or the
#   turn is the newest, message_writes._store_transcript_turns_txn; that is
#   the same for both once the rows before the boundary are left alone.)
#
# So: a turn row whose settled_at is above every settled_at the record was
# published with, and which is the recorded row once put back to unsettled,
# is let through when its reach still covers the old turns. And the rows it
# re-stamped are let through where a tail rewrites them anyway: transcript
# rows of this file from the boundary on, written since the record's base,
# that differ in nothing but the stamped columns. Everything else about a
# turn row or a message row is refused as before. Needs LATER_TURNS. Off
# unless switched on.
SETTLED_TURNS = "carry_settled_turns"
# LIVE_ROWS refuses a changed live row when a reply before the boundary
# carries its trace, whatever the words. What a full replay does with such a
# row is narrower. It removes a leftover live row only when a reply of the
# row's own trace in its batch has words related to the row's
# (message_writes._finish_import), and a reply takes a live row over only
# when it is of the reply's trace and their words are related
# (message_import_rows._streamed_twin); related is message_text._related, one
# text beginning with the other. A tail's batch is the replies from the
# boundary on. So the two can differ only when a reply BEFORE the boundary
# has the row's trace and related words, and with this rule only that is
# refused.
#
# The rows do not hold those words. A reply is stored as the automation left
# it (an acknowledgement replaced by a notice, a dream's marker cut off), and
# the replay compares what the transcript said. So the import that chooses
# the boundary keeps the words itself, in the store carry it captures there
# (``reply_words``): for the newest turns before the boundary, each reply's
# words as its batch names them and as they were stored when the two differ.
# A trace it did not keep (an older turn, a turn with too many words, a carry
# captured while the switch was off) is refused as before. It happens when a
# running turn spans the boundary: a context compaction leaves a user record
# inside the turn, and the next full import puts the boundary there (issue
# 352). Needs LIVE_ROWS. Off unless switched on.
LIVE_WORDS = "carry_live_rows_by_words"
RULES = (SESSION_ROWS, LATER_TURNS, NEW_SENDS, LIVE_ROWS, SETTLED_TURNS, LIVE_WORDS)
# How many of the newest turns before the boundary have their replies' words
# kept, and how many bytes of words at most, counted as the checkpoint stores
# them (``stored_size``): the carry is written whole whenever it changes.
_WORDS_TRACES = 3
_WORDS_BYTES = 65536

# The kinds of message row, by where the row is now: one digest each, and the
# word a refusal names it by. ``row_tail`` is a transcript row of this file
# from the boundary on (below the band of parked rows), told apart only
# among the rows written since the record's base; a refusal names it
# ``row_transcript`` like any other.
KINDS = ("row_send", "row_live", "row_final", "row_transcript", "row_other", "row_other_session",
         "row_tail")
_SOURCE_KINDS = (("client:", "row_send"), ("live:", "row_live"), ("final:", "row_final"))
_TURN_COLUMNS = ("trace_id", "started_at", "settled_at", "outcome")
# What stamp_turn writes on a row, and the two that every write moves.
_STAMPED = frozenset({"tools_json", "display_cells_json", "turn_json", "revision", "updated_at"})
_VERSION = 3
# The rows of this many of the newest turns keep their revisions in the record
# (``restamp``), at most this many rows; beyond that none are kept, and a
# restamped row is refused as before.
_RESTAMP_TURNS = 2
_RESTAMP_ROWS = 256
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

    def kind(self, row, source_file, backend_session_id, boundary_seq=None) -> str:
        found = _kind_of(row[self.at["source_file"]], row[self.at["backend_session_id"]],
                         source_file, backend_session_id)
        if found == "row_transcript" and boundary_seq is not None \
                and self._from_boundary(row, boundary_seq):
            return "row_tail"
        return found

    def _from_boundary(self, row, boundary_seq) -> bool:
        seq = row[self.at["seq"]]
        return isinstance(seq, int) and boundary_seq <= seq < _PARKED_SEQ_BASE

    def unstamped(self, rows) -> str:
        """A digest of ``rows`` without what a turn's settling stamps on them."""
        keep = [position for position, name in enumerate(self.names) if name not in _STAMPED]
        return _digest((tuple(self.names[position] for position in keep),
                        [tuple(row[position] for position in keep) for row in rows]))

    def live(self, backend_session_id) -> list:
        """The rows of this session a reply streams into, as they are now."""
        source, session = self.at.get("source_file"), self.at.get("backend_session_id")
        if source is None:
            return []
        return [row for row in self.values
                if row[session] == backend_session_id and str(row[source] or "").startswith("live:")]

    def digests(self, values, source_file, backend_session_id, boundary_seq=None) -> dict[str, str]:
        """One digest per kind, over every column of every row of that kind.
        With ``boundary_seq`` the transcript rows from it on are a kind of
        their own (``row_tail``)."""
        return self.hashed(self.grouped(values, source_file, backend_session_id, boundary_seq))

    def hashed(self, groups: dict[str, list]) -> dict[str, str]:
        return {kind: _digest((self.names, found)) for kind, found in groups.items()}

    def grouped(self, values, source_file, backend_session_id, boundary_seq=None) -> dict[str, list]:
        """The rows by kind, as ``digests`` hashes them."""
        by_kind: dict[str, list] = {kind: [] for kind in KINDS}
        if values:
            source, session = self.at["source_file"], self.at["backend_session_id"]
            transcript, tail = by_kind["row_transcript"].append, by_kind["row_tail"].append
            for row in values:
                if row[session] == backend_session_id and row[source] == source_file:
                    if boundary_seq is not None and self._from_boundary(row, boundary_seq):
                        tail(row)
                    else:
                        transcript(row)     # nearly every row of a long conversation
                else:
                    by_kind[_kind_of(row[source], row[session], source_file,
                                     backend_session_id)].append(row)
        return by_kind


def _counted(groups: dict[str, list]) -> dict[str, int]:
    return {kind: len(found) for kind, found in groups.items()}


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


def _settled_mark(turns: list[tuple]) -> int:
    """The highest settled_at among the turn rows: a row above it settled since."""
    return max((row[2] for row in turns if isinstance(row[2], int)), default=0)


def _unsettled(turns: list[tuple], mark: int) -> tuple[list[tuple], list[tuple]]:
    """The turn rows with those that settled after ``mark`` put back to
    unsettled, and those rows as they are. When the first hash to what the
    record was published with, the rows differ from the recorded ones in
    nothing but that: a row that was unsettled then has settled."""
    restored, settled = [], []
    for row in turns:
        if isinstance(row[2], int) and row[2] > mark:
            settled.append(row)
            row = (row[0], row[1], None, None)
        restored.append(row)
    return restored, settled


def _reach_covers(row: tuple, carry) -> bool:
    """Whether a turn that has settled is, for every turn before the
    boundary, the turn it was while it ran. It holds the old turns within
    its reach, and its reach moved from started_at + _UNSETTLED_REACH_MS to
    settled_at + _SETTLED_REACH_MS: no old turn gains or loses it when the
    last of them lies within both. Nor does an old row it held as the turn
    still running at its time, when it settled at or after the last of them.
    (A turn that began after them all, and so holds none, ends after them
    too: the same test passes it.)"""
    latest = (carry or {}).get("prefix_latest_ms")
    if latest is None or not isinstance(row[1], int):
        return False
    return int(latest) <= min(int(row[1]) + _UNSETTLED_REACH_MS, int(row[2]) + _SETTLED_REACH_MS,
                              int(row[2]))


def _oracle(oracle_rows) -> str:
    """'' for an agent with no Oracle history (a frontier is kept for no other)."""
    if not oracle_rows:
        return ""
    table = _Rows(oracle_rows, key="delegation_id")
    return _digest((table.names, table.values))


def record(raw_inputs, *, carry=None, source_file: str = "", backend_session_id: str = "",
           session_rows: bool = False, witness=None, boundary_seq: int | None = None) -> dict | None:
    """What a frontier keeps of the rows it is published with (the five
    groups of ``_prepare_import_prelude`` with ``raw_only``). None when the
    message rows were not read. ``boundary_seq`` is the boundary of the
    checkpoint it is published with.

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
    base, digests, counts, since = mark, None, None, []
    if witness is not None and witness[0] <= mark:
        kept, since = table.split(witness[0])
        if kept == witness[1]:
            groups = table.grouped(since, source_file, backend_session_id, boundary_seq)
            base, digests = witness[0], {"base": witness[2], "since": table.hashed(groups)}
            counts = {"base": witness[3], "since": _counted(groups)} \
                if len(witness) > 3 and witness[3] is not None else None
        else:
            since = []
    if digests is None:
        # Every row is hashed. The base is the mark, or just below the oldest
        # transcript row from the boundary on: a turn's settling stamps such
        # rows, and SETTLED_TURNS can only let a stamp through on a row that
        # is not sealed in the base's one digest for its kind.
        if boundary_seq is not None and revision is not None:
            base = min([base] + [int(row[revision] or 0) - 1 for row in table.values if table.kind(
                row, source_file, backend_session_id, boundary_seq) == "row_tail"])
        kept, since = table.split(base)
        kept_groups = table.grouped(kept, source_file, backend_session_id)
        since_groups = table.grouped(since, source_file, backend_session_id, boundary_seq)
        digests = {"base": table.hashed(kept_groups), "since": table.hashed(since_groups)}
        counts = {"base": _counted(kept_groups), "since": _counted(since_groups)}
    turn_values = _turn_values(turns)
    trace = table.at.get("trace_id")
    ident = table.at.get("message_id")
    return {
        "version": _VERSION,
        "scope": "session" if session_rows else "agent",
        "mark": mark,
        "base": base,
        # Where ``row_tail`` begins among the rows since the base (None: nowhere).
        "boundary": boundary_seq,
        "rows": digests,
        # How many rows of each kind those digests are over: only to say, when
        # a kind is refused, how its rows changed (``changed``'s ``why``).
        "counts": counts,
        # Each live row of this session by itself (there are few): LIVE_ROWS
        # asks only those that are not as they were.
        "live": {str(row[ident]): _digest((table.names, row))
                 for row in table.live(backend_session_id)},
        # The transcript rows from the boundary on, written since the base,
        # without what a turn's settling stamps on them (SETTLED_TURNS).
        "tail": table.unstamped(
            [row for row in since
             if table.kind(row, source_file, backend_session_id, boundary_seq) == "row_tail"]
            if boundary_seq is not None else []),
        "turns": _digest(turn_values),
        # A turn row whose settled_at is above this settled since.
        "settled": _settled_mark(turn_values),
        "early": _early(turn_values, carry),
        "ends": _ends(turn_values, {row[trace] for row in table.values if row[trace]}
                      if trace is not None else ()),
        "user": _digest(tuple(user)),
        "oracle": _oracle(oracle),
        "restamp": _restampable(table, turn_values),
    }


def _restampable(table: _Rows, turn_values: list[tuple]) -> dict[str, int]:
    """{message_id: revision} of the rows of the newest turns: the turns a
    coordination turn made visible can be (a running turn, or the message being
    recorded). Empty when there are more rows than _RESTAMP_ROWS."""
    trace, ident, revision = (table.at.get(name) for name in ("trace_id", "message_id", "revision"))
    if trace is None or ident is None or revision is None:
        return {}
    newest = {row[0] for row in turn_values[-_RESTAMP_TURNS:] if row[0]}
    if not newest:
        return {}
    rows = [row for row in table.values if row[trace] in newest]
    if len(rows) > _RESTAMP_ROWS:
        return {}
    return {str(row[ident]): int(row[revision] or 0) for row in rows}


def _put_back(table: _Rows, recorded: dict) -> set[str]:
    """Put every row of the record's ``restamp`` that has moved above the mark
    back at its recorded revision, in ``table``. The ids put back."""
    kept = recorded.get("restamp") or {}
    ident, revision = table.at.get("message_id"), table.at.get("revision")
    if not kept or ident is None or revision is None:
        return set()
    moved: set[str] = set()
    for index, row in enumerate(table.values):
        was = kept.get(str(row[ident]))
        if was is not None and int(row[revision] or 0) > recorded["mark"]:
            table.values[index] = row[:revision] + (was,) + row[revision + 1:]
            moved.add(str(row[ident]))
    return moved


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
            or not isinstance(recorded.get("settled"), int)
            or not isinstance(recorded.get("boundary"), (int, type(None)))
            or not isinstance(recorded.get("tail"), str)
            or set(recorded["rows"]) != {"base", "since"}
            or any(not isinstance(part, dict) or set(part) != set(KINDS)
                   for part in recorded["rows"].values())):
        return None
    restamp = recorded.get("restamp")
    if not isinstance(restamp, dict) or not all(
            isinstance(key, str) and isinstance(value, int) for key, value in restamp.items()):
        recorded["restamp"] = {}     # a record written before it was kept, or not one
    return recorded


def changed(recorded, current, *, rules=frozenset(), carry=None, boundary_seq: int = 0,
            source_file: str = "", backend_session_id: str = "",
            session_rows: bool = False, witness: dict | None = None,
            pending_tail: dict | None = None, accepted_tail: dict | None = None,
            why: dict | None = None) -> str:
    """'' when the turns before the boundary would be replayed as they stand.
    Otherwise what stands in the way: fixed words, comma separated, for the
    log (no ids, no text).

    ``recorded`` is the record the frontier was published with, ``current``
    the rows now; ``carry`` is the checkpoint's store carry (the state at the
    boundary), ``boundary_seq`` the first turn the tail imports. ``witness``,
    when given, receives under "base" what ``record`` can build on.

    After a tail whose publication was refused by a foreign commit, the store
    holds the previous frontier while the refused tail's rows (transcript rows
    of this file from the boundary on) stand above its mark. With
    ``pending_tail`` (a dict) those rows are not refused: their ids and the
    digest of their unstamped values are put there, for the caller to check
    against the tail's plan. With ``accepted_tail`` (that dict, once the plan
    has reproduced every one of them line for line) they pass only while they
    are still exactly those rows.

    ``why``, when given, receives under "how" how the rows of each refused
    kind changed, for the log (``_how``)."""
    if recorded is None or current[3] is None:
        return "not_recorded"
    if recorded["scope"] != ("session" if session_rows else "agent"):
        return "rows_scope"       # carry_session_rows was switched since
    carry = carry or {}
    turns, _sends, user, rows, oracle = current
    found: set[str] = set()
    table = _Rows(rows)
    # Rows a coordination turn's restamp moved above the mark go back to their
    # recorded revision: then they are judged, like every old row, by the digests.
    put_back = _put_back(table, recorded)
    base, since, new = table.split(recorded["base"], recorded["mark"])
    # Every row the frontier was published with is at or below the mark. They
    # are all there and unchanged exactly when each kind hashes as recorded,
    # in both parts: up to the base, and above it.
    live_rows = LIVE_ROWS in rules
    settled_turns = SETTLED_TURNS in rules and LATER_TURNS in rules
    tail_differs = False
    # The kinds are told apart as the record told them: by its own boundary.
    tail_from = recorded["boundary"]
    counted = dict.fromkeys(KINDS, 0)
    refused_kinds: set[str] = set()
    for part, values in (("base", base), ("since", since)):
        groups = table.grouped(values, source_file, backend_session_id,
                               tail_from if part == "since" else None)
        now = table.hashed(groups)
        for kind, found_rows in groups.items():
            counted[kind] += len(found_rows)
        differing = [kind for kind in KINDS if now[kind] != recorded["rows"][part][kind]]
        refused_kinds.update(kind for kind in differing
                             if kind != "row_tail" and not (live_rows and kind == "row_live"))
        # Under LIVE_ROWS the live rows are asked one by one below; the rows
        # from the boundary on are asked together, next.
        tail_differs = tail_differs or "row_tail" in differing
        found.update(kind for kind in differing
                     if kind != "row_tail" and not (live_rows and kind == "row_live"))
        if part == "base" and not differing and witness is not None:
            witness["base"] = (recorded["base"], base, recorded["rows"]["base"],
                               (recorded.get("counts") or {}).get("base"))
    # The transcript rows from the boundary on, written since the base: a
    # tail writes them again from the transcript, as a full import does.
    restamped = [row for row in new
                 if table.kind(row, source_file, backend_session_id, tail_from) == "row_tail"]
    if tail_differs or restamped:
        tail = [row for row in since
                if table.kind(row, source_file, backend_session_id, tail_from) == "row_tail"]
        # They are the recorded rows in everything but what a turn's settling
        # stamps (sorted by id again: the two parts are each in that order).
        ident = table.at["message_id"]
        if not settled_turns or table.unstamped(
                sorted(tail + restamped, key=operator.itemgetter(ident))) != recorded["tail"]:
            standing = sorted(tail + restamped, key=operator.itemgetter(ident))
            ids = frozenset(str(row[ident]) for row in standing)
            digest = table.unstamped(standing)
            if accepted_tail is not None and accepted_tail.get("ids") == ids \
                    and accepted_tail.get("digest") == digest:
                pass        # the tail's plan reproduces each of them as it stands
            elif pending_tail is not None:
                pending_tail.update(ids=ids, digest=digest)
            else:
                found.add("row_transcript")
    rows_stand = not found
    # The old rows are both parts. No turn before the boundary has a row in
    # the second part, but the traces the record was published with are the
    # traces of both: asked of the first part alone, a turn row that changed
    # would be refused for every conversation that has rows since its base.
    # A row that was only re-stamped is above the mark now and is still one
    # of them.
    old = base + since if since else base
    if restamped and rows_stand:
        old = old + restamped
    said = early_traces = None
    for row in new:
        kind = table.kind(row, source_file, backend_session_id, tail_from)
        if kind == "row_live" and live_rows:
            continue
        if kind == "row_tail":
            continue            # asked above
        if kind != "row_send" or NEW_SENDS not in rules:
            found.add(kind)
            continue
        if said is None:
            said = _said_before(table, old, carry, boundary_seq, source_file, backend_session_id)
        text = str(row[table.at["text"]] or "")
        # A row that begins with the delivery notice decides whether an old
        # copy of a prompt is stripped of its notice: said before, always.
        if text.lstrip().startswith(DELIVERY_CONTEXT_OPEN):
            found.add("send_said_before")
        elif _norm_text(text) in said:
            if early_traces is None:
                early_traces = _early_traces(table, old, turns, carry)
            if not _sent_after_old_turns(table, row, carry, early_traces):
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
            if row[trace] not in replied:
                continue
            # The words of those replies, if the carry kept them for this turn.
            kept = carry.get("reply_words") if LIVE_WORDS in rules else None
            words = kept.get(row[trace]) if isinstance(kept, dict) else None
            if words is None:
                found.add("live_row_of_old_reply")
                continue
            showing = _strip_voice_markup(row[table.at["text"]])
            if any(_related(said, showing) for said in words):
                # Its own word: how often the trace alone still refuses (a
                # turn not kept) and how often the words do can be counted.
                found.add("live_row_shows_old_reply")
    turn_values = _turn_values(turns)
    settled: list[tuple] = []
    if _digest(turn_values) != recorded["turns"]:
        if LATER_TURNS not in rules or recorded["early"] is None:
            found.add("turn_rows")      # no rule, or a checkpoint that recorded no time
        else:
            if settled_turns:
                # Compared as they were before they settled; that they then
                # are the recorded rows is what proves nothing else changed.
                turn_values, settled = _unsettled(turn_values, recorded["settled"])
            if _early(turn_values, carry) != recorded["early"]:
                found.add("turn_before_boundary")
            # Every trace an old turn asks about is on a row the session held
            # when the frontier was published: the rows at or below the mark,
            # once they are known to be those rows.
            trace = table.at.get("trace_id")
            if rows_stand and trace is not None and _ends(
                    turn_values, {row[trace] for row in old if row[trace]}) != recorded["ends"]:
                found.add("turn_of_old_row")
            if not all(_reach_covers(row, carry) for row in settled):
                found.add("turn_settled_out_of_reach")
            cut = {row[0] for row in settled if row[3] == "interrupted" and row[0]}
            if cut and rows_stand and trace is not None and any(
                    row[trace] in cut and table.kind(
                        row, source_file, backend_session_id, boundary_seq) == "row_transcript"
                    for row in old):
                # Its unfinished tools before the boundary: only a full import writes them.
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
    if why is not None and refused_kinds:
        why["how"] = _how(table, recorded, refused_kinds, counted, new, turns,
                          source_file, backend_session_id, tail_from, put_back)
    return ",".join(sorted(found))


def _how(table, recorded, kinds, counted, new, turns, source_file, backend_session_id,
         tail_from, put_back=frozenset()) -> str:
    """For the log: how the old rows of each refused kind changed, told by their
    count against the record's (no ids, no text). ``in_place``: as many rows
    as recorded, at or below the mark, one of them different. ``rewritten``:
    fewer, and rows of that kind above the mark to account for them;
    ``rewritten_settled`` when every one of those is of a turn that settled
    since. ``removed``: fewer, with nothing above the mark to account for them.
    ``old_revision``: more than recorded. ``uncounted``: a record without counts.
    A row put back at its recorded revision (``_put_back``) moved above the
    mark: it counts among the rows above it."""
    counts = recorded.get("counts")
    settled = {str(row[0]) for row in _turn_values(turns)
               if row[0] and isinstance(row[2], int) and row[2] > recorded["settled"]}
    trace, ident = table.at.get("trace_id"), table.at.get("message_id")
    returned = [row for row in table.values if str(row[ident]) in put_back] \
        if put_back and ident is not None else []
    words = []
    for kind in sorted(kinds):
        if not isinstance(counts, dict) or not all(
                isinstance(part, dict) and isinstance(part.get(kind), int) for part in counts.values()):
            words.append(f"{kind}:uncounted")
            continue
        was = sum(part[kind] for part in counts.values())
        moved = [row for row in returned if table.kind(row, source_file, backend_session_id) == kind]
        now = counted[kind] - len(moved)
        above = moved + [row for row in new if table.kind(
            row, source_file, backend_session_id, tail_from) == kind]
        if now == was:
            how = "in_place"
        elif now > was:
            how = "old_revision"
        elif now + len(above) >= was:
            how = "rewritten_settled" if trace is not None and above and all(
                str(row[trace] or "") in settled for row in above) else "rewritten"
        else:
            how = "removed"
        words.append(f"{kind}:{how}")
    return ",".join(words)


def _early_traces(table, old, turns, carry) -> set[str]:
    """The traces an old turn's copy can be linked by: every old row's, and
    every turn's that began by _START_SLACK_MS after the last turn before the
    boundary (the turns running_at can name for an old time)."""
    trace = table.at.get("trace_id")
    found = {str(row[trace]) for row in old if row[trace]} if trace is not None else set()
    latest = (carry or {}).get("prefix_latest_ms")
    if latest is not None:
        limit = int(latest) + _START_SLACK_MS
        found.update(str(row[0]) for row in _turn_values(turns)
                     if row[0] and isinstance(row[1], int) and row[1] <= limit)
    return found


def _sent_after_old_turns(table, row, carry, early_traces) -> bool:
    """Whether a new /send row was sent too late for any old turn's copy to be
    linked to it, for a trace none of them can name (NEW_SENDS)."""
    latest = (carry or {}).get("prefix_latest_ms")
    at = table.at.get("timestamp")
    if latest is None or at is None:
        return False
    sent = iso_ms(row[at])
    if sent is None or sent <= int(latest) + _START_SLACK_MS:
        return False
    trace = table.at.get("trace_id")
    return trace is None or not row[trace] or str(row[trace]) not in early_traces


def stored_size(text: str) -> int:
    """The bytes one reply's words take in the stored carry. The checkpoint
    writes its carry as JSON in ASCII (transcript_incremental.write_checkpoint),
    where a character that is not ASCII takes six bytes and one outside the
    basic plane twelve: 60,152 such characters, counted as characters, made a
    carry of 360 KB under a limit of 64 KB."""
    return len(json.dumps(text))


def reply_words(said) -> dict[str, list[str]]:
    """What the store carry keeps of the replies before the boundary, for
    LIVE_WORDS. ``said`` is (trace, words) for every reply so far, in the
    transcript's order, the trace as the import's batch names it and words ""
    for a reply that has none. {trace: its replies' words} for the newest
    ``_WORDS_TRACES`` turns, newest first, while their words fit in
    ``_WORDS_BYTES`` as stored: a turn whose words do not fit is left out,
    and so is every turn older than it."""
    newest: list[str] = []
    for trace, _words in reversed(said):
        if trace and trace not in newest:
            newest.append(trace)
            if len(newest) == _WORDS_TRACES:
                break
    kept: dict[str, list[str]] = {}
    left = _WORDS_BYTES
    for trace in newest:
        words = sorted({words for own, words in said if own == trace and words})
        left -= sum(stored_size(text) for text in words)
        if left < 0:
            break
        kept[trace] = words
    return kept


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
