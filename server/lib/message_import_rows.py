"""Transcript provenance, authority, twin matching and row upserts.

The orchestrator supplies its writer and prepared inputs. Matching, consumption
order and SQL stay unchanged; this module never commits or unwraps the writer.
"""
from __future__ import annotations

import json
from typing import Any, NamedTuple

from .clock import now_ms
from .message_context import _strip_voice_markup
from .message_import_common import (
    _FINAL_TWIN_WINDOW_S,
    _PARKED_SEQ_BASE,
    _client_provenance,
    _client_provenance_rows,
    _message_id,
    _next_revision,
    _parse_timestamp,
    _row_payload,
)


def _park_adopted_occupant(database, agent_id: str, backend_session_id: str,
                           source_file: str, seq: int, msg_id: str) -> bool:
    """Move an adopted reply (its id is not its slot's hash) out of the slot
    another turn is about to take: a record appeared earlier and turns shifted
    up. Its own turn, later in this import, adopts it again in place."""
    occupant = database.execute(
        """SELECT message_id FROM messages
            WHERE agent_id = ? AND backend_session_id = ? AND seq = ?
              AND source_file = ? AND message_id <> ?""",
        (agent_id, backend_session_id, seq, source_file, msg_id)).fetchone()
    if occupant is None or occupant["message_id"] == _message_id(
            agent_id, backend_session_id, source_file, seq):
        return False
    database.execute("UPDATE messages SET seq = ? WHERE message_id = ?",
                     (_PARKED_SEQ_BASE + seq, occupant["message_id"]))
    return True


def _seq_taken(database, agent_id: str, backend_session_id: str, seq: int) -> bool:
    return database.execute(
        "SELECT 1 FROM messages WHERE agent_id = ? AND backend_session_id = ? AND seq = ?",
        (agent_id, backend_session_id, seq)).fetchone() is not None


def _restore_unadopted_parked(database, agent_id: str, backend_session_id: str,
                              source_file: str) -> int:
    """A parked reply no turn re-adopted belongs to a request that left the
    transcript: it goes back to its final:<trace> row, as a fresh import would
    leave it, instead of the stale cleanup deleting a reply the user saw
    (RM-N1c). Its seq is the one a live final row takes (-800000 + the turn's
    id), or the next free one below everything the conversation holds. Without a trace it cannot be named, and the
    stale cleanup takes it as before."""
    restored = 0
    for row in database.execute(
            """SELECT message_id, seq, trace_id FROM messages
                WHERE agent_id = ? AND backend_session_id = ? AND source_file = ?
                  AND seq >= ?""",
            (agent_id, backend_session_id, source_file, _PARKED_SEQ_BASE)).fetchall():
        trace = row["trace_id"] or ""
        if not trace:
            continue
        turn = database.execute(
            """SELECT turn_id FROM turns WHERE agent_id = ? AND trace_id = ?
                ORDER BY turn_id DESC LIMIT 1""", (agent_id, trace)).fetchone()
        seq = -800000 + int(turn["turn_id"]) if turn is not None else None
        if seq is None or _seq_taken(database, agent_id, backend_session_id, seq):
            # Below everything the conversation holds: free by construction,
            # also for a reply an earlier import restored the same way.
            lowest = database.execute(
                "SELECT MIN(seq) FROM messages WHERE agent_id = ? AND backend_session_id = ?",
                (agent_id, backend_session_id)).fetchone()[0]
            seq = min(-_PARKED_SEQ_BASE, int(lowest if lowest is not None else 0)) - 1
        database.execute(
            # As a final row is written: no transcript phase, no empty sender.
            "UPDATE messages SET source_file = ?, seq = ?, revision = ?, phase = NULL,"
            " sender_agent_id = NULLIF(sender_agent_id, '') WHERE message_id = ?",
            (f"final:{trace}", seq, _next_revision(database), row["message_id"]))
        restored += 1
    return restored


def _parked_twin(database, agent_id: str, backend_session_id: str,
                 source_file: str, visible: str, *, exclude: set[str]):
    """(message_id, seq) of the one parked reply with this visible text."""
    found = [
        (row["message_id"], int(row["seq"])) for row in database.execute(
            """SELECT message_id, seq, text FROM messages
                WHERE agent_id = ? AND backend_session_id = ? AND source_file = ?
                  AND role = 'assistant' AND seq >= ?""",
            (agent_id, backend_session_id, source_file, _PARKED_SEQ_BASE))
        if row["message_id"] not in exclude
        and _strip_voice_markup(row["text"]) == visible]
    return found[0] if len(found) == 1 else None


