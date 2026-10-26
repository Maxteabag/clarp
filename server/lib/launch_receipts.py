"""Pending-launch receipts: proof that an admitted user message never reached a backend.

A user send is admitted (its durable row written) before the dispatcher opens a
turn and hands the prompt to a backend. Anything that fails in between used to
leave the row with no state at all, and `agents.trace_launch_status` rightly
calls missing state "unknown": a retry with the same client message id was
refused for good (2026-10-25, Lumi: the post-COMMIT SSE insert hit "database is
locked" and the message was saved but never sent).

The receipt closes that gap without guessing. It is written in the admission
transaction, so it exists exactly when the row does, and it moves to
``attempted`` (fail-closed) immediately before the prompt is handed to a
backend by spawn or steer. Only a receipt still ``pending`` proves nothing was
sent; ``attempted`` says nothing, and the state log keeps deciding as before.
Stop fences pending receipts so an automatic retry is not fresh intent.

Rows live in ``settings`` under one prefix: same database and transaction as
the admission, no schema change. Pruning is throttled, uses the key index, and
never deletes a fenced receipt; deleting any other one only makes it unknown.

The handoff never trusts a compare-and-set that changed nothing: it reads the
receipt back and refuses a fenced or superseded one. Stop also records an
in-process epoch first, so a fence write that failed still blocks a same-id
resend here, and it retries the write.

Delivery state (F4, docs: joint packet 9a96df74 + addendum 1). Every
transition also moves the receipt's ``rev`` (+1) and takes a fresh global
``seq`` (``launch-receipt.seq``), and moves the agent's seq pointer
``launch-receipt-seq:<agent>:<seq>`` -> trace, all in the transition's own
write transaction. The
pointers give clients a seq-ordered, page-bounded change feed (``delivery_page``)
without a schema change; ``project`` maps a receipt to the client-visible
state. Nothing here is retry authority: ``status`` and ``mark_attempted`` decide
exactly as before, and the projection is computed after those decisions.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from . import db, settings_store
from .db import now_ms
from .timing import SQLITE_CRITICAL_BUSY_TIMEOUT_MS

PREFIX = "launch-receipt:"
PENDING = "pending"
ATTEMPTED = "attempted"
FENCED = "fenced"
RELAUNCHED = "relaunched"
RETENTION_MS = 7 * 24 * 3600 * 1000
PRUNE_INTERVAL_SEC = 600.0
PRUNE_LIMIT = 500

SEQ_KEY = "launch-receipt.seq"              # global; one per transition, in its transaction
PRUNE_SEQ_KEY = "launch-receipt.prune_seq"  # max seq the prune ever deleted (monotonic)
POINTER_PREFIX = "launch-receipt-seq:"      # <agent>:<seq, 20 digits> -> trace_id
PAGE_CAP = 200
_SEQ_WIDTH = 20

# Client-visible delivery states (the receipt states are internal).
PENDING_LAUNCH = "pending_launch"
STOPPED = "stopped"
HANDED_OVER = "handed_over"

_LOCK = threading.Lock()
_STOP_EPOCH: dict[str, int] = {}     # agent_id -> ms of its latest Stop in this process
_LAST_PRUNE = 0.0


class HandoffRefused(RuntimeError):
    """The receipt says this prompt must not be handed to a backend."""

    def __init__(self, state: str):
        self.state = state
        super().__init__(f"launch receipt is {state}")


def eligible(*, origin: str, client_msg_id: str, call_opening: bool) -> bool:
    """User sends the client retries under the same id. Queue receipts,
    openings and machine origins keep their own recovery."""
    return origin == "user" and bool(client_msg_id) and not call_opening


def _key(trace_id: str) -> str:
    return PREFIX + trace_id


def get(trace_id: str) -> dict | None:
    if not trace_id:
        return None
    raw = settings_store.get(_key(trace_id))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _count(value) -> int:
    """A stored rev/seq/generation: a positive int, else 0 (absent, legacy, malformed)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def pointer_key(agent_id: str, seq: int) -> str:
    return f"{POINTER_PREFIX}{agent_id}:{int(seq):0{_SEQ_WIDTH}d}"


