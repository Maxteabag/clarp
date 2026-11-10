"""HandoffMixin: methods moved out of lib/turn_dispatch.py::TurnDispatchService by tools/refactor/s3_apply.py
(group "turn_handoff"). Generated: change the recipe, not this file."""

from __future__ import annotations

from dataclasses import replace
from . import agents as agents_db
from . import error_classify, eventlog
from . import turn_queue
from . import native_rotation_admission as rotation_admission
from . import turn_lifecycle
from . import events
from .turn_lifecycle import TurnEvent
from .turn_slots import defer_on
from .log import log, log_exception
from .protocol import AgentState
from .turn_dispatch_state import _TURN_LOCK, _SLOTS, account_failover, _INFLIGHT, _CLAIMED_AT, _launching
from .turn_command import _TurnSpec, _stop_holds, _park, DispatchError, JanitorDispatchError, GoalWakeDispatchError, _validate_goal_dispatch, _validate_janitor_target, _recovered_janitor_run, _remove_queued_run, _ADMISSION_REFUSED, _is_call_opening, _backend_invoked
from .turn_stop import _record_janitor_cancelled


class HandoffMixin:
    def _discard_fenced_janitor(self, spec: _TurnSpec) -> None:
        _remove_queued_run(spec.queue_id)
        with _TURN_LOCK:
            if _SLOTS.owns(spec.agent_id, spec.trace_id):
                defer_on(_TURN_LOCK, lambda: _record_janitor_cancelled(
                    self.ctx, spec.agent_id, spec.session, spec.trace_id))
        self._finish_turn(spec)

    def _finish_turn(self, spec: _TurnSpec) -> None:
        """A turn reached a terminal state. If a message queued behind it, take
        over the in-flight slot and spawn it; otherwise free the slot. Guarded
        by trace so a duplicate terminal callback (or a superseded turn) is a
        no-op."""
        if spec.park_id:
            # A dropped or finished send needs no park row to survive restarts.
            turn_queue.unpark(spec.park_id)
        self._finish_slot(spec.agent_id, spec.trace_id, spec.backend)

    def _finish_slot(self, agent_id: str, trace_id: str, backend: str) -> None:
        with _TURN_LOCK:
            if _SLOTS.get(agent_id) != trace_id:
                return  # not the current turn — already drained / superseded
            account_failover(backend).discard(agent_id, trace_id)
            next_spec, selected = rotation_admission.select_next(agent_id, expected=trace_id)
            if next_spec is None:
                return
            # The handover is decided; the spawn runs once no thread waits on
            # this lock (a terminal callback may hold it around this call).
            rotation_admission.defer_selected(
                agent_id, next_spec, selected, lambda: self._spawn_next(agent_id, next_spec))

    def _spawn_next(self, agent_id: str, next_spec: _TurnSpec) -> None:
        try:
            self._resume_and_spawn(agent_id, next_spec)
        except Exception as exc:  # noqa: BLE001 - never fail the releasing caller
            log_exception("queuedSpawnFail", exc, detail=next_spec.session)

    def _resume_and_spawn(self, agent_id: str, next_spec: _TurnSpec) -> None:
        """Spawn a queued spec that just took over the in-flight slot.

        A failure before the backend starts gives the slot back and puts the
        spec back at the head of the queue. Raising out of here used to leave
        the slot claimed and "spawning" with no process, which every leak check
        exempts: Pebble (2026-10-10 07:26) had its handoff's INSERT INTO turns
        fail on "database is locked" and 18 messages queued behind nothing.
        """
        phase = {"value": "prepare"}
        with _launching(agent_id, next_spec.trace_id):
            try:
                self._resume_and_spawn_claimed(agent_id, next_spec, phase)
            except Exception as exc:  # noqa: BLE001 - the slot must not strand
                if phase["value"] == "launched":
                    # The backend owns the turn; its callbacks free the slot.
                    log_exception("queuedSpawnBookkeepingFail", exc,
                                  detail=next_spec.session)
                    return
                self._requeue_failed_handoff(agent_id, next_spec, exc,
                                             phase=phase["value"])

    def _requeue_failed_handoff(self, agent_id: str, spec: _TurnSpec,
                                error: BaseException, *, phase: str) -> None:
        log_exception("queuedSpawnFail", error, detail=spec.session)
        if phase == "spawning" and self._agent_has_live_turn(agent_id, spec.backend):
            # The launch may have started a process before it failed; that
            # process owns the slot and releases it when it ends.
            log("queuedSpawnFailLive",
                f"agent={agent_id} trace={spec.trace_id} — a process is live; "
                f"slot kept")
            return
        self._release_unsent_opening(spec, error)
        retry = replace(spec, handoff_failures=spec.handoff_failures + 1)
        with _TURN_LOCK:
            if not _SLOTS.owns(agent_id, spec.trace_id):
                return  # Stop or a newer owner already took the slot over
            _SLOTS.release(agent_id, spec.trace_id)
            _SLOTS.requeue_front(agent_id, retry)
        delay = min(60.0, float(2 ** (retry.handoff_failures - 1)))
        log("queuedSpawnRequeued",
            f"agent={agent_id} trace={spec.trace_id} failures="
            f"{retry.handoff_failures} retry_in={delay:g}s — launch failed before "
            f"the backend started; slot released, message kept at the queue head")
        eventlog.emit("server", "queuedSpawnRequeued", context=spec.context,
                      detail={"error": str(error)[:200],
                              "failures": retry.handoff_failures})
        try:
            turn_lifecycle.close_turns_for_trace(agent_id, spec.trace_id)
        except Exception as exc:  # noqa: BLE001 - the retry opens a new row
            log_exception("queuedSpawnCloseFail", exc, detail=spec.session)
        self.retry_scheduler(delay, lambda: self._retry_handoff(agent_id))

    def _retry_handoff(self, agent_id: str) -> None:
        """Hand a free slot to the head of the agent's queue."""
        with _TURN_LOCK:
            if _SLOTS.get(agent_id) or not _SLOTS.queue_depth(agent_id):
                return  # someone owns the slot; its finish drains the queue
            head, selected = rotation_admission.select_next(agent_id)
            if head is not None:
                rotation_admission.defer_selected(
                    agent_id, head, selected, lambda: self._resume_and_spawn(agent_id, head))

    def _hold_stopped_queue(self, agent_id: str, spec: _TurnSpec) -> None:
        """A Stop's pause holds ``spec``, which just took the slot over.

        Every spec the pause holds stays queued for Resume, in its order, in
        the durable queue that Resume recovers: a spec without a queue row
        gets one first, and older rows behind it move back after it, as the
        release drain orders them. Nothing is withdrawn. The first spec the
        pause does not hold starts; with none the slot is freed."""
        with _TURN_LOCK:
            later = (_SLOTS.take_queued(agent_id)
                     if _SLOTS.owns(agent_id, spec.trace_id) else [])
        taken = [spec, *later]
        kept: list[_TurnSpec] = []
        held: list[str] = []
        stuck: set[str] = set()
        reorder = False
        try:
            for index, item in enumerate(taken):
                if item is not spec and not _stop_holds(item):
                    kept.append(item)
                    continue
                row_id = item.queue_id or item.park_id
                if not row_id:
                    row_id = _park(item)
                    if not row_id:
                        # It cannot be made durable: memory keeps it, unstarted.
                        kept.append(item)
                        stuck.add(item.trace_id)
                        continue
                    # The newest row: older rows behind it move back after it.
                    reorder = True
                elif reorder:
                    turn_queue.move_to_back(row_id)
                if not item.queue_id:
                    turn_queue.requeue_rows([row_id])
                    # From here its queue row is the work; a copy put back
                    # below must not run it a second time beside that row.
                    taken[index] = replace(item, queue_id=row_id, park_id="")
                held.append(row_id)
        except Exception as exc:  # noqa: BLE001 - nothing taken may be lost
            # A failed write ("database is locked") leaves the split unknown:
            # everything taken goes back, in order, and the head is tried again
            # later, held or started then.
            log_exception("queuedHoldFail", exc, detail=spec.session)
            with _TURN_LOCK:
                if not _SLOTS.release(agent_id, spec.trace_id):
                    # A Stop took the slot meanwhile (see below).
                    taken = [replace(item, bypass_pause=False) for item in taken]
                _SLOTS.put_front(agent_id, taken)
            self.retry_scheduler(1.0, lambda: self._retry_handoff(agent_id))
            return
        log("queuedHeldByStop",
            f"agent={agent_id} rows={','.join(held) or '∅'} kept={len(kept)} "
            f"— queued before a Stop; waits for Resume")
        with _TURN_LOCK:
            if not _SLOTS.owns(agent_id, spec.trace_id):
                # A Stop took the slot while the queue was out of memory, so
                # its snapshot missed these: it holds them as it holds all
                # work queued before it. A free slot hands them on now.
                kept = [replace(item, bypass_pause=False) for item in kept]
                if kept and not _SLOTS.get(agent_id):
                    defer_on(_TURN_LOCK, lambda: self._retry_handoff(agent_id))
            _SLOTS.put_front(agent_id, kept)
            next_spec, selected = rotation_admission.select_next(agent_id, expected=spec.trace_id)
            if next_spec is not None and next_spec.trace_id in stuck:
                _SLOTS.release(agent_id, next_spec.trace_id)
                _SLOTS.requeue_front(agent_id, next_spec)
                rotation_admission.finish_selected(agent_id, selected)
            elif next_spec is not None:
                rotation_admission.defer_selected(
                    agent_id, next_spec, selected, lambda: self._spawn_next(agent_id, next_spec))

    def _resume_and_spawn_claimed(self, agent_id: str, next_spec: _TurnSpec,
                                  phase: dict) -> None:
        """The prior owner (a finished turn, or a closed terminal) may have
        created/advanced the backend session, so re-resolve it before
        spawning."""
        if next_spec.queue_id:
            durable = turn_queue.get(next_spec.queue_id)
            if durable is None:
                # It was removed while waiting. Do not execute the stale
                # in-memory copy; advance to any later queued turn.
                self._finish_turn(next_spec)
                return
            next_spec = replace(
                next_spec,
                text=str(durable["text"]),
                client_msg_id=str(durable["client_msg_id"]),
                origin=str(durable["origin"]),
                sender_agent_id=str(durable["sender_agent_id"]),
                prompt_admission_id=str(durable["prompt_admission_id"]),
            )
        if _stop_holds(next_spec):
            self._hold_stopped_queue(agent_id, next_spec)
            return
        try:
            agent = agents_db.get_by_agent_id(agent_id) or {}
            _validate_janitor_target(agent, next_spec.janitor_run_id, next_spec.trace_id)
            if next_spec.janitor_run_id:
                if not next_spec.queue_id or _recovered_janitor_run(durable) != next_spec.janitor_run_id:
                    raise JanitorDispatchError(409, "Maintenance queue ownership changed")
        except JanitorDispatchError:
            self._discard_fenced_janitor(next_spec)
            return
        bsid = (agents_db.live_backend_session(agent_id)
                or next_spec.backend_session_id)
        next_spec = replace(next_spec, backend_session_id=bsid,
                            is_new_session=not bsid)
        try:
            _validate_goal_dispatch(agent, next_spec.client_msg_id, native_session_id=bsid)
        except GoalWakeDispatchError as exc:
            if exc.permanent:
                if next_spec.queue_id:
                    self._retire_goal_queue(durable, exc)
                self._finish_turn(next_spec)
            else:
                _SLOTS.release(agent_id, next_spec.trace_id)
                self.retry_scheduler(1.0, self.recover_queued)
            return
        if not self._call_opening_claimed(next_spec):
            self._finish_turn(next_spec)
            return
        if _is_call_opening(next_spec):         # its admission mark, before its live trace
            next_spec = replace(next_spec, s_open=turn_lifecycle.state_generation(agent_id))
        agents_db.set_trace_for_session(next_spec.session, next_spec.trace_id)
        try:
            if next_spec.queue_id:
                # Queue admission is intentionally invisible. Materialize the
                # ordinary user turn only at the moment execution begins.
                self._record_user_message(next_spec)
            turn_lifecycle.open_turn(
                agent_id=next_spec.agent_id, source="pwa",
                trace_id=next_spec.trace_id,
                synthesize_audio=next_spec.synthesize_audio)
            phase["value"] = "spawning"
            spawned = self._spawn_attempt(next_spec, attempt=1)
            if spawned is _ADMISSION_REFUSED:
                return                  # settled by the final call-open check; never spawned
            if not spawned:
                self._release_unsent_opening(next_spec, None)
                return
            phase["value"] = "launched"
            if next_spec.park_id:
                # It launched; the durable park row has done its job. A failed
                # launch keeps it, so a restart still finds the send.
                turn_queue.unpark(next_spec.park_id)
            self._mark_spawned(next_spec)
        except JanitorDispatchError:
            self._discard_fenced_janitor(next_spec)
        except GoalWakeDispatchError as exc:
            if exc.permanent:
                if next_spec.queue_id:
                    self._retire_goal_queue(durable, exc)
                self._finish_turn(next_spec)
            else:
                _SLOTS.release(agent_id, next_spec.trace_id)
                self.retry_scheduler(1.0, self.recover_queued)
        except DispatchError as e:
            if next_spec.queue_id and _is_call_opening(next_spec) and _backend_invoked(e):
                # The opening's one launch reached the backend and then failed: the same
                # fenced end as any post-launch failure, never requeued as unsent.
                self._end_launched_opening(next_spec, error_classify.RUNNER_EXIT, str(e), 1)
                try:
                    turn_lifecycle.close_turns_for_trace(next_spec.agent_id, next_spec.trace_id)
                except Exception as close_error:  # noqa: BLE001
                    log_exception("callOpenTurnCloseFail", close_error, detail=next_spec.session)
                return
            if not next_spec.queue_id:
                # Legacy automatic terminal queue is memory-only; preserve its
                # existing behavior and continue to the next waiting spec.
                log_exception("queuedSpawnFail", e, detail=next_spec.session)
                self._finish_turn(next_spec)
                return
            # Keep the durable head and retry it before later queue entries.
            # The client already received queued=true, so dropping it here
            # would silently lose acknowledged work.
            self._requeue_failed_handoff(agent_id, next_spec, e, phase="prepare")

    def _mark_spawned(self, spec: _TurnSpec) -> None:
        with _TURN_LOCK:
            recovering = account_failover(spec.backend).attempts.get(spec.agent_id)
            if (recovering and recovering.trace_id == spec.trace_id
                    and recovering.state.get("account_recovery")):
                return
            if _INFLIGHT.get(spec.agent_id) == spec.trace_id:
                _CLAIMED_AT.pop(spec.agent_id, None)
        if spec.janitor_run_id:
            from . import janitors
            try:
                janitors.mark_started(spec.janitor_run_id)
            except janitors.JanitorError:
                # Pause may fence the run immediately after backend spawn.
                # Its exact-run cancellation owns teardown; do not revive it.
                return
        turn_queue.mark_started(spec.queue_id)
        if spec.origin == "oracle":
            from . import oracle_delegations
            oracle_delegations.mark_started_for_trace(spec.trace_id)
        if spec.queue_id:
            self._broadcast_queue_state(spec, started=True)
        try:
            # The causal baseline of this write, read before the check below:
            # a terminal row of this trace committed after it (the provider
            # finished meanwhile, in another process) refuses the write (TP-N2).
            begun_at = turn_lifecycle.causal_baseline(spec.agent_id)
            # A very fast backend may complete before spawn_turn returns. Do
            # not overwrite its terminal state with a late THINKING record.
            latest = agents_db.latest_state(spec.agent_id)
            if latest and latest.get("kind") in turn_lifecycle.TERMINAL:
                detail = latest.get("detail") or {}
                if (detail.get("trace_id") == spec.trace_id
                        or agents_db.get_trace(spec.agent_id) != spec.trace_id):
                    return
            try:
                turn_lifecycle.transition(
                    spec.agent_id,
                    TurnEvent.SPAWN_STARTED,
                    {
                        "source": "pwa",
                        "dispatch": spec.backend,
                        "origin": spec.origin,
                        "trace_id": spec.trace_id,
                        "backend_session_id": spec.backend_session_id,
                    },
                    begun_at=begun_at,
                )
            except turn_lifecycle.TransitionNotWritten:
                # The turn already ended (or was stopped): no 'thinking'
                # frame for it either, or clients would show it busy.
                return
            except turn_lifecycle.IllegalTransition:
                pass                    # as try_transition: logged and counted
            if getattr(self.ctx, "stream", None) is not None:
                events.broadcast(self.ctx.stream, events.agent_state(
                    session=spec.session,
                    agent_id=spec.agent_id,
                    kind=AgentState.THINKING,
                    trace_id=spec.trace_id,
                    client_msg_id=spec.client_msg_id,
                    queue_started=bool(spec.queue_id),
                    queue_remaining=turn_queue.pending_count(spec.agent_id),
                ))
        except Exception as e:
            log_exception("spawnStateFail", e, detail=spec.session)

    def _broadcast_queue_state(self, spec: _TurnSpec, *, started: bool) -> None:
        if getattr(self.ctx, "stream", None) is None:
            return
        queue_state = turn_queue.state(spec.agent_id)
        events.broadcast(self.ctx.stream, events.queue_updated(
            session=spec.session,
            agent_id=spec.agent_id,
            client_msg_id=spec.client_msg_id,
            queue_depth=queue_state["count"],
            queue_paused=queue_state["paused"],
            queue_started=started,
            queue_revision=queue_state["revision"],
        ))
