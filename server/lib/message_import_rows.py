"""Transcript provenance, authority, twin matching and row upserts.

The orchestrator supplies its writer and prepared inputs. Matching, consumption
order and SQL stay unchanged; this module never commits or unwraps the writer.
"""
from __future__ import annotations

import json
import os
from typing import Any, NamedTuple

from .clock import now_ms
from .message_text import (_related, _strip_voice_markup, strip_injected_blocks,
                           strip_delivery_context, CALL_OPENING_OPEN)  # noqa: F401 - _related is used from here too
from .message_turns import iso_ms
from .message_import_common import (
    _FINAL_TWIN_WINDOW_S,
    _norm_text,
    _is_retry_echo,
    _sqlite_order,
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


def _parsed_traces(turns, turn_index, provenance, client_traces, *,
                   start_seq=0, current_trace="", unanswered_key=""):
    """Replay provenance selection without SQL or consuming the writer's rows."""
    copied = (_ClientProvenance(None, "", "", prepared={
        key: list(rows) for key, rows in provenance._by_text.items()})
        if provenance is not None else None)
    trace_at = getattr(turn_index, "trace_at", lambda _: "")
    running_at = getattr(turn_index, "running_at", lambda _: ("", None))
    outcomes = getattr(turn_index, "outcomes", {})
    result = {}
    for index, turn in enumerate(turns):
        role = turn.get("role")
        stamp = turn.get("timestamp")
        if role == "assistant":
            if not turn.get("failed"):
                unanswered_key = ""
            trace = current_trace or trace_at(stamp)
        elif role == "user":
            text = str(turn.get("text") or "")
            if not text.lstrip().startswith(CALL_OPENING_OPEN):
                if turn.get("id") is None and copied is not None:
                    text = strip_injected_blocks(text)
                    words = strip_delivery_context(text)
                    if words != text and not copied.candidates(_norm_text(text)):
                        text = words
                    key = _norm_text(text)
                    rows = copied.candidates(key)
                    if key and key == unanswered_key and _is_retry_echo(
                            stamp, rows[0][3] if rows else None):
                        result[start_seq+index] = current_trace or trace_at(stamp)
                        continue
                    if rows:
                        running, began = running_at(stamp)
                        chosen = copied.take(key, rows, _own_turns_row(
                            None, rows, running, began, trace_ids=client_traces))
                        current_trace = client_traces.get(chosen[0]) or ""
                        if running and current_trace not in outcomes:
                            current_trace = running
                        unanswered_key = key
                        result[start_seq+index] = current_trace or trace_at(stamp)
                        continue
                    unanswered_key = ""
                current_trace = str(turn.get("trace_id") or "")
            trace = current_trace or trace_at(stamp)
        else:
            trace = str(turn.get("trace_id") or "") or trace_at(stamp)
        result[start_seq+index] = trace
    return result


class _FinalRows:
    """Server-authored final rows and live candidates, cached per writer hold.

    Every assistant turn of an import looks for its request's final row or a
    twin among them. Asking SQLite each time scanned the whole conversation
    per turn (no index covers ``source_file``): ~33 s of a 45 s import of a
    13k-row transcript, all of it holding the writer lock. Between commits the
    rows change through this import's own deletes and upserts, which
    ``discard`` follows; each commit drops the cache, since other writers
    may run then.
    """

    def __init__(self, database, agent_id: str, backend_session_id: str, *,
                 turns=(), visible_replies=None, start_seq=0, turn_index=None,
                 client_rows=(), provenance=None, current_trace="", unanswered_key=""):
        self._database = database
        self._key = (agent_id, backend_session_id)
        self._rows: list[tuple[str, str, str, Any]] | None = None
        self._live_rows = None
        self._live_traces = set()
        self._live_all = False
        self._total_turns = start_seq + len(turns)
        self._unresolved_peers = {}
        self._trace_replies = {}
        self._last_by_trace = {}
        self._last_unknown = start_seq - 1
        self._last_seq = start_seq + len(turns) - 1
        client_traces = {row.message_id: row.trace_id for row in client_rows
                         if hasattr(row, "trace_id")}
        resolved = _parsed_traces(turns, turn_index, provenance, client_traces,
            start_seq=start_seq, current_trace=current_trace, unanswered_key=unanswered_key)
        group = None
        for index, turn in enumerate(turns):
            trace = resolved[start_seq+index]
            if trace:
                self._last_by_trace[trace] = start_seq + index
            else:
                self._last_unknown = start_seq + index
            role = turn.get("role")
            visible = None
            if role == "assistant":
                visible = (visible_replies[index] if visible_replies is not None
                           else _strip_voice_markup(turn.get("text")))
                if trace and visible:
                    self._trace_replies.setdefault(trace, []).append((start_seq + index, visible))
            if role == "user":
                group = []
            elif role == "assistant" and group is not None:
                if visible:
                    if not trace:
                        group.append((start_seq + index, visible))
                    self._unresolved_peers[start_seq + index] = group
        self._turns = {row["trace_id"]: row for row in turn_index._rows} if turn_index else {}
        self._declined = {}
        self._occupied_adoptions = set()
        self._guard_logged = False
        self._candidate_traces = dict.fromkeys(
            resolved[start_seq+index] for index, turn in enumerate(turns)
            if turn.get("role") == "assistant" and resolved[start_seq+index])

    def invalidate(self) -> None:
        self._rows = None
        self._live_rows = None
        self._turns.clear()
        self._live_traces = set()
        self._live_all = False

    def discard(self, message_ids) -> None:
        gone = set(message_ids)
        if self._rows is not None:
            self._rows = [row for row in self._rows if row[0] not in gone]
        if self._live_rows is not None:
            self._live_rows = [row for row in self._live_rows if row["message_id"] not in gone]

    def live_rows(self, trace):
        """One matching read family per hold; cleanup always reads separately."""
        if self._live_rows is None:
            traces = tuple(dict.fromkeys((*self._candidate_traces, trace)))
            self._live_traces = set(traces)
            self._live_all = len(traces) > max(1, self._total_turns // 5)
            # Same bound, chunks and threshold as _finish_import. The
            # projection carries the ordering keys so the query keeps its
            # existing plan; sorting this small live set preserves main's
            # timestamp/seq candidate order, including SQLite value types.
            sql = """SELECT message_id, text, trace_id, kind, timestamp, seq FROM messages
                       WHERE agent_id = ? AND backend_session_id = ?
                         AND source_file LIKE 'live:%' AND role = 'assistant'"""
            if self._live_all:
                rows = self._database.execute(sql, self._key).fetchall()
            else:
                rows = []
                for start in range(0, len(traces), 32):
                    selected = traces[start:start+32]
                    bounded = sql.replace('FROM messages', 'FROM messages INDEXED BY idx_messages_trace')
                    rows.extend(self._database.execute(
                        bounded + ' AND trace_id IN (' + ','.join('?' for _ in selected) + ')',
                        (*self._key, *selected)).fetchall())
            rows.sort(key=lambda row: (_sqlite_order(row["timestamp"]), _sqlite_order(row["seq"])))
            self._live_rows = [{**dict(row), "visible": _strip_voice_markup(row["text"])}
                               for row in rows]
        if not self._live_all and trace not in self._live_traces:
            self._guard_failure("batch_trace_not_in_read")
            return []
        return [row for row in self._live_rows if (row["trace_id"] or "") == trace]

    def rows(self) -> list[tuple[str, str, str, Any]]:
        """Server-authored final:% rows: id, source, visible text, timestamp."""
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

    def unique_reply(self, row, seq, trace, *, exact=False):
        """A live id identifies exactly one parsed reply of this trace.

        Look ahead through the parsed trace, including steered inputs, before
        adopting an earlier remark. A partial live final can otherwise lend its id to that remark.
        No provider ordinal is guessed from the opaque live id.
        """
        # Known traces span steered prompts. A reply with unresolved ownership
        # in the current prompt group may veto a match but cannot adopt a row.
        unknown = self._unresolved_peers.get(seq)
        known = self._trace_replies.get(trace)
        if known is None and unknown is None:
            if exact:
                self.decline_live(row, trace, seq)
            return not exact
        group = (known or []) + (unknown or [])
        compatible = {position for position, visible in group
                      if (visible == row["visible"] if exact
                          else _related(visible, row["visible"]))}
        # The caller already checked its effective text against this row.
        compatible.add(seq)
        if len(compatible) > 1:
            self.decline_live(row, trace, seq)
            return False
        return True

    def is_last_reply(self, seq, trace):
        # Unknown later records cannot establish that this is the last of
        # its trace. The final parsed record is last without an inference.
        return seq == self._last_seq or (
            self._last_by_trace.get(trace) == seq and self._last_unknown < seq)

    def decline_live(self, row, trace, seq):
        if self.is_last_reply(seq, trace):
            self._declined[row["message_id"]] = (trace, row["visible"])

    def stamp_after_cleanup(self, deleted_stamps, stamp, next_revision):
        """Re-stamp only traces whose visible survivor changed in this import.

        Adoption markers survive writer yields; candidate rows do not. Ordinary
        replay has neither marker and performs no extra reads.
        """
        traces = self._occupied_adoptions | deleted_stamps
        return max((stamp(self._database, self._key[0], trace, next_revision)
                    for trace in sorted(traces)), default=0)

    def keep_declined_live(self, live) -> bool:
        """Only the last reply of a known, still-running turn can wait.

        Ended turns always use ordinary cleanup. This is per-import knowledge,
        with turn state validated in the writer hold that performs cleanup.
        A failure or an obsolete decision never makes an import keep a row.
        """
        try:
            declined = self._declined.get(live["message_id"])
            if declined is None:
                return False
            trace = live["trace_id"] or ""
            if declined != (trace, _strip_voice_markup(live["text"])):
                self._guard_failure("obsolete_decision")
                return False
            if trace not in self._turns:
                # A release invalidates the prelude's turn state. Read only
                # on an actual decline and share the result within this hold.
                self._turns[trace] = self._database.execute(
                    "SELECT settled_at, outcome FROM turns WHERE agent_id=? AND trace_id=? "
                    "ORDER BY started_at DESC LIMIT 1", (self._key[0], trace)).fetchone()
            turn = self._turns[trace]
            if turn is None:
                self._guard_failure("obsolete_decision")
                return False
            return turn["settled_at"] is None and turn["outcome"] is None
        except Exception as exc:  # noqa: BLE001 - uncertain retention must fall back to ordinary cleanup
            self._guard_failure(type(exc).__name__)
            return False

    def _guard_failure(self, reason):
        if not self._guard_logged:
            self._guard_logged = True
            _diagnose("transcriptLiveDeclineGuardFail", f"reason={reason} count=1")


def _streamed_twin(database, agent_id: str, backend_session_id: str, text: str,
                   *, exclude: set[str], trace: str, final_rows: _FinalRows,
                   seq: int, visible: str | None = None, occupied: bool = False) -> str:
    """The still-live streamed row of this reply's own turn that it completes.

    Kept rows of several turns can coexist, so only a row of the reply's turn
    qualifies. A reply whose turn is unknown takes none: a missing trace is
    missing identity, not proof of a shared turn. A text match in another turn
    is never taken. Exact text wins over a prefix; among equals the oldest."""
    if visible is None:
        visible = _strip_voice_markup(text)
    if not visible or not trace:
        return ""
    if occupied and not final_rows.is_last_reply(seq, trace):
        return ""
    candidates = [row for row in final_rows.live_rows(trace)
                  if row["message_id"] not in exclude]
    if occupied:
        # A new streaming reply can begin with an earlier reply's words.
        # Replacing that earlier placeholder would lend its position to a
        # row the live writer is still changing. Only a final exact match
        # can replace an occupied slot, and neither side may be ambiguous.
        matches = []
        for row in candidates:
            if not _related(visible, row["visible"]):
                continue
            if row["kind"] is None and row["visible"] == visible:
                matches.append(row)
            else:
                final_rows.decline_live(row, trace, seq)
        if len(matches) != 1:
            for row in matches:
                final_rows.decline_live(row, trace, seq)
            return ""
        row = matches[0]
        return str(row["message_id"]) if final_rows.unique_reply(
            row, seq, trace, exact=True) else ""
    exact = prefix = ""
    for row in candidates:
        streamed = row["visible"]
        if not _related(visible, streamed):
            continue
        if not final_rows.unique_reply(row, seq, trace):
            continue
        if streamed == visible:
            exact = exact or str(row["message_id"])
        else:
            prefix = prefix or str(row["message_id"])
    return exact or prefix


def _diagnose(event: str, detail: str) -> None:
    """A diagnostic line that can never fail the import it describes."""
    try:
        from .log import log
        log(event, detail)
    except Exception:  # noqa: BLE001 - an optional diagnostic must not fail the import
        # Independent one-shot, content-free fallback. A closed/broken fd or
        # a short write is best-effort loss, never proof of delivery.
        try:
            os.write(2, b"transcriptDiagnosticFallback code=primary_log_failed count=1\n")
        except OSError:
            return


def _final_twins(final_rows: _FinalRows, *, text: str, timestamp: Any,
                 exclude: set[str], visible: str | None = None) -> list[str]:
    """Finalized live rows showing `text`, written near `timestamp`."""
    if visible is None:
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

    def take(self, key: str, rows: list, index: int = 0) -> tuple[str, str, str, str]:
        """Link one of ``rows`` (from ``candidates(key)``): the oldest, unless
        the caller knows which of them the transcript turn belongs to."""
        row = rows.pop(index)
        self.consumed.append(row[0])
        if not rows:
            self._by_text.pop(key, None)
        return row


def _own_turns_row(database, rows: list, trace: str, began: int | None = None, *,
                   trace_ids=None) -> int:
    """Which of several client rows with the same words a transcript turn
    belongs to. ``trace`` and ``began`` are the turn that was running when
    the provider wrote its copy (from the turn index).

    First the row sent for that turn. Else the copy was steered into it, and
    is the first row sent after the turn began. Else the oldest, also when
    no turn was running.

    The oldest is right when every send ran, in order. It is wrong when
    earlier ones never ran: sends removed from the queue or stopped before
    they started keep their rows in the chat. Linked to one of those, the
    reply took that row's trace, was not recognised as the reply its turn
    was streaming, and was stored under a new id beside the live row
    (issue 285: the same words sent three times to an agent that could not
    run, then a fourth time)."""
    if len(rows) < 2 or not trace:
        return 0
    for index, row in enumerate(rows):
        if trace_ids is None:
            sent = database.execute(
                "SELECT trace_id FROM messages WHERE message_id=?", (row[0],)).fetchone()
            sent_trace = (sent["trace_id"] or "") if sent is not None else ""
        else:
            sent_trace = trace_ids.get(row[0]) or ""
        if sent_trace == trace:
            return index
    if began is not None:
        for index, row in enumerate(rows):
            sent_at = iso_ms(row[3])
            if sent_at is not None and sent_at > began:
                return index
    return 0


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
                      placed_ids: set[str], visible: str | None = None) -> tuple[str, bool]:
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
        if visible is None:
            visible = _strip_voice_markup(text)
        moved = _shifted_twin(database, agent_id, backend_session_id,
                              source_file, request_trace,
                              visible, seq,
                              exclude=adopted_final_ids | placed_ids)
    elif not slot:
        # A reply this import parked (RM-N1b) whose request could not
        # be linked: found again by its visible text, as a fresh
        # import adopts an unlinked reply's final row.
        if visible is None:
            visible = _strip_voice_markup(text)
        moved = _parked_twin(database, agent_id, backend_session_id,
                             source_file, visible,
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
        if visible is None:
            visible = _strip_voice_markup(text)
        matching = [message_id for message_id, source, row_visible, _ts in final_rows.rows()
                    if source == request_source and message_id not in adopted_final_ids
                    and row_visible == visible]
        if len(matching) == 1:
            msg_id = matching[0]
            adopted_final_ids.add(msg_id)
    if (not slot or slot["message_id"] == _message_id(
            agent_id, backend_session_id, source_file, seq)) and msg_id not in adopted_final_ids:
        # The row this reply streamed into becomes its durable row,
        # in place: same id, so no client sees it vanish and return.
        if visible is None:
            visible = _strip_voice_markup(text)
        streamed = _streamed_twin(database, agent_id, backend_session_id,
                                  text, exclude=adopted_final_ids | placed_ids,
                                  trace=reply_trace, visible=visible,
                                  final_rows=final_rows, seq=seq, occupied=bool(slot))
        if streamed:
            if slot:
                # This slot was already read: do not add a second point read.
                database.execute("DELETE FROM messages WHERE message_id=?", (slot["message_id"],))
                changed = True
                final_rows._occupied_adoptions.add(reply_trace)
                slot = None
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
                             exclude=adopted_final_ids | {msg_id}, visible=visible)
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