def _shifted_twin(database, agent_id: str, backend_session_id: str,
                  source_file: str, request_trace: str, visible: str, seq: int,
                  *, exclude: set[str]):
    """(message_id, seq) of the one reply to ``request_trace`` that an earlier
    import adopted (its id is not its slot's hash) and that sits at a later
    slot of this transcript, with this visible text; else None. A shift moves
    turns down, so only a later slot means this reply moved (RM-N1)."""
    found = [
        (row["message_id"], int(row["seq"])) for row in database.execute(
            # The existing trace index is required: the competing agent/seq
            # range reads every later transcript row once per assistant turn.
            """SELECT message_id, seq, text FROM messages INDEXED BY idx_messages_trace
                WHERE trace_id = ? AND agent_id = ? AND backend_session_id = ?
                  AND source_file = ? AND role = 'assistant' AND seq > ?""",
            (request_trace, agent_id, backend_session_id, source_file, seq))
        if row["message_id"] not in exclude
        and row["message_id"] != _message_id(agent_id, backend_session_id,
                                             source_file, row["seq"])
        and _strip_voice_markup(row["text"]) == visible]
    return found[0] if len(found) == 1 else None


class _FinalRows:
    """Finalized live rows of one conversation, read once per write batch.

    Every assistant turn of an import looks for its request's final row or a
    twin among them. Asking SQLite each time scanned the whole conversation
    per turn (no index covers ``source_file``): ~33 s of a 45 s import of a
    13k-row transcript, all of it holding the writer lock. Between commits the
    rows change through this import's own deletes and upserts, which
    ``discard`` follows; each commit drops the cache, since other writers
    may run then.
    """

    def __init__(self, database, agent_id: str, backend_session_id: str):
        self._database = database
        self._key = (agent_id, backend_session_id)
        self._rows: list[tuple[str, str, str, Any]] | None = None

    def invalidate(self) -> None:
        self._rows = None

    def discard(self, message_ids) -> None:
        if self._rows is not None:
            gone = set(message_ids)
            self._rows = [row for row in self._rows if row[0] not in gone]

    def rows(self) -> list[tuple[str, str, str, Any]]:
        """(message_id, source_file, visible text, timestamp) per row."""
        if self._rows is None:
            self._rows = [
                (row["message_id"], row["source_file"],
                 _strip_voice_markup(row["text"]), row["timestamp"])
                for row in self._database.execute(
                    """SELECT message_id, source_file, text, timestamp FROM messages
                        WHERE agent_id = ? AND backend_session_id = ? AND role = 'assistant'
                          AND source_file LIKE 'final:%'""",
                    self._key,
                ).fetchall()
            ]
        return self._rows


def _streamed_twin(database, agent_id: str, backend_session_id: str, text: str,
                   *, exclude: set[str], trace: str) -> str:
    """The still-live streamed row of this reply's own turn that it completes.

    Kept rows of several turns can coexist, so only a row of the reply's turn
    qualifies. A reply whose turn is unknown takes none: a missing trace is
    missing identity, not proof of a shared turn. A text match in another turn
    is never taken. Exact text wins over a prefix; among equals the oldest."""
    visible = _strip_voice_markup(text)
    if not visible or not trace:
        return ""
    exact = prefix = elsewhere = ""
    for row in database.execute(
            """SELECT message_id, text, trace_id FROM messages
                WHERE agent_id=? AND backend_session_id=? AND role='assistant'
                  AND source_file LIKE 'live:%'
                ORDER BY timestamp, seq""",
            (agent_id, backend_session_id)).fetchall():
        if row["message_id"] in exclude:
            continue
        streamed = _strip_voice_markup(row["text"])
        if not _related(visible, streamed):
            continue
        if (row["trace_id"] or "") != trace:
            elsewhere = elsewhere or str(row["message_id"])
        elif streamed == visible:
            exact = exact or str(row["message_id"])
        else:
            prefix = prefix or str(row["message_id"])
    if not (exact or prefix) and elsewhere:
        _diagnose("transcriptLiveTwinOtherTurn",
                  f"agent={agent_id} trace={trace} kept={elsewhere}")
    return exact or prefix


def _diagnose(event: str, detail: str) -> None:
    """A diagnostic line that can never fail the import it describes."""
    try:
        from .log import log
        log(event, detail)
    except Exception:  # noqa: BLE001 - an optional diagnostic must not fail the import
        pass


