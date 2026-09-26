"""Turn lifecycle state machine: the only writer of ``state_log`` and of
``turns.ended_at``.

Every observer of an agent turn (the dispatcher, the backend drain threads,
the Claude Code hooks running as separate processes, the reconciler, restart
recovery) reports what it saw as an *event*. The machine knows which events
are legal from which state, writes the resulting ``state_log`` row, and
refuses the rest: an illegal transition is logged, counted and raises
:class:`IllegalTransition` without touching the database.

The state set is :class:`lib.protocol.AgentState` plus ``NONE`` (an agent that
has no row yet). ``BUSY`` and ``TERMINAL`` are the single source for "is this
agent working" and "did this turn settle"; ``AgentState.busy_states()`` and the
hand-built sets that used to live in ``turn_dispatch`` and ``reconcile`` are
views of them.

Why the table is permissive in places. ``state_log`` is an observation log
written by several processes that race by milliseconds: the Stop hook and the
dispatcher both report completion, a fast backend can finish before the
dispatcher records the spawn, the reconciler may flip a turn whose process it
cannot see (a bare ``claude`` in a shell) to IDLE in the middle of that turn.
Each of those is a legitimate sequence and stays legal. What the table refuses
is what no legitimate sequence produces: tool or compaction activity for an
agent that has never been prompted or whose turn already settled, a turn
ending for an agent that never started one, a repair of a state that is not
busy.

``force`` exists for repairs. ``reconcile`` uses it because a repair is by
definition a write the table would not otherwise allow; the back-dated
``UNLAUNCHED`` receipt uses it because it is a historical insert, not a
transition from the current state.
"""
from __future__ import annotations

import collections
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

from .db import conn, now_ms
from .log import log
from .protocol import AgentState, TurnSource

# An agent with no state_log row yet.
NONE = ""

BUSY: frozenset[str] = frozenset({
    AgentState.THINKING, AgentState.TOOL, AgentState.COMPACTING,
})
# A turn settled here. Nothing is running; the next event is a new turn.
TERMINAL: frozenset[str] = frozenset({
    AgentState.DONE, AgentState.IDLE, AgentState.INTERRUPTED,
    AgentState.STOPPED,
})
ALL: frozenset[str] = frozenset(AgentState.valid()) | {NONE}
# Any agent that has at least one row.
STARTED: frozenset[str] = ALL - {NONE}
# States in which in-turn activity (tools, compaction) is legitimate. IDLE is
# included on purpose: the reconciler flips a busy agent it cannot see a
# process for (an interactive `claude` in a plain shell) to IDLE, and the
# hooks of that very turn keep reporting afterwards.
ACTIVE: frozenset[str] = BUSY | {
    AgentState.WAITING, AgentState.BACKGROUND, AgentState.IDLE,
}
# States a turn can end from. DONE and INTERRUPTED are here because the Stop
# hook and the dispatcher both report the end of one turn, in either order.
ENDABLE: frozenset[str] = ACTIVE | {AgentState.DONE, AgentState.INTERRUPTED}


class TurnEvent:
    """What an observer saw. Each event maps to exactly one target kind."""
    AGENT_CREATED = "agent_created"
    AGENT_DELETED = "agent_deleted"
    PROMPT_ADMITTED = "prompt_admitted"          # hook: UserPromptSubmit
    SPAWN_STARTED = "spawn_started"              # dispatcher: backend accepted
    RETRY_SCHEDULED = "retry_scheduled"          # dispatcher: reconnecting
    ACCOUNT_RECOVERY_WAIT = "account_recovery_wait"
    MODEL_FALLBACK_STARTED = "model_fallback_started"
    TEXT_STREAMED = "text_streamed"              # backend: assistant delta
    TOOL_STARTED = "tool_started"                # hook / backend
    TOOL_FINISHED = "tool_finished"              # hook / backend
    COMPACTION_STARTED = "compaction_started"    # hook / backend
    COMPACTION_FINISHED = "compaction_finished"  # backend
    NOTIFICATION = "notification"                # hook: waiting on the user
    BACKGROUND_DECLARED = "background_declared"  # agent-declared out-of-band work
    HOOK_STOP = "hook_stop"                      # hook: Stop
    PROCESS_EXITED_OK = "process_exited_ok"      # dispatcher: clean result
    PROCESS_EXITED_FAILED = "process_exited_failed"  # dispatcher: gave up
    TURN_FAILED_UNCLASSIFIED = "turn_failed_unclassified"  # legacy idle flip
    STOP_REQUESTED = "stop_requested"            # user stop / janitor cancel
    RESTART_INTERRUPTED = "restart_interrupted"  # boot recovery
    UNLAUNCHED = "unlaunched"                    # back-dated launch receipt
    RECONCILE_REPAIR = "reconcile_repair"        # busy with nothing running
    # An administrative note (a janitor ownership handoff) that repeats the
    # current state with new detail. Not an inferred change of state: the
    # target is whatever the agent is in now, and it needs a started agent.
    AUDIT_NOTED = "audit_noted"
    # Compatibility shim for writers that have not adopted an event yet
    # (`agents.record_state`). The kind is given explicitly and the edge is
    # legal from every state, so behaviour is unchanged for those callers.
    RECORDED = "recorded"

    @classmethod
    def all(cls) -> frozenset[str]:
        return frozenset(TRANSITIONS) | {cls.RECORDED, cls.AUDIT_NOTED}