def _pointer_seq(key: str) -> int:
    try:
        return int(key[-_SEQ_WIDTH:])
    except ValueError:
        return 0


@contextmanager
def _write() -> Iterator[None]:
    """The caller's transaction (admission, Stop pause), else our own."""
    if db.conn().in_transaction:
        yield
    else:
        with settings_store.transaction():
            yield


def _next_seq() -> int:
    """Inside a write transaction: the writer lock is held, so the value read
    back is exactly this transition's."""
    settings_store.increment(SEQ_KEY)
    return settings_store.get_int(SEQ_KEY)


def _store(trace_id: str, receipt: dict, *, stamp: int, previous_seq: int = 0) -> None:
    """Write ``receipt`` (already carrying its new rev and seq) and move its
    pointer; the caller holds the write transaction."""
    agent_id = str(receipt.get("agent_id") or "")
    settings_store.set_text(_key(trace_id), json.dumps(receipt, separators=(",", ":")),
                            updated_at=stamp)
    if previous_seq:
        settings_store.delete(pointer_key(agent_id, previous_seq))
    settings_store.set_text(pointer_key(agent_id, receipt["seq"]), trace_id, updated_at=stamp)


def _move(trace_id: str, receipt: dict, state: str) -> None:
    """One transition of a receipt just read in this write transaction."""
    stamp = now_ms()
    _store(trace_id, dict(receipt, state=state, rev=_count(receipt.get("rev")) + 1,
                          seq=_next_seq()),
           stamp=stamp, previous_seq=_count(receipt.get("seq")))


def record_pending(*, agent_id: str, trace_id: str, client_msg_id: str,
                   origin: str, sender_agent_id: str = "",
                   supersedes: str = "", user_stop_generation: int = 0) -> None:
    """Write the receipt inside the caller's admission transaction.

    ``user_stop_generation`` is the agent's durable USER Stop counter at
    admission (turn_queue, moved only in the pause transaction of the user's
    Stop, never by a maintenance cancel or its restore): a later user Stop is
    visible to a retry even across a restart, without relying on this
    module's fence write or its in-process epoch."""
    stamp = now_ms()
    with _write():
        if supersedes and supersedes != trace_id:
            old = get(supersedes)
            if old is not None and old.get("state") == PENDING:
                _move(supersedes, old, RELAUNCHED)
        existing = get(trace_id)
        _store(trace_id, {
            "state": PENDING, "agent_id": agent_id, "trace_id": trace_id,
            "client_msg_id": client_msg_id, "origin": origin,
            "sender_agent_id": sender_agent_id or "", "admitted_at": stamp,
            "user_stop_gen": int(user_stop_generation),
            "rev": _count((existing or {}).get("rev")) + 1, "seq": _next_seq(),
        }, stamp=stamp, previous_seq=_count((existing or {}).get("seq")))
    _maybe_prune(stamp)


def _maybe_prune(stamp: int) -> None:
    global _LAST_PRUNE
    now = time.monotonic()
    with _LOCK:
        if _LAST_PRUNE and now - _LAST_PRUNE < PRUNE_INTERVAL_SEC:
            return
        _LAST_PRUNE = now
    prune(stamp)


def prune(stamp: int) -> dict:
    """Delete receipts last written over RETENTION_MS ago (fenced ones are kept:
    without them an older state row could read "retryable" again after Stop;
    any other deletion only yields "unknown") and their pointers, at most
    PRUNE_LIMIT receipts, and advance PRUNE_SEQ_KEY over every deleted seq, all
    in one transaction. A seq-less (legacy) receipt deletes no pointer and
    moves no watermark."""
    cutoff = stamp - RETENTION_MS
    deleted = {"receipts": 0, "pointers": 0, "watermark": 0}
    with _write():
        doomed = settings_store.prune_candidates(
            PREFIX, updated_before=cutoff, field="state", keep=(FENCED,), limit=PRUNE_LIMIT)
        keys, top = [], 0
        for key, raw in doomed:
            keys.append(key)
            receipt = _parse(raw)
            seq = _count((receipt or {}).get("seq"))
            if seq:
                keys.append(pointer_key(str(receipt.get("agent_id") or ""), seq))
                deleted["pointers"] += 1
                top = max(top, seq)
        settings_store.delete_keys(keys)
        deleted["receipts"] = len(doomed)
        if top > settings_store.get_int(PRUNE_SEQ_KEY):
            settings_store.set_int(PRUNE_SEQ_KEY, top)
        deleted["watermark"] = settings_store.get_int(PRUNE_SEQ_KEY)
    return deleted