def _related(a: str, b: str) -> bool:
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def _final_twins(final_rows: _FinalRows, *, text: str, timestamp: Any,
                 exclude: set[str]) -> list[str]:
    """Finalized live rows showing `text`, written near `timestamp`."""
    visible = _strip_voice_markup(text)
    if not visible:
        return []
    reply_at = _parse_timestamp(timestamp)
    if reply_at is None:
        return []
    twins = []
    for message_id, _source, row_visible, row_timestamp in final_rows.rows():
        if message_id in exclude or row_visible != visible:
            continue
        written = _parse_timestamp(row_timestamp)
        if written is None:
            continue
        if abs((written - reply_at).total_seconds()) <= _FINAL_TWIN_WINDOW_S:
            twins.append(message_id)
    return twins


def _delete_slot(database, agent_id: str, backend_session_id: str,
                 source_file: str, seq: int) -> int:
    """Clear an import slot, taking the write lock only when it is occupied."""
    if database.execute(
            """SELECT 1 FROM messages WHERE agent_id = ? AND backend_session_id = ?
                  AND source_file = ? AND seq = ? LIMIT 1""",
            (agent_id, backend_session_id, source_file, seq)).fetchone() is None:
        return 0
    return database.execute(
        """DELETE FROM messages
            WHERE agent_id = ? AND backend_session_id = ?
              AND source_file = ? AND seq = ?""",
        (agent_id, backend_session_id, source_file, seq)).rowcount


class _ClientProvenance:
    """User turns the client already recorded durably (keyed by client_msg_id).

    Claude's transcript carries its own copy of each; the import links those
    back to the client rows by text in send order rather than inserting a
    duplicate. ``consumed`` lists the client rows this import linked to
    transcript turns, in match order (the incremental import carries it)."""

    def __init__(self, database, agent_id: str, backend_session_id: str,
                 *, prepared=None):
        self._by_text = prepared if prepared is not None else _client_provenance(
            _client_provenance_rows(database, agent_id, backend_session_id))
        self.consumed: list[str] = []

    def resume(self, consumed_client_ids) -> None:
        """Continue after the rows an earlier import already linked."""
        self.consumed = list(consumed_client_ids)
        consumed = set(self.consumed)
        for key in list(self._by_text):
            remaining = [r for r in self._by_text[key] if r[0] not in consumed]
            if remaining:
                self._by_text[key] = remaining
            else:
                del self._by_text[key]

    def candidates(self, key: str) -> list[tuple[str, str, str, str]]:
        """The unlinked client rows with this text, oldest first."""
        return self._by_text.get(key) or []

    def take(self, key: str, rows: list) -> tuple[str, str, str, str]:
        """Link the oldest of ``rows`` (from ``candidates(key)``)."""
        row = rows.pop(0)
        self.consumed.append(row[0])
        if not rows:
            self._by_text.pop(key, None)
        return row


class _AgyAuthority:
    """agy turns whose text the stream owns (``agy_turn_authority``).

    Each assistant turn of the transcript has an ordinal; a turn inside an
    authority row's range is not imported as a row of its own, and its tool
    calls are collected for the authoritative row (_attach_authority_tools)."""

    def __init__(self):
        self.ordinal = 0
        # Tools of agy turns whose final row the stream owns: trace -> (row, tools, cells).
        self.tools: dict[str, tuple[str, list, list]] = {}
        # A dispatched turn's range stays open until the next Clarp turn binds, so
        # once a later user message appears its tools are complete: assistant turns
        # after that (agy run from a terminal, say) are not its own.
        self.closed: set[str] = set()

    def resume(self, carry: dict[str, Any]) -> None:
        self.ordinal = int(carry["assistant_ordinal"])
        self.tools = {trace: (entry[0], list(entry[1]), list(entry[2]))
                      for trace, entry in carry["authority_tools"].items()}
        self.closed = set(carry["closed_authority"])

    def close_open(self) -> None:
        """A user turn: every range collected so far is complete."""
        self.closed.update(self.tools)

    def lookup(self, database, agent_id: str, backend_session_id: str):
        """The authority row owning the next assistant turn, or None."""
        authority = database.execute(
            """SELECT trace_id, authoritative_message_id FROM agy_turn_authority
                 WHERE agent_id=? AND backend_session_id=?
                   AND assistant_start_ordinal<=?
                   AND (assistant_end_ordinal IS NULL
                        OR ?<assistant_end_ordinal)
                 ORDER BY updated_at DESC LIMIT 1""",
            (agent_id, backend_session_id, self.ordinal,
             self.ordinal),).fetchone()
        self.ordinal += 1
        return authority

    def collect(self, authority, turn: dict[str, Any]) -> None:
        """Carry the turn's tool calls onto its authoritative row."""
        if authority["authoritative_message_id"] and \
                authority["trace_id"] not in self.closed:
            _, tools, cells = self.tools.setdefault(
                authority["trace_id"], (authority["authoritative_message_id"], [], []))
            tools.extend(turn.get("tools") or [])
            cells.extend(turn.get("display_cells") or [])