# event -> (target kind, states the event is legal from)
TRANSITIONS: dict[str, tuple[str, frozenset[str]]] = {
    TurnEvent.AGENT_CREATED: (AgentState.SPAWNED, ALL),
    TurnEvent.AGENT_DELETED: (AgentState.STOPPED, ALL),
    TurnEvent.PROMPT_ADMITTED: (AgentState.THINKING, ALL),
    TurnEvent.SPAWN_STARTED: (AgentState.THINKING, ALL),
    TurnEvent.RETRY_SCHEDULED: (AgentState.THINKING, STARTED),
    TurnEvent.ACCOUNT_RECOVERY_WAIT: (AgentState.THINKING, STARTED),
    TurnEvent.MODEL_FALLBACK_STARTED: (AgentState.THINKING, STARTED),
    # A fast backend streams before the dispatcher records the spawn, so the
    # previous turn's terminal state is a legal origin.
    TurnEvent.TEXT_STREAMED: (AgentState.THINKING, STARTED),
    TurnEvent.TOOL_STARTED: (AgentState.TOOL, ACTIVE),
    TurnEvent.TOOL_FINISHED: (AgentState.TOOL, ACTIVE),
    TurnEvent.COMPACTION_STARTED: (AgentState.COMPACTING, ACTIVE),
    TurnEvent.COMPACTION_FINISHED: (AgentState.IDLE, ACTIVE),
    # Claude Code nags about idleness from a settled turn too.
    TurnEvent.NOTIFICATION: (AgentState.WAITING, STARTED),
    TurnEvent.BACKGROUND_DECLARED: (AgentState.BACKGROUND, STARTED),
    TurnEvent.HOOK_STOP: (AgentState.DONE, ENDABLE),
    TurnEvent.PROCESS_EXITED_OK: (AgentState.DONE, ENDABLE),
    TurnEvent.PROCESS_EXITED_FAILED: (AgentState.INTERRUPTED, ENDABLE),
    TurnEvent.TURN_FAILED_UNCLASSIFIED: (AgentState.IDLE, ENDABLE),
    TurnEvent.STOP_REQUESTED: (AgentState.INTERRUPTED, STARTED),
    TurnEvent.RESTART_INTERRUPTED: (AgentState.INTERRUPTED, BUSY),
    TurnEvent.UNLAUNCHED: (AgentState.INTERRUPTED, ALL),
    TurnEvent.RECONCILE_REPAIR: (AgentState.IDLE, BUSY),
}

# Kinds whose detail is enriched with the turn origin (see
# ``_detail_with_origin``); the set predates the machine and is unchanged.
_ORIGIN_ENRICHED_KINDS = frozenset({
    AgentState.THINKING, AgentState.TOOL, AgentState.COMPACTING,
    AgentState.DONE, AgentState.IDLE,
})
_ORIGIN_CARRY_WINDOW_MS = 120_000


class IllegalTransition(Exception):
    """The table has no edge for (current state, event); nothing was written."""

    def __init__(self, agent_id: str, event: str, from_state: str,
                 to_state: str):
        self.agent_id = agent_id
        self.event = event
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"illegal transition agent={agent_id} event={event} "
            f"{from_state or 'none'} -> {to_state}")


