"""Steer receipts: did a steered message get a final answer in its turn?

A steer (`turn/steer`) folds a user message into the turn Codex is already
running. Whether that message was answered is decided from protocol evidence on
the expected turn, per steered message, keyed by (agent_id, client_msg_id):

- the UserMessage item echoing the message's clientId (its consumption seq);
- AgentMessage items with phase=final_answer after that seq;
- how the turn ended, and whether the reader saw it end (coverage).

`state` holds the evidence outcome and `ack` the steer RPC's answer, an audit
field. Item evidence always beats the ACK, and a terminal state is never
rewritten: every write is a conditional UPDATE, so a replayed event changes
nothing. A steer costs at most four small writes (register, ack, consume,
settle); none per token.

The runtime writes receipts (codex_app_server); the Host only reads them
(`responses_for` for /log, `receipt_for_message` for notifications) and
tolerates a missing table, since a newer Host may run against an older runtime.
Design: docs/protocol.md "Steer receipts".
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import sqlite3
import threading
from typing import Iterator

log = logging.getLogger(__name__)

NON_TERMINAL = ("registered", "acked", "consumed", "awaiting_ack")
# /log names only these two; answered rows and rows without a receipt stay unchanged.
MARKED = ("no_final_response", "unknown")

_OPEN = "('registered','acked','consumed','awaiting_ack')"
_UNSETTLED = "('registered','acked','consumed')"

SCHEMA = """
CREATE TABLE steer_receipts (
    agent_id TEXT NOT NULL,
    client_msg_id TEXT NOT NULL,
    -- A rejected steer falls through to the queue; a later steer of that
    -- message is attempt n+1. Rows are never rewritten; readers use the latest.
    attempt INTEGER NOT NULL DEFAULT 1,
    message_id TEXT NOT NULL,
    prompt_admission_id TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    expected_turn_id TEXT NOT NULL DEFAULT '',
    gen TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    ack TEXT NOT NULL DEFAULT 'pending',
    consumed_seq INTEGER,
    final_seq INTEGER,
    registered_at INTEGER NOT NULL,
    settled_at INTEGER,
    PRIMARY KEY (agent_id, client_msg_id, attempt)
);
CREATE INDEX idx_steer_receipts_open ON steer_receipts(state)
    WHERE state IN ('registered','acked','consumed','awaiting_ack');
