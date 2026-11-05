"""RecoveryMixin: methods moved out of lib/turn_dispatch.py::TurnDispatchService by tools/refactor/s3_apply.py
(group "turn_recovery"). Generated: change the recipe, not this file."""

from __future__ import annotations

from . import agents as agents_db
from . import db, eventlog
from . import turn_queue
from . import recovery_backoff, turn_lifecycle
from . import events
from .turn_lifecycle import TurnEvent
from .log import log, log_exception
from .turn_dispatch_state import _TURN_LOCK, _SLOTS, account_failover, _INFLIGHT, _RECOVERY_LOCK, _RECOVERY_BACKOFF, _NO_LOCK, LEAKED_SLOT_GRACE_MS, ORPHAN_SPAWN_GRACE_S, REPAIR_SPAWN_GRACE_S, _launch_running
from .turn_command import _TurnSpec, JanitorDispatchError, GoalWakeDispatchError, _validate_goal_dispatch, _recovered_janitor_run, _REGENERATED_ORIGINS, _remove_queued_run, DispatchCommand


class RecoveryMixin:
    def recover_queued(self) -> int:
        """Re-admit durable explicit queues after a server restart."""
        runtime = getattr(self.ctx, "runtime_client", None)
        if runtime is not None:
            return int(runtime.recover_queued())
        if not _RECOVERY_LOCK.acquire(blocking=False):
            return 0
        try:
            return self._recover_queued_locked()
        finally:
            _RECOVERY_LOCK.release()

    def _rehydrate_once(self) -> None:
        """First recovery after boot: rebuild the slot view from the durable
        rows. An open turns row whose process this runtime can still see
        owns its slot again; Stop-parked sends become queued work (the
        barrier that parked them died with the previous process)."""
        if not _SLOTS.first_rehydration():
            return
        try:
            # A listen-first claim left by an earlier process: a process
            # boundary is never proof of what it sent, whatever its age.
            from . import agent_rings
            agent_rings.resolve_stale_claims(None)
        except Exception as exc:  # noqa: BLE001 - recovery must still run
            log_exception("callOpenBootResolveFail", exc)

        def live(agent_id: str, trace_id: str) -> bool:
            agent = agents_db.get_by_agent_id(agent_id)
            if not agent or agents_db.get_trace(agent_id) != trace_id:
                return False
            try:
                return bool(self.backends.active_handles(
                    self.backends.normalize(agent.get("backend")), agent_id))
            except Exception:  # noqa: BLE001
                return False

        try:
            result = _SLOTS.rehydrate(live=live)
        except Exception as exc:  # noqa: BLE001 - recovery must still run
            log_exception("turnSlotsRehydrateFail", exc)
            return
        if result["adopted"] or result["requeued_parked"]:
            log("turnSlotsRehydrated",
                f"adopted={len(result['adopted'])} "
                f"requeued_parked={result['requeued_parked']}")

    def _recover_queued_locked(self) -> int:
        from . import task_goal_recovery
        self._rehydrate_once()
        from . import agent_rings
        examined = agent_rings.resolve_stale_claims(db.now_ms() - 30_000)
        turn_queue.reset_stale_claims(exclude=examined)
        recovered = 0
        deferred = False
        # Agents whose head row is backing off or failed in this pass: their
        # later rows wait too, so a broken agent keeps its order and costs one
        # launch per step, not one per queued row.
        held: set[str] = set()
        next_try = None
        # Rows re-admitted by this pass, per agent in queue order: the other
        # half of the drain's order evidence (``drainHeldOrder``).
        readmitted: dict[str, list[str]] = {}
        for row in turn_queue.pending():
            if row["agent_id"] in held:
                continue
            agent = agents_db.get_by_agent_id(row["agent_id"])
            # Permanent fences are independent of paused/busy execution. Retire
            # obsolete automatic work before those gates can hide it forever.
            try:
                _validate_goal_dispatch(agent or {}, row["client_msg_id"])
            except GoalWakeDispatchError as exc:
                if exc.permanent:
                    self._retire_goal_queue(row, exc)
                continue
            except Exception as exc:
                log_exception("queuedRecoveryFail", exc, detail=row["session"])
                continue
            # A Stop's leftover pause holds user and peer messages, but the
            # current wake of an active goal passed the goal fence above and
            # must not wait behind it: the goal would see its own queued wake
            # as live work and never wake again.
            goal_wake = (
                str(row["client_msg_id"]).startswith(task_goal_recovery.PREFIX)
                and task_goal_recovery.allows_paused_queue(row["client_msg_id"]))
            # Work the release drain held past a pause it was never subject to.
            pause_exempt = goal_wake or bool(row.get("allow_paused"))
            if turn_queue.is_paused(row["agent_id"]) and not pause_exempt:
                continue
            if agent and self.backends.active_handles(
                    self.backends.normalize(agent.get("backend")), row["agent_id"]):
                deferred = True
                continue
            already_memory_queued = _SLOTS.has_queued(
                row["agent_id"],
                lambda spec, queue_id=row["queue_id"]: spec.queue_id == queue_id)
            if already_memory_queued:
                continue
            if row["origin"] in _REGENERATED_ORIGINS:
                # A background tick queued behind a turn this process no longer
                # remembers decided about a state that has moved on, and its
                # janitor check refuses readmission. Its scheduler decides
                # again; kept, the row would hold off every later tick (AUT-05).
                _remove_queued_run(str(row["queue_id"]))
                log("backgroundTickDropped",
                    f"agent={row['agent_id']} origin={row['origin']} "
                    f"queue={row['queue_id']}")
                continue
            backoff = _RECOVERY_BACKOFF.hold(
                row["agent_id"], str(row["queue_id"]), self.now())
            if backoff is not None:
                held.add(row["agent_id"])
                if not backoff.parked:
                    next_try = min(next_try or backoff.retry_at, backoff.retry_at)
                continue
            try:
                self.submit(DispatchCommand(
                    text=row["text"], requested_session=row["session"],
                    trace_id=row["trace_id"],
                    synthesize_audio=bool(row["synthesize_audio"]),
                    forced_session=row["session"],
                    client_msg_id=row["client_msg_id"], origin=row["origin"],
                    sender_agent_id=row["sender_agent_id"],
                    prompt_admission_id=row["prompt_admission_id"],
                    queue_if_busy=True, skip_admission=True,
                    durable_queue_id=row["queue_id"],
                    allow_paused_queue=pause_exempt,
                    janitor_run_id=_recovered_janitor_run(row),
                ))
                recovered += 1
                readmitted.setdefault(row["agent_id"], []).append(str(row["queue_id"]))
                _RECOVERY_BACKOFF.clear(row["agent_id"])
            except JanitorDispatchError as exc:
                _remove_queued_run(str(row["queue_id"]))
                log("janitorQueueRejected", str(exc))
            except GoalWakeDispatchError as exc:
                if exc.permanent:
                    self._retire_goal_queue(row, exc)
            except Exception as exc:  # keep ledger row for the next retry/restart
                held.add(row["agent_id"])
                backoff = self._recovery_failed(row, exc)
                if not backoff.parked:
                    next_try = min(next_try or backoff.retry_at, backoff.retry_at)
        for agent_id, rows in sorted(readmitted.items()):
            log("queuedRecoveredOrder", f"agent={agent_id} n={len(rows)} "
                                        f"rows={','.join(rows)}")
        if deferred or turn_queue.claimed_count() > 0:
            self.retry_scheduler(1.0, self.recover_queued)
        if next_try is not None and _RECOVERY_BACKOFF.claim_wake(next_try):
            def wake(at=next_try):
                _RECOVERY_BACKOFF.wake_fired(at)
                self.recover_queued()
            self.retry_scheduler(max(0.0, next_try - self.now()), wake)
        return recovered

    def _recovery_failed(self, row, exc: BaseException) -> recovery_backoff.Backoff:
        """Back off one agent's queued recovery after a launch that failed
        before its backend started; park it after repeated identical
        failures. The queued rows stay exactly as they are."""
        agent_id = str(row["agent_id"])
        backoff = _RECOVERY_BACKOFF.failed(
            agent_id, str(row["queue_id"]), exc, self.now())
        if backoff.failures == 1:
            log_exception("queuedRecoveryFail", exc, detail=row["session"])
        if backoff.parked:
            log("queuedRecoveryParked",
                f"session={row['session']} queue={row['queue_id']} "
                f"failures={backoff.failures} error={backoff.error!r}")
            message = (f"Queued messages are waiting: the backend failed to start "
                       f"{backoff.failures} times ({backoff.error}). Send a message "
                       f"or retry to try again.")
            try:
                turn_lifecycle.try_transition(agent_id, TurnEvent.UNLAUNCHED, {
                    "trace_id": str(row["trace_id"]), "dispatch_not_started": True,
                    "queued_recovery_parked": True, "failures": backoff.failures,
                    "error": backoff.error, "message": message})
            except Exception as state_error:  # noqa: BLE001 - the park still holds
                log_exception("queuedRecoveryParkStateFail", state_error,
                              detail=row["session"])
        else:
            log("queuedRecoveryBackoff",
                f"session={row['session']} queue={row['queue_id']} "
                f"failures={backoff.failures} "
                f"retry_in={backoff.retry_at - self.now():.0f}s")
        return backoff

    def clear_recovery_backoff(self, agent_id: str, *, reason: str) -> None:
        """A new send, explicit send or repair asks for the agent again: its
        queued recovery tries at once."""
        cleared = _RECOVERY_BACKOFF.clear(agent_id)
        if cleared is None:
            return
        log("queuedRecoveryResumed",
            f"agent={agent_id} reason={reason} failures={cleared.failures} "
            f"parked={int(cleared.parked)}")
        self.retry_scheduler(0.0, self.recover_queued)

    def _retire_goal_queue(self, row, rejection) -> None:
        if not turn_queue.cancel(str(row["queue_id"])):
            return
        log("goalQueueRetired", f"session={row['session']} queue={row['queue_id']} reason={rejection.code}")
        if getattr(self.ctx, "stream", None) is not None:
            state = turn_queue.state(row["agent_id"])
            events.broadcast(self.ctx.stream, events.queue_updated(
                session=row["session"], agent_id=row["agent_id"],
                client_msg_id=row["client_msg_id"], queue_depth=state["count"],
                queue_paused=state["paused"], queue_started=False,
                queue_revision=state["revision"]))

    def _release_superseded(self, spec: _TurnSpec) -> None:
        """A superseded turn records no state, but its slot is a separate
        question: the agent's trace also moves when a hook inside this very
        process mints one (UserPromptSubmit after the trace TTL), and then no
        newer turn holds the slot. Only the trace holding the slot may give it
        back, which is exactly what _finish_turn checks."""
        with _TURN_LOCK:
            owned = _SLOTS.owns(spec.agent_id, spec.trace_id)
        if owned:
            log("supersededTurnReleased",
                f"agent={spec.agent_id} trace={spec.trace_id} "
                f"agent_trace={agents_db.get_trace(spec.agent_id) or '∅'} — "
                f"trace moved on but this turn still held the slot; releasing")
        self._finish_turn(spec)

    def release_leaked_slots(
        self, *, grace_ms: int = LEAKED_SLOT_GRACE_MS,
        agent_id: str | None = None,
        orphan_grace_s: float = ORPHAN_SPAWN_GRACE_S,
    ) -> dict[str, str]:
        """Release every slot no process owns once its turn has settled.

        A turn whose terminal callback never ran (or ran and bailed) keeps its
        slot forever, and a held slot blocks the runtime's idle handoff. A
        slot is leaked when the agent has no live process, terminal or spawn,
        and its newest state is terminal for at least ``grace_ms`` (a finishing
        turn records DONE just before it releases). A slot still marked
        spawning counts only when it is orphaned (``_orphaned_spawn``); the
        spec it held is then gone from memory, so durable queue recovery runs
        after the release to pick its row up again. Queued work behind it takes
        the slot as after a normal finish. ``agent_id`` limits the check to
        one agent (the Host's reconcile asks per agent). Returns
        {agent_id: released trace}.
        """
        released: dict[str, str] = {}
        now = db.now_ms()
        only = agent_id
        for agent_id, trace_id in _SLOTS.snapshot()["active"].items():
            if only is not None and agent_id != only:
                continue
            try:
                agent = agents_db.get_by_agent_id(agent_id)
                if agent is None:
                    log("leakedSlotUnknownAgent",
                        f"agent={agent_id} trace={trace_id} — slot held by an "
                        f"agent with no row; left alone")
                    continue
                backend = self.backends.normalize(agent.get("backend"))
                state = agents_db.latest_state(agent_id) or {}
                if (state.get("kind") not in turn_lifecycle.TERMINAL
                        or now - int(state.get("ts") or now) < grace_ms):
                    continue
                orphaned = _SLOTS.is_spawning(agent_id)
                if orphaned and not self._orphaned_spawn(
                        agent_id, trace_id, backend, min_age_s=orphan_grace_s):
                    continue
                if self._agent_has_live_turn(agent_id, backend):
                    continue
                external = getattr(self.backends, "external_live_work", None)
                if external is not None and external(backend, agent_id):
                    continue
                if not _SLOTS.owns(agent_id, trace_id):
                    continue
                log("leakedSlotReleased",
                    f"agent={agent_id} trace={trace_id} state={state['kind']} "
                    f"settled_ms_ago={now - int(state['ts'])} "
                    f"orphaned_spawn={int(orphaned)} — slot held with "
                    f"no live process after its turn settled; releasing")
                eventlog.emit("server", "leakedSlotReleased", detail={
                    "agent_id": agent_id, "trace_id": trace_id,
                    "state": state["kind"], "orphaned_spawn": orphaned})
                self._finish_slot(agent_id, trace_id, backend)
                released[agent_id] = trace_id
                if orphaned:
                    self.retry_scheduler(1.0, self.recover_queued)
            except Exception as exc:  # noqa: BLE001 - check the other slots
                log_exception("leakedSlotCheckFail", exc, detail=agent_id)
        self._start_ownerless_queues(only)
        return released

    def _start_ownerless_queues(self, only: str | None) -> None:
        """Hand a free slot to work queued in memory behind no owner.

        Nothing else starts such a queue but a new send, and the idle drain
        refuses while it is non-empty: an older runtime freed failed spawns'
        slots and kept the messages behind them (Nadia and psa-billing,
        2026-10-10 09:24)."""
        if _SLOTS.fenced:
            return  # the release drain holds them; lifting it starts them
        for agent_id, depth in _SLOTS.snapshot()["queued"].items():
            if only is not None and agent_id != only:
                continue
            if _SLOTS.get(agent_id):
                continue  # an owner (turn, terminal or Stop) drains it
            log("ownerlessQueueStarted",
                f"agent={agent_id} depth={depth} — queued work with a free "
                f"slot and no owner; starting its head")
            try:
                self._retry_handoff(agent_id)
            except Exception as exc:  # noqa: BLE001 - check the other queues
                log_exception("ownerlessQueueStartFail", exc, detail=agent_id)

    @staticmethod
    def _orphaned_spawn(agent_id: str, trace_id: str, backend: str, *,
                        min_age_s: float = ORPHAN_SPAWN_GRACE_S) -> bool:
        """A spawning slot nobody is launching: no launch is running for it,
        account recovery does not hold it, and it was claimed long ago."""
        if _launch_running(agent_id):
            return False
        attempt = account_failover(backend).attempts.get(agent_id)
        if attempt is not None and attempt.trace_id == trace_id:
            return False
        age = _SLOTS.claim_age(agent_id)
        return age is not None and age >= min_age_s

    def repair_slot(self, agent_id: str, *, actor: str, reason: str,
                    actor_verified: bool = False) -> dict:
        """Give back a slot no process, launch, account recovery, terminal or
        Stop owns, and let queued work run. Not a Stop: nothing is
        interrupted and the queue's and goals' pause state is left exactly as
        it was. The actor and reason are logged and kept on the agent's state
        as an audit note."""
        reason = (reason or "").strip()[:200]
        actor = (actor or "").strip()[:80] or "user"
        if not reason:
            raise ValueError("a repair needs a reason")
        self.clear_recovery_backoff(agent_id, reason="repair")
        released = self.release_leaked_slots(
            agent_id=agent_id, grace_ms=0, orphan_grace_s=REPAIR_SPAWN_GRACE_S)
        trace = released.get(agent_id, "")
        log("slotRepair",
            f"agent={agent_id} actor={actor} verified={int(actor_verified)} "
            f"released={trace or '∅'} reason={reason!r}")
        eventlog.emit("server", "slotRepair", detail={
            "agent_id": agent_id, "actor": actor,
            "actor_verified": actor_verified, "reason": reason,
            "released_trace": trace})
        turn_lifecycle.try_transition(agent_id, TurnEvent.AUDIT_NOTED, {
            "source": "slot_repair", "repair_actor": actor,
            "repair_actor_verified": actor_verified,
            "repair_reason": reason, "released_trace": trace})
        self._retry_handoff(agent_id)
        return {"released_trace": trace,
                "queue_recovered": int(self.recover_queued() or 0)}

    def _superseded(self, spec: _TurnSpec, *, locked: bool = True) -> bool:
        """True if a newer turn has taken over this agent since `spec` was
        dispatched. A preempted/old turn's drain thread fires its terminal
        callback asynchronously — if we let it record INTERRUPTED/IDLE it would
        clobber the new turn's THINKING and leave the pill looking idle while
        the agent is actually working. The agent's current trace is the newest
        /send's; a stale turn carries an older one. Same-trace retries are NOT
        superseded (they share spec.trace_id), so they still record normally."""
        try:
            if spec.janitor_run_id:
                from . import janitors
                with (_TURN_LOCK if locked else _NO_LOCK):
                    if _INFLIGHT.get(spec.agent_id) != spec.trace_id:
                        return True
                    if not janitors.validate_dispatch(spec.session, spec.janitor_run_id, spec.trace_id):
                        return True
            current = agents_db.get_trace(spec.agent_id)
        except Exception:
            return bool(spec.janitor_run_id)
        return bool(current) and current != spec.trace_id

    def _preempt(self, *, agent_id: str, backend: str,
                 context: eventlog.EventContext) -> None:
        try:
            killed = self.backends.interrupt(backend, agent_id)
            if killed:
                eventlog.emit("server", "turnPreemptKilled", context=context,
                              detail={"killed": killed, "backend": backend})
        except Exception as e:
            log_exception("turnPreemptFail", e, detail=context.session or "")
