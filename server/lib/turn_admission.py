"""AdmissionMixin: methods moved out of lib/turn_dispatch.py::TurnDispatchService by tools/refactor/s3_apply.py
(group "turn_admission"). Generated: change the recipe, not this file."""

from __future__ import annotations

import contextlib
from dataclasses import replace
from . import agents as agents_db
from . import backends, db, error_classify, eventlog
from . import message_store, team_store, turn_queue
from . import launch_receipts, turn_lifecycle
from . import events
from .policies import admission as admission_policy
from .log import log, log_exception
from .protocol import SSEType
from .prompt_admissions import PromptAdmission
from . import prompt_admissions
from .send_service import SendTarget, resolve_send_target
from .turn_dispatch_state import _SLOTS, _RECOVERY_RESUMING_ORIGINS, _ADMISSION
from .turn_command import DispatchResult, _with_delivery, _resolve_llm, _TurnSpec, _pause_exempt_on_arrival, DispatchError, _refuse_oversized, JanitorDispatchError, UNCONFIRMED_DELIVERY, GoalWakeDispatchError, _validate_goal_dispatch, _janitor_run_active, _admission_facts, _admit, _UNLAUNCHED_MARKER_RETRY_SEC, _unlaunched_marked, _ADMISSION_REFUSED, _is_call_opening, _backend_invoked, DispatchCommand, _serialize_client_retry, _PreInvokeFailure, _mark_preinvoke_failure, _preinvoke_failure, _QueuedReceiptUnavailable
from .turn_stop import _existing_cwd