CREATE INDEX idx_steer_receipts_message ON steer_receipts(message_id, attempt);
-- A receipt lives exactly as long as its user message, including when older
-- code (which knows nothing of receipts) deletes the message.
CREATE TRIGGER steer_receipts_follow_message AFTER DELETE ON messages
BEGIN DELETE FROM steer_receipts WHERE message_id = OLD.message_id; END;
"""

# After SCHEMA: db_schema imports it while `db` is still loading.
from . import db  # noqa: E402


def decide(consumed_seq: int | None, final_seqs, unphased_seqs, turn_status: str,
           coverage: str, ack: str, no_client_id: bool) -> tuple[str, str]:
    """The receipt's state at (or after) the turn's terminal event.

    `coverage` is "" when the reader saw the whole turn on the bound thread,
    "coverage" for a gap and "rebind" for a thread change; both block every
    evidence outcome. "unbound" (the agent lost its binding mid-turn) is a
    weaker gap: it only keeps absence from being asserted, so it turns
    no_final_response into unknown(coverage) and changes nothing else. `ack`
    is the steer RPC outcome so far: pending, ok, error, mismatch, timeout or
    reader_end."""
    unbound = coverage == "unbound"
    if unbound:
        coverage = ""
    if consumed_seq is not None:
        if coverage:
            return "unknown", coverage
        if turn_status in ("interrupted", "failed"):
            return turn_status, ""
        if any(seq > consumed_seq for seq in final_seqs):
            return "answered", ""
        if any(seq > consumed_seq for seq in unphased_seqs):
            return "unknown", "no_phase"
        return ("unknown", "coverage") if unbound else ("no_final_response", "")
    if ack == "error":
        return "rejected", ""
    if ack == "mismatch":
        return "unknown", "ack_turn_mismatch"
    if ack == "timeout":
        return "unknown", "ack_timeout"
    if ack == "reader_end" or coverage:
        return "unknown", coverage or "coverage"
    if ack == "pending":
        return "awaiting_ack", ""
    if turn_status in ("interrupted", "failed"):
        return turn_status, ""
    if no_client_id:
        return "unknown", "no_client_id"
    return "unknown", "accepted_not_observed"


def ack_outcome(result, expected_turn_id: str) -> str:
    """ok or mismatch for a steer reply. An older Codex answers without a
    turnId; that is still ok, because the outcome stays evidence-driven."""
    turn_id = (result or {}).get("turnId") if isinstance(result, dict) else None
    if not turn_id or turn_id == expected_turn_id:
        return "ok"
    return "mismatch"


def _locked(method):
    def run(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    run.__name__, run.__doc__ = method.__name__, method.__doc__
    return run


class Receipt:
    """One steered message's receipt as its runtime writer sees it."""

    def __init__(self, agent_id: str, client_msg_id: str, *, expected_turn_id: str,
                 attempt: int = 1):
        self.agent_id = agent_id
        self.client_msg_id = client_msg_id
        self.attempt = attempt
        self.expected_turn_id = expected_turn_id
        self.consumed_seq: int | None = None
        self.ack = "pending"
        self.turn_status = ""
        self.no_client_id = False
        # The reader thread consumes and settles; the steer thread applies the ACK.
        self._lock = threading.RLock()

    def _update(self, assignments: str, condition: str, params: tuple) -> bool:
        cur = db.conn().execute(
            f"UPDATE steer_receipts SET {assignments}"
            f" WHERE agent_id=? AND client_msg_id=? AND attempt=? AND {condition}",
            (*params, self.agent_id, self.client_msg_id, self.attempt))
        return cur.rowcount > 0

    @_locked
    def consume(self, seq: int) -> None:
        """The UserMessage item echoing this message arrived; the first wins."""
        if self.consumed_seq is not None:
            return
        self.consumed_seq = seq
        self._update("state='consumed', consumed_seq=?",
                     f"consumed_seq IS NULL AND state IN ('registered','acked')", (seq,))

    @_locked
    def settle_at_terminal(self, *, turn_status: str, final_seqs, unphased_seqs,
                           coverage: str, no_client_id: bool) -> tuple[str, str]:
        self.turn_status = turn_status
        self.no_client_id = no_client_id
        state, reason = decide(self.consumed_seq, final_seqs, unphased_seqs, turn_status,
                               coverage, self.ack, no_client_id)
        # The last final answer of the turn, whatever the outcome: ordering
        # facts for notifications (user_notifications._receipt_disowns).
        final_seq = max(final_seqs, default=None)
        settled_at = None if state in NON_TERMINAL else db.now_ms()
        self._update("state=?, reason=?, final_seq=?, settled_at=?", f"state IN {_UNSETTLED}",
                     (state, reason, final_seq, settled_at))
        return state, reason

    @_locked
    def apply_ack(self, outcome: str) -> bool:
        """Record the steer RPC's outcome. True only when this ACK proves Codex
        never took the message (a definitive rejection with no consumption):
        the caller then runs the existing fall-through. A timeout or reader end
        never falls through, since acceptance cannot be ruled out."""
        first = self.ack == "pending"
        if first and outcome != "reader_end":
            self.ack = outcome
        audit = outcome if outcome != "reader_end" else None
        if self.consumed_seq is not None:
            if outcome in ("error", "mismatch") and first:
                log.info("steer receipt %s/%s: ack_%s_after_consumption",
                         self.agent_id, self.client_msg_id, outcome)
            if first and audit:
                self._update("ack=?", "ack='pending'", (audit,))
            return False
        if self.turn_status:                                  # awaiting_ack
            state, reason = decide(None, [], [], self.turn_status, "", outcome, self.no_client_id)
            allowed = "state='awaiting_ack'"
        elif outcome == "ok":
            state, reason = "acked", ""
            allowed = "state='registered'"
        else:
            state, reason = decide(None, [], [], "", "", outcome, False)
            allowed = "state='registered'"
        if not first and outcome != "reader_end":
            return False
        settled_at = None if state in NON_TERMINAL else db.now_ms()
        if audit:
            changed = self._update("ack=?, state=?, reason=?, settled_at=?",
                                   f"ack='pending' AND {allowed}",
                                   (audit, state, reason, settled_at))
            if not changed:
                self._update("ack=?", "ack='pending'", (audit,))
        else:
            changed = self._update("state=?, reason=?, settled_at=?", allowed,
                                   (state, reason, settled_at))
        return changed and state == "rejected"


