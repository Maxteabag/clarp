"""DrainMixin: methods moved out of lib/turn_dispatch.py::TurnDispatchService by tools/refactor/s3_apply.py
(group "turn_drain"). Generated: change the recipe, not this file."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from . import agents as agents_db
from . import db
from . import turn_queue
from . import turn_lifecycle
from .turn_lifecycle import TurnEvent
from .log import log, log_exception
from .turn_dispatch_state import _TURN_LOCK, _SLOTS, _FAILOVERS, RECOVERY_PARK_PREFIX, _transfer_refusal, UPDATE_HOLD_HEADLINE, refresh_hold_headlines
from .turn_command import _TurnSpec, _park


class DrainMixin:
    def begin_admission_fence(self) -> bool:
        """Stop starting turns; in-flight turns finish. New sends, queued
        handoffs and recovery all wait, durably, behind the fence."""
        changed = _SLOTS.set_fenced(True)
        if changed:
            snapshot = _SLOTS.snapshot()
            log("admissionFenceRaised",
                f"active={len(snapshot['active'])} "
                f"queued={sum(snapshot['queued'].values())}")
        self.persist_held()
        refresh_hold_headlines()
        return changed

    @staticmethod
    def admission_fenced() -> bool:
        return bool(_SLOTS.fenced)

    @staticmethod
    def refresh_hold_headlines() -> None:
        refresh_hold_headlines()

    @staticmethod
    def clear_hold_headlines() -> None:
        from . import live_hub
        hub = live_hub.current()
        if hub is not None:
            hub.set_update_holds(set(), UPDATE_HOLD_HEADLINE)

    def lower_admission_fence(self) -> bool:
        """Let turns start again; ``start_held_work`` starts the held ones."""
        # Queue rows the hand-over parked in place run from memory again,
        # which reads them only while queued. Only work still held: a
        # cancelled spec's row was ended with it (prepare_queued_for_finish).
        rows = sorted(spec.queue_id for specs in _SLOTS.queued_specs().values()
                      for spec in specs if spec.taken_over and spec.queue_id)
        if rows:
            turn_queue.requeue_rows(rows)
        self.handed_over.clear()
        changed = _SLOTS.set_fenced(False)
        if changed:
            log("admissionFenceLowered", f"held={len(_SLOTS.queued_specs())}")
        refresh_hold_headlines()
        return changed

    def start_held_work(self) -> None:
        """Start the head of every queue the fence left without an owner."""
        self._start_ownerless_queues(None)

    def persist_held(self, only: str | None = None) -> int:
        """Make held work survive the handoff, keeping its order.

        A queued spec with neither a durable queue row nor a park row (a normal
        send queued behind a spawning slot, a terminal or a process) gets a
        ``drain-park`` row. Rows are numbered by arrival, so any durable spec
        behind a newly parked one moves to the back after it, preserving the
        agent's FIFO order for the runtime that recovers them. A spec whose
        arrival was exempt from the Stop pause keeps that on its row. Runs
        with no lock held (the turn-lock write rule); returns new park rows."""
        parked = 0
        for agent_id, specs in _SLOTS.queued_specs().items():
            if only is not None and agent_id != only:
                continue
            reorder = False
            for spec in specs:
                if spec.drain_persisted and not reorder:
                    continue
                durable = spec
                if not spec.queue_id and not spec.park_id:
                    park_id = _park(spec, prefix="drain-park")
                    if not park_id:
                        continue  # still unpersisted; the handoff waits
                    durable = replace(spec, park_id=park_id)
                    parked += 1
                    reorder = True
                else:
                    row_id = spec.queue_id or spec.park_id
                    if reorder:
                        turn_queue.move_to_back(row_id)
                    if spec.bypass_pause and not spec.drain_persisted:
                        turn_queue.allow_paused(row_id)
                durable = replace(durable, drain_persisted=True)
                if not _SLOTS.swap_queued(agent_id, spec, durable):
                    # Stop or a cancel took it meanwhile; a park row written
                    # for it here must not resurrect it after a restart.
                    if durable.park_id and durable.park_id != spec.park_id:
                        turn_queue.unpark(durable.park_id)
        if parked:
            log("drainHeldPersisted", f"parked={parked}")
        return parked

    def transfer_recovery_parks(self, excluded: dict[str, str] | None = None
                                ) -> dict[str, Any]:
        """The release drain's hand-over of account-recovery parks.

        Work parked for an account before any process ran for it (the
        coordinator registered it while recovering) becomes durable held work:
        a ``recovery-park`` row at the head of the agent's queue, in one write
        transaction with the end of its turn row and an ``idle`` state that
        names the handoff, then the same spec at the head of the in-memory
        queue with that park id, and the slot is given back. From there it is
        held work like any other: the next runtime recovers it in order (after
        ``resume_account_recovery`` restores the park's start, so the bound
        does not restart), a revert starts it first. Only work whose exact
        prompt, binding, order and pause state are known qualifies
        (``_transfer_refusal``); a running recovery pass, a process that ran,
        or anything uncertain keeps its blocker. Credentials and account
        choice never move. Returns ``{"transferred": [agents], "refused":
        {agent: reason}}``."""
        excluded = dict(excluded or {})
        # Reads before any lock (the turn-lock rule): the binding and the
        # pause state the transfer must preserve.
        facts: dict[str, dict] = {}
        for coordinator in _FAILOVERS.values():
            for agent_id in coordinator.status().get("waiting") or ():
                attempt = coordinator.attempts.get(agent_id)
                work = getattr(attempt, "work", None)
                spec = work[0] if isinstance(work, tuple) and work else None
                facts[agent_id] = {
                    "binding": agents_db.live_backend_session(agent_id),
                    "paused": turn_queue.is_paused(agent_id),
                    "excluded": excluded.get(agent_id, ""),
                    # Work that already has a durable row is taken over, so
                    # that row must be unambiguously its own.
                    "queued_row": bool(
                        isinstance(spec, _TurnSpec) and spec.queue_id
                        and turn_queue.queued_row_matches(
                            spec.queue_id, agent_id=agent_id,
                            trace_id=spec.trace_id,
                            prompt_admission_id=spec.prompt_admission_id)),
                }
        refused: dict[str, str] = {}

        def eligible(attempt) -> bool:
            work = attempt.work
            if not (isinstance(work, tuple) and len(work) == 2
                    and isinstance(work[0], _TurnSpec)):
                refused[attempt.agent_id] = "unknown_work"
                return False
            spec, number = work
            if spec.trace_id != attempt.trace_id:
                refused[attempt.agent_id] = "trace_mismatch"
                return False
            reason = _transfer_refusal(spec, number,
                                       facts.get(attempt.agent_id, {}))
            if reason:
                refused[attempt.agent_id] = reason
            return not reason

        taken: list[tuple[Any, Any]] = []
        with _TURN_LOCK:
            for coordinator in _FAILOVERS.values():
                if coordinator.checking and coordinator.status().get("waiting"):
                    for agent_id in coordinator.status()["waiting"]:
                        refused.setdefault(agent_id, "selector_running")
                taken += [(coordinator, attempt)
                          for attempt in coordinator.take_parked(eligible)]
        moved = {attempt.agent_id for _coordinator, attempt in taken}
        for agent_id in facts:
            if agent_id not in moved:
                # A process ran for it (or it is mid-launch): its own
                # recovery continues it; the handoff waits.
                refused.setdefault(agent_id, "process_ran")
        if not taken:
            return {"transferred": [], "refused": refused}
        con = db.conn()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                for _coordinator, attempt in taken:
                    spec = attempt.work[0]
                    if spec.queue_id:
                        # Its own queued row: take it over, never a second row.
                        turn_queue.take_over_at_head(
                            spec.queue_id, agent_id=spec.agent_id,
                            trace_id=spec.trace_id,
                            prompt_admission_id=spec.prompt_admission_id)
                        closed_ids = [row[0] for row in con.execute(
                            "SELECT turn_id FROM turns WHERE agent_id=? AND trace_id=? AND ended_at IS NULL",
                            (spec.agent_id, spec.trace_id)).fetchall()]
                        if turn_lifecycle.close_turns_for_trace(spec.agent_id, spec.trace_id) != len(closed_ids):
                            raise RuntimeError("handoff frontier close mismatch")
                        turn_lifecycle.transition(
                            spec.agent_id, TurnEvent.UPDATE_HANDOFF, {
                                "reason": "update_handoff", "trace_id": spec.trace_id,
                                "message": UPDATE_HOLD_HEADLINE,
                                **({"dispatch_not_started": True,
                                    "dispatch_not_started_through_turn_id": max(closed_ids)}
                                   if closed_ids else {})})
                        continue
                    turn_queue.park_at_head(
                        queue_id=f"{RECOVERY_PARK_PREFIX}-{spec.trace_id}",
                        agent_id=spec.agent_id, session=spec.session,
                        text=spec.text, trace_id=spec.trace_id,
                        client_msg_id=spec.client_msg_id,
                        synthesize_audio=spec.synthesize_audio,
                        origin=spec.origin, sender_agent_id=spec.sender_agent_id,
                        prompt_admission_id=spec.prompt_admission_id)
                    closed_ids = [row[0] for row in con.execute(
                        "SELECT turn_id FROM turns WHERE agent_id=? AND trace_id=? AND ended_at IS NULL",
                        (spec.agent_id, spec.trace_id)).fetchall()]
                    if turn_lifecycle.close_turns_for_trace(spec.agent_id, spec.trace_id) != len(closed_ids):
                        raise RuntimeError("handoff frontier close mismatch")
                    # Not busy (a crash leaves no "interrupted" turn behind)
                    # and not an outcome: the turn has not run yet.
                    turn_lifecycle.transition(
                        spec.agent_id, TurnEvent.UPDATE_HANDOFF, {
                            "reason": "update_handoff", "trace_id": spec.trace_id,
                            "message": UPDATE_HOLD_HEADLINE,
                            **({"dispatch_not_started": True,
                                "dispatch_not_started_through_turn_id": max(closed_ids)}
                               if closed_ids else {})})
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001 - the work stays parked and blocking
            log_exception("drainRecoveryTransferFail", exc)
            by_pool: dict[int, tuple[Any, list]] = {}
            for coordinator, attempt in taken:
                by_pool.setdefault(id(coordinator), (coordinator, []))[1].append(attempt)
                refused[attempt.agent_id] = "persist_failed"
            parked_again = []
            for coordinator, attempts in by_pool.values():
                parked_again += coordinator.restore(attempts)
            # Only work still parked waits; a resumed turn is running.
            for attempt in parked_again:
                self._pause_for_account(attempt.work[0])
            return {"transferred": [], "refused": refused}
        transferred = []
        orphaned = []
        with _TURN_LOCK:
            for _coordinator, attempt in taken:
                spec = attempt.work[0]
                row_id = spec.queue_id or f"{RECOVERY_PARK_PREFIX}-{spec.trace_id}"
                if not _SLOTS.release(spec.agent_id, spec.trace_id):
                    orphaned.append((spec, row_id))
                    continue
                pool = next((name for name, value in _FAILOVERS.items()
                             if value is _coordinator), "")
                if pool:
                    self.handed_over.setdefault(pool, set()).add(spec.agent_id)
                if spec.queue_id:
                    # Its own row, parked for the handoff; a lifted fence
                    # makes it queued again before held work starts.
                    held = replace(spec, park_id="", bypass_pause=False,
                                   drain_persisted=True, taken_over=True)
                else:
                    held = replace(spec, queue_id="", park_id=row_id,
                                   bypass_pause=False, drain_persisted=True)
                _SLOTS.requeue_front(spec.agent_id, held)
                transferred.append(spec.agent_id)
        for spec, row_id in orphaned:
            # Unreachable while the seal holds the admission lock; never
            # leave a row that would run work a new owner replaced.
            if spec.queue_id:
                turn_queue.requeue_rows([row_id])
            else:
                turn_queue.unpark(row_id)
            log("drainRecoveryTransferLost",
                f"agent={spec.agent_id} trace={spec.trace_id}")
        if transferred:
            log("drainRecoveryTransferred",
                f"agents={','.join(sorted(transferred))}")
            refresh_hold_headlines()
        return {"transferred": sorted(transferred), "refused": refused}