class AdmissionMixin:
    def dispatch(self, *, text: str, requested_session: str,
                 trace_id: str, synthesize_audio: bool = True,
                 forced_session: str = "",
                 routed_by_orchestrator: bool = False,
                 client_msg_id: str = "",
                 origin: str = "user",
                 sender_agent_id: str = "",
                 prompt_admission: PromptAdmission | None = None,
                 prompt_admission_id: str = "",
                 queue_if_busy: bool = False,
                 skip_admission: bool = False,
                 durable_queue_id: str = "",
                 unheard_audio_sessions: tuple[str, ...] = (),
                 allow_paused_queue: bool = False,
                 janitor_run_id: str = "") -> DispatchResult:
        """Keyword form of :meth:`submit`, for tests and ad-hoc callers.
        Production entry points build the :class:`DispatchCommand` themselves."""
        return self.submit(DispatchCommand(
            text=text, requested_session=requested_session,
            trace_id=trace_id, synthesize_audio=synthesize_audio,
            forced_session=forced_session,
            routed_by_orchestrator=routed_by_orchestrator,
            client_msg_id=client_msg_id, origin=origin,
            sender_agent_id=sender_agent_id,
            prompt_admission=prompt_admission,
            prompt_admission_id=prompt_admission_id,
            queue_if_busy=queue_if_busy, skip_admission=skip_admission,
            durable_queue_id=durable_queue_id,
            unheard_audio_sessions=tuple(unheard_audio_sessions),
            allow_paused_queue=allow_paused_queue,
            janitor_run_id=janitor_run_id))

    def submit(self, command: DispatchCommand) -> DispatchResult:
        """Run ``command`` where turns are owned: forward it unchanged over the
        runtime RPC, or admit and launch it here."""
        runtime = getattr(self.ctx, "runtime_client", None)
        if runtime is None:
            return _with_delivery(command, self._dispatch_owned_marked(command))
        try:
            send = getattr(runtime, "dispatch_command", None)
            result = (send(command) if send is not None
                      else runtime.dispatch(**command.as_kwargs()))
        except Exception as exc:
            from .runtime_bridge import RuntimeUnavailable
            if isinstance(exc, RuntimeUnavailable):
                raise DispatchError(503, str(exc)) from exc
            raise
        # The runtime's active map just changed; do not let a shared
        # status window older than this dispatch answer for it.
        invalidate = getattr(self.backends, "invalidate_runtime_status", None)
        if invalidate is not None:
            invalidate()
        return result

    @_serialize_client_retry
    def _dispatch_owned_marked(self, command: DispatchCommand) -> DispatchResult:
        """``_dispatch_owned``, whose agent is marked as admitting a send from
        its session binding until this call returns: the session is bound
        before the slot is claimed, and a snapshot's reconciler must see that
        as live work (TP-05b)."""
        outer = getattr(_ADMISSION, "stack", None)
        with contextlib.ExitStack() as stack:
            _ADMISSION.stack = stack
            try:
                return self._dispatch_owned(command)
            finally:
                _ADMISSION.stack = outer

    def _dispatch_owned(self, command: DispatchCommand) -> DispatchResult:
        text = command.text
        requested_session = command.requested_session
        trace_id = command.trace_id
        synthesize_audio = command.synthesize_audio
        forced_session = command.forced_session
        routed_by_orchestrator = command.routed_by_orchestrator
        client_msg_id = command.client_msg_id
        origin = command.origin
        sender_agent_id = command.sender_agent_id
        prompt_admission = command.prompt_admission
        prompt_admission_id = command.prompt_admission_id
        queue_if_busy = command.queue_if_busy
        skip_admission = command.skip_admission
        durable_queue_id = command.durable_queue_id
        unheard_audio_sessions = command.unheard_audio_sessions
        allow_paused_queue = command.allow_paused_queue
        janitor_run_id = command.janitor_run_id
        # The Janitor-demand authority is looked up before routing, as it
        # always was; admission_policy decides what the answer means.
        janitor_demand_valid = None
        if admission_policy.needs_janitor_demand_check(origin, client_msg_id):
            from .janitor_autonomy import validate_dispatch
            janitor_demand_valid = bool(
                validate_dispatch(requested_session, client_msg_id))
        early = admission_policy.before_routing(
            origin, client_msg_id, janitor_demand_valid)
        if early is not None:
            raise DispatchError(early.status, early.reason)
        # NB: the live session->trace mapping is set only when a turn actually
        # spawns (see below / _finish_turn), NOT here — a message that merely
        # queues behind a busy agent must not move the trace, or the running
        # turn's completion would look superseded and the queue would never
        # drain.
        eventlog.emit("server", "send", trace_id=trace_id,
                      session=requested_session,
                      detail={
                          "text": text,
                          "forced_session": forced_session,
                          "routed_by_orchestrator": routed_by_orchestrator,
                      })
        if forced_session:
            if not agents_db.get_by_session(forced_session):
                raise DispatchError(404, "unknown forced agent")
            target = SendTarget(
                session=forced_session,
                text=text,
                routed_by_name=True,
            )
        else:
            target = resolve_send_target(
                text=text,
                requested_session=requested_session,
                default_session=self.ctx.default_session,
                agents_path=self.ctx.agents_path,
                sticky_session=self._sticky_session(),
            )
        session = target.session
        text = target.text
        unheard_audio = session in set(unheard_audio_sessions)
        agent = agents_db.get_by_session(session)
        if not agent:
            raise DispatchError(404, "unknown agent")
        from . import task_goal_recovery, task_plans
        if origin == "heartbeat" and task_plans.recovery_owns_agent(agent["agent_id"]):
            raise DispatchError(409, "Durable goal owns continuation")
        _validate_goal_dispatch(agent, client_msg_id)
        agent_id = agent["agent_id"]
        if not skip_admission and origin in _RECOVERY_RESUMING_ORIGINS:
            self.clear_recovery_backoff(agent_id, reason=f"send:{origin}")
        request = admission_policy.LiveWork(
            client_msg_id=client_msg_id, durable_queue_id=durable_queue_id,
            janitor_run_id=janitor_run_id, sender_agent_id=sender_agent_id,
            queue_if_busy=queue_if_busy, skip_admission=skip_admission,
            allow_paused_queue=allow_paused_queue,
            janitor_demand_valid=janitor_demand_valid,
            janitor_run_active=_janitor_run_active(agent, janitor_run_id, trace_id))
        facts = _admission_facts(origin)
        decision = _admit(origin, agent, request, facts=facts, queue_paused=False)
        effective = decision.effective
        client_msg_id = effective.client_msg_id
        origin = effective.origin
        queue_if_busy = effective.queue_if_busy
        if effective.mute_audio:
            synthesize_audio = False
            unheard_audio = False
        if effective.notify_herald:
            self._notify_herald(session)
        # Sticky focus: addressing an agent by name makes them the new default,
        # so subsequent un-named messages keep going to them (hands-free, you
        # don't want to re-say the name every turn).
        if not janitor_run_id and (target.routed_by_name or routed_by_orchestrator):
            try:
                agents_db.set_focus(agent_id)
            except Exception as e:
                log_exception("stickyFocusSetFail", e, detail=session)
        backend = self.backends.normalize(agent.get("backend"))
        _refuse_oversized(self.backends, backend, text)
        # The Claude pwa-voice marker is (re)written per attempt in
        # _spawn_attempt — not here — so a retry or a redispatch always re-arms
        # a fresh marker. Writing it once was a single-use race: the first
        # UserPromptSubmit fire consumed it, leaving a later fire to tag the
        # real turn `local` and the Stop hook to skip TTS.
        backend_session_id = agents_db.live_backend_session(agent_id)
        cwd = _existing_cwd(agent.get("cwd"))
        # Ghost-session guard: a turn that crashed before its session was
        # created leaves the agent bound to a backend_session_id with no
        # transcript on disk. Resuming it exits instantly (rc=0, no output) and
        # wedges the agent on every turn forever. If the backend has no
        # resume target for the binding, drop it and start fresh. (A CLI that
        # resumes by session id always has one; only a transcript-file CLI
        # checks the disk.)
        # Only a binding nothing is using: a turn that is starting or running
        # writes its transcript only once its CLI has begun, so a disk miss
        # then is not a ghost (TP-05).
        if (backend_session_id and not self._turn_in_flight(agent_id)
                and backends.by_id(backend).resume_target(
                    backend_session_id, str(cwd), self.home) is None):
            log("ghostSessionReset",
                f"agent={agent_id} bsid={backend_session_id} — resume target "
                f"has no transcript on disk; starting a fresh session")
            eventlog.emit("server", "ghostSessionReset", trace_id=trace_id,
                          session=session, detail={"bsid": backend_session_id})
            agents_db.end_current_runtime(agent_id)
            backend_session_id = ""
        is_new_session = not backend_session_id

        admission = getattr(_ADMISSION, "stack", None)
        if admission is not None:
            admission.enter_context(_SLOTS.admitting_send(agent_id))
        if is_new_session:
            backend_session_id = self._bind_new_session(backend, agent_id, session)

        context = eventlog.EventContext(
            trace_id=trace_id,
            agent_id=agent_id,
            session=session,
            backend_session_id=backend_session_id or None,
        )
        model, effort = _resolve_llm(agent, backend)
        team_digest, team_inbox_ids = team_store.pending_digest(agent_id)
        team_protocol = team_store.team_protocol_instruction(
            agent_id,
            turn_origin=origin,
        )
        spec = _TurnSpec(
            backend=backend,
            text=text,
            cwd=cwd,
            backend_session_id=backend_session_id,
            is_new_session=is_new_session,
            session=session,
            agent_id=agent_id,
            trace_id=trace_id,
            context=context,
            synthesize_audio=synthesize_audio,
            model=model,
            effort=effort,
            client_msg_id=client_msg_id,
            team_digest=team_digest,
            team_inbox_ids=tuple(team_inbox_ids),
            team_protocol=team_protocol,
            origin=origin,
            sender_agent_id=sender_agent_id,
            prompt_admission_id=prompt_admission_id,
            queue_id=(durable_queue_id
                      or ((client_msg_id or trace_id) if queue_if_busy else "")),
            unheard_audio=unheard_audio,
            janitor_run_id=janitor_run_id,
        )
        # Queue receipts and ordinary user admissions share one SQLite write
        # transaction, preserving client_msg_id idempotency even if concurrent
        # retries switch between normal and queue delivery.
        if skip_admission:
            if spec.queue_id and not turn_queue.dispatchable(
                    spec.queue_id, agent_id=spec.agent_id, trace_id=spec.trace_id,
                    session=spec.session, client_msg_id=spec.client_msg_id,
                    origin=command.origin, sender_agent_id=spec.sender_agent_id,
                    prompt_admission_id=spec.prompt_admission_id):
                raise _QueuedReceiptUnavailable(409, "queued message is no longer dispatchable")
            admission = True
        else:
            # Lock order: _TURN_LOCK is taken before SQLite writes everywhere
            # else (Stop, guarded callbacks, retries). Inside the write
            # transaction below this thread must therefore never wait for
            # _TURN_LOCK, or a lock holder blocks on our SQLite lock for the
            # busy timeout and fails with "database is locked". Read the
            # in-memory slot up front and pass the snapshot in.
            slot = self._slot_snapshot(spec)
            held = self._held_in_memory(spec)
            deferred_events: list[dict] = []
            database = db.conn()
            database.execute("BEGIN IMMEDIATE")
            try:
                request_id = spec.client_msg_id or spec.trace_id
                if spec.origin == "heartbeat" and request_id.startswith("janitor-demand-"):
                    from .janitor_autonomy import validate_dispatch
                    if not validate_dispatch(spec.session, request_id):
                        raise DispatchError(409, "Heartbeat decision authority changed before admission")
                existing_queue_status = turn_queue.status(request_id)
                if existing_queue_status:
                    database.execute("COMMIT")
                    queue_state = turn_queue.state(spec.agent_id)
                    return DispatchResult(
                        session=session, backend=backend,
                        queued=existing_queue_status == "queued",
                        queue_depth=queue_state["count"],
                        queue_revision=queue_state["revision"])
                if prompt_admission is not None:
                    prompt_admission_id = prompt_admissions.record(
                        prompt_admission, agent_id=spec.agent_id,
                        session=spec.session,
                    )
                    spec = replace(
                        spec, prompt_admission_id=prompt_admission_id,
                    )
                if queue_if_busy:
                    if message_store.has_client_message(request_id):
                        if not self._admitted_but_never_launched(
                                spec, request_id, slot=slot, held=held):
                            database.execute("COMMIT")
                            return DispatchResult(session=session, backend=backend)
                        message_store.relink_client_message(
                            request_id, trace_id=spec.trace_id)
                    turn_queue.enqueue(
                        queue_id=spec.queue_id, agent_id=spec.agent_id,
                        session=spec.session, text=spec.text,
                        trace_id=spec.trace_id,
                        client_msg_id=spec.client_msg_id,
                        synthesize_audio=spec.synthesize_audio,
                        origin=spec.origin,
                        sender_agent_id=spec.sender_agent_id,
                        prompt_admission_id=spec.prompt_admission_id,
                    )
                    admission = True
                else:
                    admission = self._record_user_message(
                        spec, deferred=deferred_events)
                    superseded_trace = ""
                    if admission is False and self._admitted_but_never_launched(
                            spec, request_id, slot=slot, held=held):
                        # The client is retrying a message this Host admitted
                        # but never launched (2026-09-20: the launch died on
                        # "database is locked" after the durable row was
                        # written, and every retry was answered "already
                        # sent"). Own the row under this attempt and launch.
                        superseded_trace = message_store.client_message_trace(request_id) or ""
                        message_store.relink_client_message(
                            request_id, trace_id=spec.trace_id)
                        eventlog.emit("server", "sendRelaunched", context=spec.context)
                        log("sendRelaunched",
                            f"agent={spec.agent_id} client_msg_id={request_id} "
                            f"trace={spec.trace_id or '∅'}")
                        admission = True
                    if admission is True and launch_receipts.eligible(
                            origin=spec.origin, client_msg_id=spec.client_msg_id,
                            call_opening=_is_call_opening(spec)):
                        # Atomic with the row: until a backend is handed the
                        # prompt, this is the proof a same-id retry may launch.
                        launch_receipts.record_pending(
                            agent_id=spec.agent_id, trace_id=spec.trace_id,
                            client_msg_id=spec.client_msg_id, origin=spec.origin,
                            sender_agent_id=spec.sender_agent_id,
                            supersedes=superseded_trace,
                            user_stop_generation=turn_queue.user_stop_generation(spec.agent_id))
                database.execute("COMMIT")
            except BaseException:
                database.execute("ROLLBACK")
                raise
            # SSE fan-out takes the stream hub locks and writes sse_events;
            # do it only once the admission is durable and the lock released.
            # It is a projection of a row that is already durable: a failed
            # insert (2026-10-25, Lumi: "database is locked" here escaped
            # before the launch) must never stop the turn from starting.
            for event in deferred_events:
                try:
                    events.broadcast(self.ctx.stream, events.as_event(event))
                except Exception as e:  # noqa: BLE001
                    try:
                        log_exception("admissionBroadcastFail", e, detail=spec.session)
                    except Exception:  # noqa: BLE001 - not even logging may stop the launch
                        pass
        if admission is False:
            eventlog.emit("server", "sendDeduplicated", context=spec.context)
            log("sendDeduplicated",
                f"agent={spec.agent_id} "
                f"client_msg_id={spec.client_msg_id or spec.trace_id}")
            return DispatchResult(session=session, backend=backend)

        # The pause is read after the durable receipt, as before; the policy
        # is asked again with that one fact, every other input unchanged.
        decision = _admit(command.origin, agent, request, facts=facts,
                          queue_paused=turn_queue.is_paused(spec.agent_id))
        if decision.effective.paused_bypass:
            # Stop parks already-admitted follow-ups, not the conversation.
            # Fresh user intent can arrive through chat OR an Oracle handoff.
            # Admit this request only: lifting the global pause would also
            # drain old parked work. Recovery/retries must not grant fresh
            # intent to a durable item that Stop already fenced.
            log("pausedQueueBypassedByFreshSend",
                f"agent={spec.agent_id} origin={spec.origin} trace={spec.trace_id or '∅'}")
        elif isinstance(decision, admission_policy.Queue):
            self._broadcast_queue_state(spec, started=False)
            queue_state = turn_queue.state(spec.agent_id)
            return DispatchResult(
                session=session, backend=backend, queued=True,
                queue_depth=queue_state["count"],
                queue_revision=queue_state["revision"])
        spec = replace(spec, bypass_pause=_pause_exempt_on_arrival(
            command, decision.effective))

        # A live Codex turn accepts follow-ups through the official turn/steer
        # protocol. Other backends retain their existing dispatch behavior.
        # Behind the release drain's fence a follow-up waits instead: steering
        # would stretch the very turn the drain is waiting for.
        wait_for_start = False
        if decision.effective.steer_allowed and not _SLOTS.fenced and self._start_pending(spec):
            # The agent's own turn is still starting (its turn/start not yet
            # confirmed): the follow-up waits for it, durably (a park row
            # written before it is acknowledged), rather than preempting the
            # start or holding this request for it (TP-N3).
            wait_for_start = True
        elif (decision.effective.steer_allowed and not _SLOTS.fenced
                and self._steer_if_supported(spec)):
            return DispatchResult(session=session, backend=backend)
        if self._enqueue_if_busy(spec, queue_if_busy=queue_if_busy, wait_for_start=wait_for_start):
            if queue_if_busy:
                self._broadcast_queue_state(spec, started=False)
            queue_state = turn_queue.state(spec.agent_id)
            return DispatchResult(
                session=session, backend=backend, queued=True,
                queue_depth=queue_state["count"],
                queue_revision=queue_state["revision"])
        if not self._call_opening_claimed(spec):
            # A listen-first initiative that a greeting withdrew: never runs.
            self._finish_turn(spec)
            return DispatchResult(session=session, backend=backend)
        if queue_if_busy:
            # Idle queue request: it starts immediately, so admit its visible
            # user row now. Recovery may find the row already present.
            try:
                self._record_user_message(spec)
            except DispatchError:
                # The durable queue receipt remains authoritative. Release the
                # claimed slot and retry recovery instead of leaving a phantom
                # in-flight owner or requiring a client restart.
                _SLOTS.release(spec.agent_id, spec.trace_id)
                self.retry_scheduler(1.0, self.recover_queued)
                queue_state = turn_queue.state(spec.agent_id)
                return DispatchResult(
                    session=session, backend=backend, queued=True,
                    queue_depth=queue_state["count"],
                    queue_revision=queue_state["revision"])
        turn_id = 0
        launch_entered = False
        try:
            turn_id = turn_lifecycle.open_turn(
                agent_id=agent_id, source="pwa", trace_id=trace_id,
                synthesize_audio=synthesize_audio)
            # This turn owns the agent's live trace. A call opening first records its admission
            # mark (s_open); ordinary turns keep None and read no generation.
            if _is_call_opening(spec):
                spec = replace(spec, s_open=turn_lifecycle.state_generation(agent_id))
            agents_db.set_trace_for_session(session, trace_id)
            try:
                launch_entered = True
                spawned = self._spawn_attempt(spec, attempt=1)
                if spawned is _ADMISSION_REFUSED:
                    # The final call-open check refused it and settled the turn: never spawned,
                    # and not superseded either (its receipt stands).
                    return DispatchResult(session=session, backend=backend)
                if not spawned:
                    unsent = DispatchError(409, "turn superseded before spawn")
                    _mark_preinvoke_failure(unsent, agent_id=spec.agent_id, trace_id=spec.trace_id)
                    raise unsent
            except JanitorDispatchError:
                self._discard_fenced_janitor(spec)
                raise
            except DispatchError as exc:
                if _is_call_opening(spec) and _backend_invoked(exc):
                    # The opening's one launch reached the backend and then failed: the same
                    # fenced end as any post-launch failure, never an "unlaunched" turn.
                    self._end_launched_opening(spec, error_classify.RUNNER_EXIT, str(exc), 1)
                    try:
                        turn_lifecycle.close_turns_for_trace(spec.agent_id, spec.trace_id)
                    except Exception as close_error:  # noqa: BLE001
                        log_exception("callOpenTurnCloseFail", close_error, detail=spec.session)
                    raise
                self._release_unsent_opening(spec, exc)
                if not self._has_live_turn(spec):
                    self._abandon_unlaunched(spec, turn_id, exc)
                if isinstance(exc, GoalWakeDispatchError) and exc.permanent:
                    self._retire_goal_queue({
                        "queue_id": spec.queue_id, "agent_id": spec.agent_id,
                        "session": spec.session, "client_msg_id": spec.client_msg_id}, exc)
                # Spawn never started: release the in-flight slot (and drain any
                # message that queued behind it) so the agent isn't wedged.
                if turn_queue.contains(spec.queue_id):
                    _SLOTS.release(spec.agent_id, spec.trace_id)
                    self.retry_scheduler(1.0, self.recover_queued)
                else:
                    self._finish_turn(spec)
                raise
        except (DispatchError, JanitorDispatchError):
            raise
        except BaseException as e:
            if not launch_entered:
                _mark_preinvoke_failure(e, agent_id=spec.agent_id, trace_id=spec.trace_id)
            # Anything else (2026-09-20: sqlite "database is locked" under a
            # long transcript import) used to leave a phantom in-flight slot
            # with an open turn row and no process. Codex is steerable, so
            # every later send for the agent was "steered" into that phantom
            # and vanished. Give the slot and the turn back before failing.
            self._release_unsent_opening(spec, e)
            self._abandon_unlaunched(spec, turn_id, e)
            raise
        # Once the backend has accepted the turn, a bookkeeping failure must
        # never free its ownership or permit a duplicate launch.
        self._mark_spawned(spec)
        return DispatchResult(session=session, backend=backend)

    def _end_launched_opening(self, spec: _TurnSpec, category: str, message: str | None,
                              attempt: int) -> None:
        """F1: a listen-first initiative is launched at most once. Its turn failed after the
        backend had it, or in a state that cannot prove otherwise: no retry, redelivery or
        resume. Its opening becomes ``unknown`` and the turn ends visibly interrupted."""
        try:
            from . import agent_rings
            agent_rings.resolve_launch_failure(spec.queue_id or spec.client_msg_id, spec.trace_id)
        except Exception as exc:  # noqa: BLE001 - the turn still ends visibly
            log_exception("callOpenLaunchFailureResolveFail", exc, detail=spec.session)
        self._mark_interrupted(spec, category, message, attempts=attempt, call_opening=True)

    def _release_unsent_opening(self, spec: _TurnSpec, error: BaseException | None) -> None:
        """A listen-first initiative whose launch failed before the backend
        was invoked goes back to its normal first claim (agent_rings D2); a
        failure after that point stays claimed and becomes unknown."""
        if not spec.queue_id or _backend_invoked(error) or not _is_call_opening(spec):
            return
        try:
            from . import agent_rings
            agent_rings.release_unsent(spec.queue_id, spec.trace_id)
        except Exception as exc:  # noqa: BLE001 - an unreleased claim resolves to unknown
            log_exception("callOpenReleaseFail", exc, detail=spec.session)

    def _abandon_unlaunched(self, spec: _TurnSpec, turn_id: int, error: BaseException) -> None:
        """Release an abandoned slot; negative proof requires a known phase."""
        _SLOTS.release(spec.agent_id, spec.trace_id)
        log_exception("spawnAbandonedUnlaunched", error, detail=spec.session)
        eventlog.emit("server", "spawnAbandonedUnlaunched", context=spec.context,
                      detail={"error": str(error)[:200]})
        proof = _preinvoke_failure(error)
        if proof is None or (proof.agent_id, proof.trace_id) != (spec.agent_id, spec.trace_id):
            log("unlaunchedMarkerDeferred", f"agent={spec.agent_id} trace={spec.trace_id} reason=phase_unknown")
            return
        known = turn_id if type(turn_id) is int and turn_id > 0 else None
        _mark_preinvoke_failure(error, agent_id=spec.agent_id, trace_id=spec.trace_id, turn_id=known)
        proof = _preinvoke_failure(error)
        marked = False
        try:
            turn_lifecycle.record_unlaunched(spec.agent_id, spec.trace_id, turn_id=known)
            marked = True
            if turn_id:
                turn_lifecycle.close_turn(turn_id)
        except Exception as close_error:  # noqa: BLE001
            log_exception("spawnAbandonedCloseFail", close_error, detail=spec.session)
            if not marked:
                # As before, a failed marker skips the close: no second write
                # waits before the 500. The marker is retried in the background.
                try:
                    self._retry_unlaunched_marker(
                        spec, len(_UNLAUNCHED_MARKER_RETRY_SEC), turn_id=known, proof=proof)
                except Exception as scheduling_error:
                    # Preserve the original launch failure even if proof capture fails.
                    BaseException.add_note(error, f"unlaunchedRetryCaptureFailed agent={spec.agent_id} trace={spec.trace_id} type={type(scheduling_error).__name__}")

    def _retry_unlaunched_marker(self, spec: _TurnSpec, attempts_left: int, *,
                                 turn_id: int | None = None, frontier: int | None = None,
                                 proof: _PreInvokeFailure | None = None) -> None:
        """The 'never started' marker is what lets the client's same-id resend
        launch this message once; without it the resend is refused for ever
        (CLIENT-SEND-500). Write it again in the background, never delaying
        the 500, and only if it is not already there: its INSERT may have
        committed before the error (CLIENT-SEND-500, reviewer B)."""
        if attempts_left <= 0:
            return
        if (not isinstance(proof, _PreInvokeFailure)
                or (proof.agent_id, proof.trace_id) != (spec.agent_id, spec.trace_id)):
            log("unlaunchedMarkerDeferred", f"agent={spec.agent_id} trace={spec.trace_id} reason=phase_unknown")
            return
        if turn_id != proof.turn_id:
            log("unlaunchedMarkerDeferred", f"agent={spec.agent_id} trace={spec.trace_id} reason=attempt_mismatch")
            return
        if frontier is None:
            # A read fault is not an empty frontier. Let the owner preserve its primary error.
            frontier = int(db.conn().execute(
                "SELECT COALESCE(MAX(turn_id),0) FROM turns WHERE agent_id=? AND trace_id=?",
                (spec.agent_id, spec.trace_id)).fetchone()[0])
        original_turn = turn_id if type(turn_id) is int and turn_id > 0 else None
        captured_frontier = frontier
        delay = _UNLAUNCHED_MARKER_RETRY_SEC[-attempts_left]

        def retry() -> None:
            try:
                if original_turn is not None:
                    marked = db.conn().execute(
                        """SELECT 1 FROM state_log WHERE agent_id=?
                             AND json_extract(detail,'$.trace_id')=?
                             AND json_extract(detail,'$.dispatch_not_started')
                             AND json_type(detail,'$.dispatch_not_started_turn_id')='integer'
                             AND json_extract(detail,'$.dispatch_not_started_turn_id')=?
                             AND COALESCE(json_extract(detail,'$.reason'),'')<>'update_handoff' LIMIT 1""",
                        (spec.agent_id, spec.trace_id, original_turn)).fetchone() is not None
                else:
                    marked = _unlaunched_marked(spec.agent_id, spec.trace_id)
                if not marked:
                    eligible = original_turn if original_turn is not None and original_turn <= captured_frontier else None
                    turn_lifecycle.record_unlaunched(spec.agent_id, spec.trace_id, turn_id=eligible)
            except Exception as error:  # noqa: BLE001 - logged; one more try if left
                log_exception("spawnAbandonedMarkerFail", error, detail=spec.session)
                self._retry_unlaunched_marker(
                    spec, attempts_left - 1, turn_id=original_turn,
                    frontier=captured_frontier, proof=proof)
        try:
            self.retry_scheduler(delay, retry)
        except Exception as error:  # noqa: BLE001 - never a second abandonment
            # No timer thread (the same exhaustion that failed the write):
            # the dispatch failure stands, the marker stays as it is.
            log_exception("spawnAbandonedMarkerRetryFail", error, detail=spec.session)

    def _held_in_memory(self, spec: _TurnSpec) -> bool:
        """Whether this process still holds the same client message behind a
        busy turn (read before a write transaction, like the slot)."""
        request_id = spec.client_msg_id or spec.trace_id
        if not request_id:
            return False
        return _SLOTS.has_queued(spec.agent_id, lambda item: (
            (getattr(item, "client_msg_id", "") or getattr(item, "trace_id", "")) == request_id))

    def _admitted_but_never_launched(self, spec: _TurnSpec, client_msg_id: str,
                                     *, slot: tuple[str, bool] | None = None,
                                     held: bool | None = None) -> bool:
        """Retry only known failed launches; never infer safety from missing logs.

        `slot` and `held` are read before the caller's write transaction;
        without them they are read here (no transaction open)."""
        trace = message_store.client_message_trace(client_msg_id)
        if not trace:
            return False
        if turn_queue.holds_client_message(client_msg_id):
            return False  # it is waiting (queued, or held by Stop or a drain)
        if held is None:
            held = self._held_in_memory(spec)
        if held:
            return False  # waiting behind a busy turn in this process
        status = agents_db.trace_launch_status(
            spec.agent_id, trace, client_msg_id=client_msg_id,
            origin=spec.origin, sender_agent_id=spec.sender_agent_id)
        if status == "fenced":
            # Stop withdrew it before launch: a resend is not new intent, and
            # it is not "sent" either. Same answer an older client already handles.
            log("sendFencedByStop", f"agent={spec.agent_id} client_msg_id={client_msg_id}")
            raise DispatchError(503, UNCONFIRMED_DELIVERY)
        if slot is None:
            slot = self._slot_snapshot(spec)
        in_flight_trace, live = slot
        if in_flight_trace == trace and live:
            return False
        if status == "unknown":
            raise DispatchError(503, UNCONFIRMED_DELIVERY)
        return status == "retryable"

    def _record_user_message(self, spec: _TurnSpec, *,
                             deferred: list[dict] | None = None) -> bool | None:
        """Durably admit the user row. With `deferred`, the transcript
        wake-up is appended there for the caller to broadcast after COMMIT
        instead of fanning out while a write transaction is open."""
        if _is_call_opening(spec):
            # A listen-first initiative is the agent's own opening: no user row.
            return True
        try:
            appended = agents_db.record_user_message(
                agent_id=spec.agent_id,
                backend_session_id=spec.backend_session_id,
                text=spec.text,
                client_msg_id=spec.client_msg_id or spec.trace_id,
                origin=spec.origin,
                sender_agent_id=spec.sender_agent_id or None,
                prompt_admission_id=spec.prompt_admission_id,
                trace_id=spec.trace_id,
            )
            if appended and getattr(self.ctx, "stream", None) is not None:
                event = {
                    "type": SSEType.TRANSCRIPT_UPDATED,
                    "agent_id": spec.agent_id,
                    "session": spec.session,
                    "backend_session_id": spec.backend_session_id,
                }
                if deferred is not None:
                    deferred.append(event)
                else:
                    events.broadcast(self.ctx.stream, events.as_event(event))
            if appended is None:
                # A brand-new Codex session does not have its backend UUID yet;
                # on_init persists this row once that identity is available.
                return None
            return bool(appended.get("created", True))
        except Exception as e:
            log_exception("pendingUserMessageFail", e, detail=spec.session)
            # Never launch a turn whose causing message was not durably
            # admitted. The client's outbox will retry the same id safely.
            raise DispatchError(503, "could not durably queue message") from e

    def _notify_herald(self, session: str) -> None:
        herald = getattr(self.ctx, "herald", None)
        if herald is None:
            return
        try:
            herald.set_awaiting(session)
        except Exception as e:
            log_exception("heraldSetAwaitingFail", e, detail=session)