def register(agent_id: str, client_msg_id: str, *, prompt_admission_id: str = "",
             trace_id: str = "", thread_id: str, expected_turn_id: str,
             gen: str) -> Receipt | None:
    """Write the receipt before the steer RPC. None when the message already
    has a receipt that is not `rejected`: a second steer sends no RPC. After a
    rejection (the message fell through to the queue) a later steer of it is
    a new delivery and gets the next attempt; the rejected row stays."""
    from .message_writes import _client_message_id
    # One statement: the latest attempt decides, so two writers cannot both win.
    cur = db.conn().execute(
        "INSERT INTO steer_receipts(agent_id, client_msg_id, attempt, message_id,"
        " prompt_admission_id, trace_id, thread_id, expected_turn_id, gen, state,"
        " registered_at)"
        " SELECT ?, ?, COALESCE(MAX(attempt), 0) + 1, ?, ?, ?, ?, ?, ?, 'registered', ?"
        " FROM steer_receipts WHERE agent_id=? AND client_msg_id=?"
        " HAVING COALESCE((SELECT state FROM steer_receipts WHERE agent_id=? AND client_msg_id=?"
        "                  ORDER BY attempt DESC LIMIT 1), 'rejected') = 'rejected'"
        " RETURNING attempt",
        (agent_id, client_msg_id, _client_message_id(client_msg_id),
         prompt_admission_id or "", trace_id or "", thread_id or "", expected_turn_id or "",
         gen, db.now_ms(), agent_id, client_msg_id, agent_id, client_msg_id))
    row = cur.fetchone()
    if row is None:
        return None
    return Receipt(agent_id, client_msg_id, expected_turn_id=expected_turn_id,
                   attempt=int(row[0]))


def _settle_open(where: str, params: tuple) -> list[tuple[str, str]]:
    rows = db.conn().execute(
        f"UPDATE steer_receipts SET state='unknown', reason='coverage', settled_at=?"
        f" WHERE state IN {_OPEN}{where} RETURNING agent_id, message_id",
        (db.now_ms(), *params)).fetchall()
    return [(r[0], r[1]) for r in rows]


def sweep(*, current_gen: str) -> int:
    """A new reader generation cannot vouch for the old one's open receipts:
    settle them all unknown(coverage) in one statement. Terminal rows stay."""
    return len(_settle_open(" AND gen <> ?", (current_gen,)))


def recover(*, current_gen: str | None = None, stream=None) -> int:
    """Settle open receipts no live reader can settle, and show it.

    At runtime boot (`current_gen` None) every open receipt belongs to a dead
    reader: receipts are written only by this process's steer path. With a
    generation, only older generations are swept. Each settled user row gets a
    new revision and its agent's clients a transcript-updated, so the marker
    appears without waiting for a turn."""
    settled = (_settle_open("", ()) if current_gen is None
               else _settle_open(" AND gen <> ?", (current_gen,)))
    if not settled:
        return 0
    from . import agents as agents_db, events, message_writes
    for _agent, message_id in settled:
        message_writes.bump_message_revision(message_id)
    if stream is not None:
        for agent_id in sorted({agent for agent, _ in settled}):
            agent = agents_db.get_by_agent_id(agent_id) or {}
            events.broadcast(stream, events.transcript_updated(
                agent_id=agent_id, session=agent.get("session")))
    return len(settled)


def get(agent_id: str, client_msg_id: str) -> dict | None:
    row = db.conn().execute(
        "SELECT * FROM steer_receipts WHERE agent_id=? AND client_msg_id=?"
        " ORDER BY attempt DESC LIMIT 1", (agent_id, client_msg_id)).fetchone()
    return dict(row) if row is not None else None


def responses_for(message_ids) -> dict[str, dict]:
    """The /log `response` of each listed user row that has one."""
    ids = list(dict.fromkeys(message_ids))
    if not ids:
        return {}
    out: dict[str, dict] = {}
    try:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = db.conn().execute(
                "SELECT message_id, state, reason, expected_turn_id FROM steer_receipts"
                f" WHERE message_id IN ({','.join('?' * len(chunk))})"
                " ORDER BY message_id, attempt", chunk).fetchall()
            latest = {row[0]: row for row in rows}          # the last attempt wins
            for message_id, row in latest.items():
                if row[1] in MARKED:
                    out[message_id] = {"state": row[1], "reason": row[2], "turn_id": row[3]}
    except sqlite3.OperationalError:
        return {}                                       # an older runtime: no receipts
    return out


def receipt_for_message(message_id: str) -> dict | None:
    try:
        row = db.conn().execute(
            "SELECT * FROM steer_receipts WHERE message_id=? ORDER BY attempt DESC LIMIT 1",
            (message_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row is not None else None


_CONTEXT: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "steer_receipt_context", default=("", ""))


@contextlib.contextmanager
def steer_context(*, prompt_admission_id: str = "", trace_id: str = "") -> Iterator[None]:
    """Hand the admission and trace of the message being steered to the
    receipt, through backend layers whose steer signatures stay unchanged.
    Set and reset on the calling thread around the synchronous steer call."""
    token = _CONTEXT.set((prompt_admission_id or "", trace_id or ""))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_context() -> dict[str, str]:
    admission, trace = _CONTEXT.get()
    return {"prompt_admission_id": admission, "trace_id": trace}