@dataclass(frozen=True)
class Transition:
    agent_id: str
    event: str
    from_state: str
    to_state: str
    ts: int
    detail: dict[str, Any] | None
    forced: bool = False
    state_id: int = 0


@dataclass
class _Refusal:
    agent_id: str
    event: str
    from_state: str
    to_state: str
    ts: int


class TurnStateMachine:
    """Transitions, the busy/terminal sets, and the ``turns`` row writers."""

    MAX_RECENT_REFUSALS = 256

    def __init__(self, table: dict[str, tuple[str, frozenset[str]]] | None = None):
        self.table = dict(table or TRANSITIONS)
        self._lock = threading.Lock()
        self._illegal: collections.Counter = collections.Counter()
        self._recent: collections.deque[_Refusal] = collections.deque(
            maxlen=self.MAX_RECENT_REFUSALS)

    # --- table -------------------------------------------------------------

    def target(self, event: str) -> str:
        return self.table[event][0]

    def is_legal(self, from_state: str, event: str) -> bool:
        if event == TurnEvent.RECORDED:
            return True
        if event == TurnEvent.AUDIT_NOTED:
            return from_state in STARTED
        entry = self.table.get(event)
        return entry is not None and from_state in entry[1]

    def legal_edges(self) -> Iterable[tuple[str, str, str]]:
        """Every (from_state, event, to_state) the table allows."""
        for event, (target, origins) in self.table.items():
            for origin in sorted(origins):
                yield origin, event, target

    # --- state_log ---------------------------------------------------------

    def transition(self, agent_id: str, event: str,
                   detail: dict[str, Any] | None = None, *,
                   kind: str | None = None, force: bool = False,
                   ts: int | None = None) -> Transition:
        """Apply ``event`` to ``agent_id`` and write its ``state_log`` row.

        ``kind`` is only read for :attr:`TurnEvent.RECORDED`, the compatibility
        event whose target the caller names. ``force`` skips the legality
        check (repairs); ``ts`` back-dates the row (historical receipts).
        """
        from_state = self.current_kind(agent_id)
        if event == TurnEvent.RECORDED:
            if kind is None or not AgentState.is_valid(kind):
                raise ValueError(f"invalid agent state: {kind}")
            target = kind
        elif event == TurnEvent.AUDIT_NOTED:
            target = from_state
        else:
            try:
                target = self.table[event][0]
            except KeyError:
                raise ValueError(f"unknown turn event: {event}") from None
        if not force and not self.is_legal(from_state, event):
            self._refuse(agent_id, event, from_state, target)
            raise IllegalTransition(agent_id, event, from_state, target)
        stamp = now_ms() if ts is None else int(ts)
        detail = _detail_with_origin(agent_id, target, detail, stamp)
        from . import agents as agents_db
        runtime_id = agents_db.current_runtime_id(agent_id)
        cursor = conn().execute(
            """INSERT INTO state_log (agent_id, runtime_id, ts, kind, detail)
               VALUES (?, ?, ?, ?, ?)""",
            (agent_id, runtime_id, stamp, target,
             json.dumps(detail) if detail else None))
        return Transition(
            agent_id=agent_id, event=event, from_state=from_state,
            to_state=target, ts=stamp, detail=detail, forced=force,
            state_id=int(cursor.lastrowid or 0))

    def record(self, agent_id: str, kind: str,
               detail: dict[str, Any] | None = None) -> Transition:
        """Compatibility writer: an explicit kind, legal from every state."""
        return self.transition(agent_id, TurnEvent.RECORDED, detail, kind=kind)

    @staticmethod
    def current_kind(agent_id: str) -> str:
        row = conn().execute(
            """SELECT kind FROM state_log WHERE agent_id = ?
                ORDER BY ts DESC, state_id DESC LIMIT 1""",
            (agent_id,)).fetchone()
        return str(row["kind"] or "") if row else NONE

    def _refuse(self, agent_id: str, event: str, from_state: str,
                to_state: str) -> None:
        with self._lock:
            self._illegal[(from_state or "none", event)] += 1
            self._recent.append(_Refusal(agent_id, event, from_state,
                                         to_state, now_ms()))
        log("illegalTransition",
            f"agent={agent_id} event={event} "
            f"{from_state or 'none'} -> {to_state} refused")

    # --- diagnostics -------------------------------------------------------

    def illegal_counts(self) -> dict[tuple[str, str], int]:
        with self._lock:
            return dict(self._illegal)

    def recent_refusals(self) -> list[dict[str, Any]]:
        with self._lock:
            return [vars(item).copy() for item in self._recent]

    def reset_for_tests(self) -> None:
        with self._lock:
            self._illegal.clear()
            self._recent.clear()

    # --- turns -------------------------------------------------------------

    @staticmethod
    def open_turn(*, agent_id: str, source: str, trace_id: str,
                  synthesize_audio: bool = True) -> int:
        if source not in TurnSource.valid():
            raise ValueError(f"invalid turn source: {source}")
        from . import agents as agents_db
        runtime_id = agents_db.current_runtime_id(agent_id)
        cursor = conn().execute(
            """INSERT INTO turns
                   (agent_id, runtime_id, source, trace_id, synthesize_audio,
                    started_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (agent_id, runtime_id, source, trace_id,
             1 if synthesize_audio else 0, now_ms()))
        return int(cursor.lastrowid or 0)

    @staticmethod
    def close_turn(turn_id: int) -> None:
        conn().execute("UPDATE turns SET ended_at = ? WHERE turn_id = ?",
                       (now_ms(), turn_id))

    @staticmethod
    def close_turns_for_trace(agent_id: str, trace_id: str) -> int:
        """End every open turn row of ``trace_id``; returns how many."""
        cursor = conn().execute(
            """UPDATE turns SET ended_at = ?
                WHERE agent_id = ? AND trace_id = ? AND ended_at IS NULL""",
            (now_ms(), agent_id, trace_id))
        return int(cursor.rowcount or 0)

    @staticmethod
    def enable_latest_turn_audio(agent_id: str) -> None:
        """Upgrade the newest turn to speech; never downgrade a voice turn."""
        conn().execute(
            """UPDATE turns SET synthesize_audio = 1
                WHERE turn_id = (
                   SELECT turn_id FROM turns WHERE agent_id = ?
                    ORDER BY started_at DESC, turn_id DESC LIMIT 1)""",
            (agent_id,))

    @staticmethod
    def open_turns() -> list[dict[str, Any]]:
        """The newest open turn per agent: the durable in-flight authority."""
        rows = conn().execute(
            """SELECT t.agent_id, t.trace_id, t.turn_id, t.started_at
                 FROM turns t
                WHERE t.ended_at IS NULL
                  AND t.turn_id = (SELECT MAX(x.turn_id) FROM turns x
                                    WHERE x.agent_id = t.agent_id
                                      AND x.ended_at IS NULL)
                ORDER BY t.turn_id""").fetchall()
        return [dict(row) for row in rows]

    def record_unlaunched(self, agent_id: str, trace_id: str) -> Transition:
        """Historical failure receipt, ordered before any newer turn's state.

        Recording a late launch failure at wall-clock now could replace the
        THINKING state of a newer owner. Anchor it to the admitted message.
        """
        row = conn().execute(
            """SELECT MIN(updated_at) AS ts FROM messages
                WHERE agent_id = ? AND trace_id = ? AND role = 'user'""",
            (agent_id, trace_id)).fetchone()
        ts = int(row["ts"] or now_ms()) - 1
        return self.transition(
            agent_id, TurnEvent.UNLAUNCHED,
            {"trace_id": trace_id, "dispatch_not_started": True,
             "message": "Message saved; backend did not start. Retry is safe."},
            force=True, ts=ts)


# --- origin enrichment (moved from agents.record_state, unchanged) -----------

def _detail_with_origin(agent_id: str, kind: str,
                        detail: dict[str, Any] | None, ts: int) -> dict | None:
    if detail and detail.get("origin"):
        return detail
    if kind not in _ORIGIN_ENRICHED_KINDS:
        return detail
    from . import agents as agents_db
    raw_detail = detail or {}
    backend_session_id = str(
        raw_detail.get("backend_session_id")
        or agents_db.live_backend_session(agent_id) or "")
    origin = ""
    if backend_session_id:
        try:
            from . import message_store
            origin = message_store.latest_turn_user_origin(
                agent_id=agent_id, backend_session_id=backend_session_id,
                done_ts=ts)
        except Exception:  # noqa: BLE001 - enrichment is best effort
            origin = ""
    if not origin:
        origin = _recent_state_origin(agent_id, ts)
    if not origin:
        return detail
    enriched = dict(detail or {})
    enriched["origin"] = origin
    return enriched


def _recent_state_origin(agent_id: str, ts: int) -> str:
    """Carry turn origin from hook DONE into immediate tail state rows."""
    row = conn().execute(
        """SELECT ts, detail FROM state_log
            WHERE agent_id = ? AND ts <= ? AND detail IS NOT NULL
            ORDER BY ts DESC, state_id DESC LIMIT 1""",
        (agent_id, ts)).fetchone()
    if row is None or ts - int(row["ts"] or 0) > _ORIGIN_CARRY_WINDOW_MS:
        return ""
    try:
        payload = json.loads(row["detail"] or "{}")
    except json.JSONDecodeError:
        return ""
    return str(payload.get("origin") or "")


# --- process-wide machine ------------------------------------------------------

MACHINE = TurnStateMachine()


def transition(agent_id: str, event: str, detail: dict[str, Any] | None = None,
               *, kind: str | None = None, force: bool = False,
               ts: int | None = None) -> Transition:
    return MACHINE.transition(agent_id, event, detail, kind=kind, force=force,
                              ts=ts)


def try_transition(agent_id: str, event: str,
                   detail: dict[str, Any] | None = None, *,
                   force: bool = False) -> Transition | None:
    """``transition`` for flows that must continue past a refusal.

    The refusal is still logged and counted; only the exception is swallowed.
    """
    try:
        return transition(agent_id, event, detail, force=force)
    except IllegalTransition:
        return None


def record(agent_id: str, kind: str,
           detail: dict[str, Any] | None = None) -> Transition:
    return MACHINE.record(agent_id, kind, detail)


def current_kind(agent_id: str) -> str:
    return MACHINE.current_kind(agent_id)


def target(event: str) -> str:
    """The state ``event`` moves an agent to."""
    return MACHINE.target(event)


def open_turn(*, agent_id: str, source: str, trace_id: str,
              synthesize_audio: bool = True) -> int:
    return MACHINE.open_turn(agent_id=agent_id, source=source,
                             trace_id=trace_id,
                             synthesize_audio=synthesize_audio)


def close_turn(turn_id: int) -> None:
    MACHINE.close_turn(turn_id)


def close_turns_for_trace(agent_id: str, trace_id: str) -> int:
    return MACHINE.close_turns_for_trace(agent_id, trace_id)


def open_turns() -> list[dict[str, Any]]:
    return MACHINE.open_turns()


def enable_latest_turn_audio(agent_id: str) -> None:
    MACHINE.enable_latest_turn_audio(agent_id)


def record_unlaunched(agent_id: str, trace_id: str) -> Transition:
    return MACHINE.record_unlaunched(agent_id, trace_id)


def illegal_counts() -> dict[tuple[str, str], int]:
    return MACHINE.illegal_counts()


def recent_refusals() -> list[dict[str, Any]]:
    return MACHINE.recent_refusals()


def reset_for_tests() -> None:
    MACHINE.reset_for_tests()


# --- hooks (separate processes) --------------------------------------------------

def hook_transition(agent_id: str, event: str,
                    detail: dict[str, Any] | None = None) -> Transition | None:
    """Entry for the Claude Code hooks, which run as their own processes.

    They reach the database exactly as before: ``lib.db.conn()`` opens the
    per-process connection from the environment on first use. A refusal is
    reported through the eventlog (the hooks' only channel) and returns
    ``None``; a hook is a sensor and never fails its turn over bookkeeping.
    """
    try:
        return transition(agent_id, event, detail)
    except IllegalTransition as exc:
        try:
            from .eventlog import emit
            emit("hook", "illegalTransition",
                 detail={"agent_id": agent_id, "event": event,
                         "from": exc.from_state, "to": exc.to_state})
        except Exception:  # noqa: BLE001
            pass
        return None