def _resolve_reply_id(database, *, agent_id: str, backend_session_id: str,
                      source_file: str, seq: int, msg_id: str, text: str,
                      timestamp: Any, request_trace: str, reply_trace: str,
                      final_rows: "_FinalRows", adopted_final_ids: set[str],
                      placed_ids: set[str]) -> tuple[str, bool]:
    """The row a transcript reply without its own id lands on (S5 step 4).

    In order: the slot's occupant, unless another request's adopted reply
    holds it (parked, RM-N1b); this reply's adopted final row moved with its
    shifted turn (RM-N1), or a reply this import parked; the request's single
    final row with the same visible text; the row it streamed into; and the
    finalized live twin of an unlinked request. Adopted ids join
    ``adopted_final_ids``. Returns the id and whether any row was removed or
    parked (the import's replace revision then moves)."""
    changed = False
    # Keep the already visible completion identity when its exact
    # authored request and final text identify this transcript reply.
    # Never deduplicate equal replies belonging to different requests.
    slot = database.execute("SELECT message_id, trace_id FROM messages WHERE agent_id=? AND backend_session_id=? AND source_file=? AND seq=? AND role='assistant'",
        (agent_id, backend_session_id, source_file, seq)).fetchone()
    if (slot and request_trace
            and slot["message_id"] != _message_id(
                agent_id, backend_session_id, source_file, seq)
            and (slot["trace_id"] or "") != request_trace
            and _park_adopted_occupant(database, agent_id, backend_session_id,
                                       source_file, seq, msg_id="")):
        # Another request's adopted reply holds this slot (its turn left
        # the transcript): it never lends its id to this reply (RM-N1b).
        changed = True
        slot = None
    moved = None
    if request_trace and not (
            slot and slot["message_id"] != _message_id(
                agent_id, backend_session_id, source_file, seq)):
        # An earlier import adopted this reply's final row (its
        # source_file is this transcript's now) and the turns have
        # shifted since: it moves with its turn, where a fresh import
        # would place it, and the slot's old occupant goes (RM-N1).
        moved = _shifted_twin(database, agent_id, backend_session_id,
                              source_file, request_trace,
                              _strip_voice_markup(text), seq,
                              exclude=adopted_final_ids | placed_ids)
    elif not slot:
        # A reply this import parked (RM-N1b) whose request could not
        # be linked: found again by its visible text, as a fresh
        # import adopts an unlinked reply's final row.
        moved = _parked_twin(database, agent_id, backend_session_id,
                             source_file, _strip_voice_markup(text),
                             exclude=adopted_final_ids | placed_ids)
    if moved is not None:
        msg_id = moved[0]
        adopted_final_ids.add(msg_id)
        _delete_slot(database, agent_id, backend_session_id, source_file, seq)
        changed = True
        slot = None
    elif slot:
        msg_id = slot["message_id"]
    elif request_trace:
        request_source = "final:" + request_trace
        visible = _strip_voice_markup(text)
        matching = [message_id for message_id, source, row_visible, _ts in final_rows.rows()
                    if source == request_source and message_id not in adopted_final_ids
                    and row_visible == visible]
        if len(matching) == 1:
            msg_id = matching[0]
            adopted_final_ids.add(msg_id)
    if not slot and msg_id not in adopted_final_ids:
        # The row this reply streamed into becomes its durable row,
        # in place: same id, so no client sees it vanish and return.
        streamed = _streamed_twin(database, agent_id, backend_session_id,
                                  text, exclude=adopted_final_ids,
                                  trace=reply_trace)
        if streamed:
            msg_id = streamed
            adopted_final_ids.add(msg_id)
    if msg_id not in adopted_final_ids:
        # Without a request trace (the user turn could not be linked
        # to its client row) the finalized live row was never adopted
        # and stayed beside this reply as a second, identical bubble.
        # Match it by visible text within the same few minutes; a
        # reply already imported sheds an earlier import's twin.
        twins = _final_twins(final_rows,
                             text=text, timestamp=timestamp,
                             exclude=adopted_final_ids | {msg_id})
        if len(twins) == 1 and not slot and not request_trace:
            msg_id = twins[0]
            adopted_final_ids.add(msg_id)
        elif twins and slot:
            removed = database.execute(
                f"DELETE FROM messages WHERE message_id IN ({','.join('?' * len(twins))})",
                twins).rowcount
            final_rows.discard(twins)
            changed = changed or removed > 0
    return msg_id, changed