def _parse(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _stop_generation_moved(receipt: dict) -> bool:
    """Durable: the USER stopped this agent after admission. A receipt without
    the counter (none exist in production) fails closed."""
    from . import turn_queue
    if "user_stop_gen" not in receipt:
        return True
    agent_id = str(receipt.get("agent_id") or "")
    return turn_queue.user_stop_generation(agent_id) > int(receipt["user_stop_gen"])


def _stopped_after_admission(receipt: dict) -> bool:
    with _LOCK:
        stopped_at = _STOP_EPOCH.get(str(receipt.get("agent_id") or ""), 0)
    return bool(stopped_at) and stopped_at >= int(receipt.get("admitted_at") or 0)


def mark_attempted(trace_id: str, *, override_fence: bool = False) -> str:
    """Called immediately before a backend receives the prompt.

    Returns "attempted" (moved from pending), "already_attempted" or
    "no_receipt". Raises HandoffRefused for a fenced or superseded receipt,
    and any database error as is, so the caller refuses to hand it over: a
    launch not recorded here could later be mistaken for one that never ran.
    A compare-and-set that changed no row never authorizes a handoff by
    itself; the receipt is read back to say why. ``override_fence`` is for a
    launch Stop or a durable queue row already decided runs: a fence that
    raced it is replaced; a superseded receipt still refuses.
    """
    if not trace_id:
        return "no_receipt"
    outcome, refused = "", ""
    with db.busy_timeout(SQLITE_CRITICAL_BUSY_TIMEOUT_MS), _write():
        # One write transaction: the state read, the transition, its rev, seq
        # and pointer commit together or not at all.
        receipt = get(trace_id)
        state = receipt.get("state") if receipt is not None else None
        if receipt is None:
            outcome = "no_receipt"
        elif state == PENDING and not override_fence and _stopped_after_admission(receipt):
            _move(trace_id, receipt, FENCED)
            refused = FENCED
        elif state == PENDING or (state == FENCED and override_fence):
            _move(trace_id, receipt, ATTEMPTED)
            outcome = "attempted"
        elif state == ATTEMPTED:
            outcome = "already_attempted"
        else:
            refused = str(state)
    if refused:
        raise HandoffRefused(refused)
    return outcome


def note_stop(agent_id: str) -> None:
    """Stop, in this process, before its fence write: a resend or handoff
    here never treats an earlier admission as unsent, even if that write fails."""
    if agent_id:
        with _LOCK:
            _STOP_EPOCH[agent_id] = now_ms()


def fence_agent(agent_id: str, *, on_fenced=None) -> int:
    """Stop: a message admitted but not yet launched is no longer
    retryable by an automatic resend under the same id."""
    if not agent_id:
        return 0
    fenced: list[str] = []
    with _write():
        # One read of the receipt prefix (as before), then one transition per
        # pending receipt of this agent, each with its own seq.
        for key, raw in settings_store.json_prefix_items(PREFIX, agent_id=agent_id, state=PENDING):
            receipt = _parse(raw)
            if receipt is not None:
                _move(key[len(PREFIX):], receipt, FENCED)
                fenced.append(key[len(PREFIX):])
    if on_fenced is not None:
        for trace_id in fenced:
            on_fenced(trace_id)
    return len(fenced)


def reset_for_tests() -> None:
    global _LAST_PRUNE
    with _LOCK:
        _STOP_EPOCH.clear()
        _LAST_PRUNE = 0.0


def status(agent_id: str, trace_id: str, *, client_msg_id: str | None = None,
           origin: str | None = None, sender_agent_id: str | None = None) -> str:
    """"retryable" only for a pending receipt of this agent (and, when given,
    this client id and principal); "fenced" after Stop; otherwise "unknown"."""
    receipt = get(trace_id)
    if not receipt or receipt.get("agent_id") != agent_id:
        return "unknown"
    state = receipt.get("state")
    if state == FENCED or (state == PENDING and (
            _stopped_after_admission(receipt) or _stop_generation_moved(receipt))):
        return "fenced"
    if state != PENDING:
        return "unknown"
    if client_msg_id is not None and receipt.get("client_msg_id") != client_msg_id:
        return "unknown"
    if origin is not None and receipt.get("origin") != origin:
        return "unknown"
    if (sender_agent_id is not None
            and (receipt.get("sender_agent_id") or "") != (sender_agent_id or "")):
        return "unknown"
    return "retryable"


# --- delivery state (F4): projection and bounded reads ------------------------------

def project(receipt: dict | None, *, agent_id: str, user_stop_generation: int,
            origin: str | None = None, sender_agent_id: str | None = None) -> dict | None:
    """The client-visible delivery object of a receipt, or None (unknown).

    attempted -> handed_over (possibly sent, not started); fenced -> stopped;
    pending -> stopped once the agent's durable USER Stop generation moved past
    the one admitted with, else pending_launch (a display hint, never retry
    authority). relaunched, legacy (no rev/seq), malformed or mismatched
    (agent, and when given origin and sender) receipts are unknown."""
    if not isinstance(receipt, dict):
        return None
    rev, seq = _count(receipt.get("rev")), _count(receipt.get("seq"))
    client_msg_id = receipt.get("client_msg_id")
    trace_id = receipt.get("trace_id")
    if (not rev or not seq or receipt.get("agent_id") != agent_id
            or not isinstance(client_msg_id, str) or not client_msg_id
            or not isinstance(trace_id, str) or not trace_id):
        return None
    if origin is not None and receipt.get("origin") != origin:
        return None
    if sender_agent_id is not None and (receipt.get("sender_agent_id") or "") != (sender_agent_id or ""):
        return None
    state = receipt.get("state")
    if state == ATTEMPTED:
        shown = HANDED_OVER
    elif state == FENCED:
        shown = STOPPED
    elif state == PENDING:
        admitted_gen = receipt.get("user_stop_gen")
        if not isinstance(admitted_gen, int) or isinstance(admitted_gen, bool):
            return None
        shown = STOPPED if int(user_stop_generation) > admitted_gen else PENDING_LAUNCH
    else:
        return None
    return {"state": shown, "rev": rev, "seq": seq,
            "for": {"client_msg_id": client_msg_id, "trace_id": trace_id}}


def _user_stop_generation(agent_id: str) -> int:
    from . import turn_queue
    return turn_queue.user_stop_generation(agent_id)


def delivery_for_send(client_msg_id: str, *, agent_id: str, origin: str,
                      sender_agent_id: str) -> dict | None:
    """S1: the delivery object a /send 200 may carry, read after the dispatch
    decided. Never ``stopped`` on a 200 (a stopped send answers 503)."""
    from . import message_writes
    if not client_msg_id or not agent_id:
        return None
    trace_id = message_writes.client_message_trace(client_msg_id)
    if not trace_id:
        return None
    with settings_store.read_snapshot():
        shown = project(get(trace_id), agent_id=agent_id,
                        user_stop_generation=_user_stop_generation(agent_id),
                        origin=origin, sender_agent_id=sender_agent_id or "")
    if shown is None or shown["state"] == STOPPED or shown["for"]["client_msg_id"] != client_msg_id:
        return None
    return shown


def _row_matches(message_id: str, shown: dict) -> bool:
    from . import message_writes
    return message_writes._client_message_id(shown["for"]["client_msg_id"]) == message_id


def annotate_rows(agent_id: str, turns: list[dict]) -> int:
    """S2: add ``delivery`` to the user rows of one /log page, from ONE batched
    receipt read and ONE user Stop generation read (no per-row reads). Returns
    that generation: the page's projection epoch."""
    users = [turn for turn in turns
             if turn.get("role") == "user" and turn.get("trace_id") and turn.get("id")]
    with settings_store.read_snapshot():
        raws = (settings_store.get_many([_key(turn["trace_id"]) for turn in users])
                if users else {})
        generation = _user_stop_generation(agent_id)
    for turn in users:
        shown = project(_parse(raws.get(_key(turn["trace_id"]))), agent_id=agent_id,
                        user_stop_generation=generation,
                        origin=turn.get("origin") or "user",
                        sender_agent_id=turn.get("sender_agent_id") or "")
        if shown is not None and shown["for"]["trace_id"] == turn["trace_id"] \
                and _row_matches(str(turn["id"]), shown):
            turn["delivery"] = shown
    return generation


def _expired() -> dict:
    return {"updates": [], "cursor": None, "more": False, "expired": True,
            "until": None, "stop_generation": None, "skipped": 0}


def delivery_page(agent_id: str, *, since: int, until: int | None = None,
                  stop_generation: int | None = None, bootstrap: bool = False,
                  cap: int = PAGE_CAP) -> dict:
    """One page of the agent's delivery feed in seq order, in ONE read snapshot.

    The cursor is (seq, G): G is the agent's durable USER Stop generation, the
    projection epoch (addendum 1). A user Stop can turn a pending receipt's
    projection into ``stopped`` without moving its seq (a failed fence write),
    so a cursor is valid only for the G it was taken under: any request whose
    G differs from the current one is expired and the client bootstraps again,
    which re-projects every retained receipt under the new G.

    Normal mode: pointers in (since, current_seq]; expired when G is missing or
    differs, the cursor is ahead of the Host, or behind the prune watermark.
    Bootstrap mode: the first page (since 0, no until) fixes until = current seq
    and G; every later page carries the same (until, G); expired if G changed,
    0 <= since <= until <= current_seq fails, or until is missing; no
    watermark expiry. The cursor advances over every RAW pointer read (cap rows
    -> more), else to the range end. Pointers are verified against their
    receipts; mismatches are skipped and counted. Work: counter, watermark and
    generation reads, one pointer range (<= cap) and one get_many."""
    from . import message_writes
    cap = max(1, int(cap))
    with settings_store.read_snapshot():
        current = settings_store.get_int(SEQ_KEY)
        watermark = settings_store.get_int(PRUNE_SEQ_KEY)
        generation = _user_stop_generation(agent_id)
        if since < 0 or since > current:
            return _expired()
        first_bootstrap_page = bootstrap and until is None
        if first_bootstrap_page:
            if since != 0:
                return _expired()
            until = current
        elif stop_generation is None or stop_generation != generation:
            return _expired()
        if bootstrap:
            if not (0 <= since <= until <= current):
                return _expired()
            high = until
        else:
            if since >= 1 and since < watermark:
                return _expired()
            high = current
        rows = settings_store.items_in_range(
            pointer_key(agent_id, since + 1), pointer_key(agent_id, high + 1), limit=cap)
        raws = settings_store.get_many([_key(trace) for _key_, trace in rows])
    updates, skipped = [], 0
    for key, trace in rows:
        seq = _pointer_seq(key)
        receipt = _parse(raws.get(_key(trace)))
        if (receipt is None or _count(receipt.get("seq")) != seq
                or receipt.get("trace_id") != trace or receipt.get("agent_id") != agent_id):
            skipped += 1
            continue
        shown = project(receipt, agent_id=agent_id, user_stop_generation=generation)
        if shown is not None:
            updates.append({"message_id": message_writes._client_message_id(
                shown["for"]["client_msg_id"]), "delivery": shown})
    if len(rows) == cap:
        cursor, more = _pointer_seq(rows[-1][0]), True
    else:
        cursor, more = high, False
    return {"updates": updates, "cursor": cursor, "more": more, "expired": False,
            "until": until if bootstrap else None, "stop_generation": generation,
            "skipped": skipped}