class _Upserted(NamedTuple):
    timestamp: Any
    tools: list
    display_cells: list
    row_trace: str
    unchanged: bool
    revision: int
    changed: bool          # an adopted occupant was parked out of the slot


def _upsert_row(database, *, agent_id: str, backend_session_id: str,
                source_file: str, seq: int, msg_id: str, turn: dict[str, Any],
                role: str, text: str, origin: str, sender_agent_id: str,
                request_trace: str, phase: str | None, turn_index,
                final_rows: "_FinalRows") -> _Upserted:
    """Write one transcript row at its slot (S5 step 5).

    Its trace is the request's (a reply) or its own, else the turn its time
    falls in, else what the row already had; tool statuses settle by that
    turn's outcome. An identical row keeps its revision and is not rewritten
    unless it moves; a moving row first parks an adopted occupant out of the
    slot (RM-N1b)."""
    from . import message_turns
    changed = False
    timestamp = turn.get("timestamp")
    kind = turn.get("kind")
    tool_name = turn.get("tool_name")
    tools = turn.get("tools") if isinstance(turn.get("tools"), list) else []
    display_cells = (
        turn.get("display_cells")
        if isinstance(turn.get("display_cells"), list)
        else []
    )
    existing = database.execute(
        """SELECT role, timestamp, text, kind, tool_name, tools_json,
                  display_cells_json, updated_at, revision,
                  origin, sender_agent_id, trace_id, phase, source_file, seq
             FROM messages WHERE message_id = ?""",
        (msg_id,),
    ).fetchone()
    row_trace = (request_trace if role == "assistant" else str(turn.get("trace_id") or "")) \
        or turn_index.trace_at(timestamp) or ((existing["trace_id"] or "") if existing else "")
    outcome = turn_index.outcomes.get(row_trace)
    tools = message_turns.settle_statuses(tools, outcome)
    display_cells = message_turns.settle_statuses(display_cells, outcome)
    phase = phase
    tools_json = json.dumps(tools, separators=(",", ":"))
    display_cells_json = json.dumps(display_cells, separators=(",", ":"))
    payload = (
        role, timestamp, text, kind, tool_name,
        tools_json, display_cells_json, origin, sender_agent_id,
    )
    unchanged = existing is not None and _row_payload(existing) == payload \
        and (existing["trace_id"] or "") == row_trace and existing["phase"] == phase
    revision = int(existing["revision"]) if unchanged else _next_revision(database)
    updated_at = int(existing["updated_at"]) if unchanged else now_ms()
    # The upsert of an identical row still rewrites its pages into the WAL;
    # skip it when every column it sets already holds that value.
    rewrite = not unchanged or existing["source_file"] != source_file \
        or existing["seq"] != seq
    if rewrite and _park_adopted_occupant(database, agent_id, backend_session_id,
                                          source_file, seq, msg_id):
        changed = True
    if rewrite:
        database.execute(
            """INSERT INTO messages (
               message_id, agent_id, backend_session_id, source_file, seq,
               role, timestamp, text, kind, tool_name, tools_json,
               display_cells_json, updated_at, revision, origin,
               sender_agent_id, trace_id, phase
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(message_id) DO UPDATE SET
               trace_id = COALESCE(excluded.trace_id, messages.trace_id),
               phase = excluded.phase,
               source_file = excluded.source_file,
               seq = excluded.seq,
               role = excluded.role,
               timestamp = excluded.timestamp,
               text = excluded.text,
               kind = excluded.kind,
               tool_name = excluded.tool_name,
               tools_json = excluded.tools_json,
               display_cells_json = excluded.display_cells_json,
               updated_at = excluded.updated_at,
               revision = excluded.revision,
               origin = excluded.origin,
               sender_agent_id = excluded.sender_agent_id""",
        (msg_id, agent_id, backend_session_id, source_file, seq,
             role, timestamp, text, kind, tool_name, tools_json,
             display_cells_json, updated_at,
             revision, origin, sender_agent_id, row_trace or None, phase),
        )
    if source_file.lower().startswith("final:"):
        final_rows.invalidate()
    else:
        # The row now carries this transcript's source_file, not final:.
        final_rows.discard((msg_id,))
    return _Upserted(timestamp, tools, display_cells, row_trace, unchanged, revision, changed)
