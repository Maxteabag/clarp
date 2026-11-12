"""LauncherMixin: methods moved out of lib/turn_dispatch.py::TurnDispatchService by tools/refactor/s3_apply.py
(group "turn_launcher"). Generated: change the recipe, not this file."""

from __future__ import annotations

from dataclasses import replace
from . import agents as agents_db
from . import backends, error_classify, eventlog
from . import judgment_sites
from . import turn_lifecycle
from .turn_lifecycle import TurnEvent
from .turn_slots import defer_on
from .log import log, log_exception
from .turn_dispatch_state import _TURN_LOCK, _SLOTS, _INFLIGHT, _launching
from .turn_command import _TurnSpec, DispatchError, JanitorDispatchError, _janitor_spawn_lock, OPENING_WITHDRAWN, _ADMISSION_REFUSED, _is_call_opening, _backend_invoked
from .turn_stop import _result_error_text, _result_detail, _result_assistant_text, _record_dreaming_result

# What a turn is resumed with when it met the limit again soon after a recovery
# had resumed it. A resume needs a prompt; this is all of it. The recovery
# message and the request are in the conversation already and are not sent
# again (laptopstudio, 2026-11-11: one more copy of both in each round).
RELAPSE_RESUME_TEXT = (
    "The account ran out again and Clarp has resumed this turn once more. Carry on from "
    "where the conversation stands; check the results of interrupted operations before "
    "retrying them. Nothing new is asked.")


class LauncherMixin:
    @staticmethod
    def _turn_in_flight(agent_id: str) -> bool:
        """A turn of this agent holds its slot here (claimed, spawning or
        running), or its state says it is busy (a turn owned elsewhere)."""
        try:
            return bool(_SLOTS.get(agent_id)) or agents_db.is_busy(agent_id)
        except Exception as exc:  # noqa: BLE001 - unknown is not 'nothing uses it'
            log_exception("ghostGuardLiveCheckFail", exc, detail=agent_id)
            return True

    def _bind_new_session(self, backend: str, agent_id: str, session: str) -> str:
        """The backend session id pre-minted for a new session, or "" when
        the CLI binds the id it reports. A binding failure is a 500."""
        try:
            return backends.by_id(backend).bind_new_session(
                agent_id, session, uuid_factory=self.uuid_factory) or ""
        except Exception as e:
            log_exception("sendPreStampSessionFail", e, detail=session)
            raise DispatchError(500, "could not bind backend session") from e

    def _spawn_attempt(self, spec: _TurnSpec, *, attempt: int) -> "bool | _AdmissionRefused":
        """Reject a stale attempt; AGY rechecks around state/Popen itself. False: superseded
        before spawn. _ADMISSION_REFUSED: the final call-open check refused and settled it.
        True: spawned, parked by the account coordinator, or ended by a later attempt's own
        failure handling."""
        with _launching(spec.agent_id, spec.trace_id):
            return self._spawn_attempt_owned(spec, attempt=attempt)

    def _spawn_attempt_owned(self, spec: _TurnSpec, *, attempt: int) -> "bool | _AdmissionRefused":
        with _TURN_LOCK:
            if _INFLIGHT.get(spec.agent_id) != spec.trace_id:
                log("spawnAbandoned",
                    f"agent={spec.agent_id} trace={spec.trace_id or '∅'} — "
                    "ownership lost before spawn")
                return False
        if spec.janitor_run_id:
            with _janitor_spawn_lock(spec.agent_id):
                with _TURN_LOCK:
                    if _INFLIGHT.get(spec.agent_id) != spec.trace_id:
                        return False
                outcome = self._spawn_attempt_claimed(spec, attempt=attempt)
        else:
            outcome = self._spawn_attempt_claimed(spec, attempt=attempt)
        if outcome is False:
            return False
        return _ADMISSION_REFUSED if outcome is _ADMISSION_REFUSED else True

    @staticmethod
    def _still_owns(spec: _TurnSpec) -> bool:
        with _TURN_LOCK:
            return _INFLIGHT.get(spec.agent_id) == spec.trace_id

    @staticmethod
    def _interrupt_unowned(spec: _TurnSpec, handle) -> None:
        log("spawnUnowned",
            f"agent={spec.agent_id} trace={spec.trace_id or '∅'} — "
            "Stop took the slot during the handoff; interrupting the started turn")
        try:
            handle.terminate()
        except Exception as exc:  # noqa: BLE001 - the launch already happened
            log_exception("spawnUnownedInterruptFail", exc, detail=spec.agent_id)

    def _resume_after_account_switch(self, spec, attempt, state):
        if not self._holds_slot(spec):
            return
        # It is the agent's live turn again. A message queued behind it while
        # it waited may have moved the trace marker (a voice message does);
        # left there, this turn's own callbacks would read as a replaced
        # turn's. The queued message sets it again when its turn starts.
        agents_db.set_trace_for_session(spec.session, spec.trace_id)
        if _is_call_opening(spec) and (attempt != 1 or state.get("spawn_started") is not None):
            # Only an attempt-1 initiative parked before its spawn flag was ever set (the flag
            # is set immediately before the backend is invoked, in this same state) is proven
            # unsent and makes its one first launch below; anything else stays unknown.
            self._end_launched_opening(spec, error_classify.INTERRUPTED,
                                       "listen-first opening not resumed after an account switch",
                                       attempt)
            return
        if _is_call_opening(spec) and not self._admit_opening_launch(spec, attempt):
            return
        bsid = state.get("backend_session_id") or spec.backend_session_id
        transcript = backends.by_id(spec.backend).resume_target(
            bsid, str(spec.cwd), self.home)
        interrupted = (state.get("spawn_started") or attempt > 1
                       or bool(spec.recovery_text))
        # One recovery message for each run of relapses. A turn that met the
        # limit again soon after a recovery resumed it has the message in its
        # conversation: the process that carried it started its session, or
        # the turn was already past that and carrying the one line.
        told = bool(state.get("relapsed") and (
            spec.recovery_text == RELAPSE_RESUME_TEXT
            or (spec.recovery_text and state.get("saw_init"))))
        resume_spec = replace(
            spec, backend_session_id=bsid,
            is_new_session=spec.is_new_session and transcript is None,
            recovery_text=(RELAPSE_RESUME_TEXT if told else (
                f"Clarp recovered from a {spec.backend} account usage limit. Continue "
                "the unfinished request from the existing conversation. Check "
                "the results of interrupted operations before retrying them; "
                "do not repeat work or external actions already completed. "
                "If the request is already complete, report that and stop.\n\n"
                f"Original request:\n{spec.text}"
            )) if interrupted and transcript is not None else "",
        )
        try:
            spawned = self._spawn_attempt(resume_spec, attempt=attempt)
            if spawned is not _ADMISSION_REFUSED and spawned:
                self._mark_spawned(resume_spec)
        except JanitorDispatchError:
            self._discard_fenced_janitor(resume_spec)
        except DispatchError as exc:
            if _is_call_opening(resume_spec) and _backend_invoked(exc):
                self._end_launched_opening(resume_spec, error_classify.RUNNER_EXIT, str(exc),
                                           attempt)
            else:
                # Refused, or the backend failed to start: no process ran, so
                # this is not a runner that exited (it used to say so, and
                # read as a crash in every goal ledger that recorded it).
                self._mark_interrupted(resume_spec, error_classify.NOT_STARTED,
                                       str(exc), attempts=attempt)

    def _end_parked_for_its_plan(self, spec: _TurnSpec, attempt: int, state: dict) -> None:
        """The provider said the account does not offer this turn's model, and
        the selector has since answered PLAN_CHECKS times without it. The turn
        ends here as it would have at that failure: with the provider's own
        sentence. Resumed, it would fail again and start the next recovery
        (2026-11-11)."""
        if not self._holds_slot(spec):
            return
        agents_db.set_trace_for_session(spec.session, spec.trace_id)
        message = state.get("plan_failure")
        if _is_call_opening(spec):
            self._end_launched_opening(spec, error_classify.ACCOUNT_PLAN, message, attempt)
            return
        self._mark_interrupted(spec, error_classify.ACCOUNT_PLAN, message, attempts=attempt)

    def _admit_opening_launch(self, spec: _TurnSpec, attempt: int) -> bool:
        """L2: an initiative is admitted again before it runs (agent_rings.authorize_parked_launch):
        when a parked first launch resumes, and as the final check before the spawn flag on every
        launch path. True only for "launch". A breached fence cancels it unsent (no spawn, not a
        failure); an unprovable identity or state never launches and ends unknown, visibly
        interrupted."""
        try:
            from . import agent_rings
            verdict = agent_rings.authorize_parked_launch(spec.queue_id or spec.client_msg_id,
                                                          spec.trace_id)
        except Exception as exc:  # noqa: BLE001 - unproven: never a launch
            log_exception("callOpenParkedAdmitFail", exc, detail=spec.session)
            verdict = "unknown"
        if verdict == "launch":
            return True
        if verdict == "cancelled":
            try:
                # The UNLAUNCHED receipt in the opening's own words: there is no saved user
                # message and it must never be retried. Not back-dated: no user row anchors it,
                # and this slot still owns the agent, so it also replaces the park's wait state.
                turn_lifecycle.transition(
                    spec.agent_id, TurnEvent.UNLAUNCHED,
                    {"trace_id": spec.trace_id, "dispatch_not_started": True,
                     "message": OPENING_WITHDRAWN}, force=True)
                turn_lifecycle.close_turns_for_trace(spec.agent_id, spec.trace_id)
            except Exception as exc:  # noqa: BLE001
                log_exception("callOpenParkedCancelCloseFail", exc, detail=spec.session)
            self._finish_turn(spec)
            return False
        self._end_launched_opening(spec, error_classify.INTERRUPTED,
                                   "listen-first opening admission could not be proven after an "
                                   "account switch", attempt)
        return False

    def _pause_for_account(self, spec: _TurnSpec, *, unserved: bool = False) -> None:
        """Show that the turn waits for an account. ``unserved``: the rest of
        the pool works on; the active account does not serve this turn's
        model now (its quota for that model is spent, or its probe failed)."""
        message = (error_classify.account_wait_message(spec.backend, spec.model or None)
                   if unserved else error_classify.account_wait_message(spec.backend))
        with _TURN_LOCK:
            if _SLOTS.owns(spec.agent_id, spec.trace_id):
                _SLOTS.touch_claim(spec.agent_id)
            defer_on(_TURN_LOCK, lambda: turn_lifecycle.try_transition(
                spec.agent_id, TurnEvent.ACCOUNT_RECOVERY_WAIT,
                {"dispatch": spec.backend, "trace_id": spec.trace_id,
                 "account_recovery": "waiting", "message": message}))

    def _sticky_session(self) -> str:
        """Session of the currently-focused agent — the last one addressed by
        name (or selected in the UI). Un-named messages stick to it. Empty if
        there's no live focus, so routing falls back to the client's request."""
        try:
            agent_id = agents_db.get_focus()
            if not agent_id:
                return ""
            agent = agents_db.get_by_agent_id(agent_id)
            return (agent["session"] if agent and
                    agents_db.interaction_capabilities(agent)["can_chat"] else "")
        except Exception:
            return ""

    def _attempt_callbacks(self, spec: _TurnSpec, attempt: int, state: dict):
        agent_id = spec.agent_id
        trace_id = spec.trace_id
        context = spec.context

        def on_init(backend_session_id: str) -> bool:
            if state.get("account_recovery") or self._superseded(spec):
                return False
            bound = False
            try:
                agents_db.bind_backend_session(agent_id, backend_session_id)
                bound = agents_db.live_backend_session(agent_id) == backend_session_id
            except agents_db.SessionAlreadyBound as e:
                state["bind_error"] = (
                    f"backend session already bound to {e.owner_agent_id}")
                log("clarpInitSessionConflict",
                    f"{backend_session_id} already owned by {e.owner_agent_id}; "
                    f"refused to rebind onto {agent_id}")
            except Exception as e:
                state["bind_error"] = str(e)
                log_exception("clarpInitRecordFail", e, detail=backend_session_id)
            if not bound:
                state["saw_init"] = False
                state["backend_session_id"] = spec.backend_session_id
                self.backends.interrupt(spec.backend, agent_id)
                return False
            state["saw_init"] = True
            state["backend_session_id"] = backend_session_id
            # New Codex sessions do not know their backend UUID when /send is
            # accepted, so the first attempt to persist the client-authored
            # message id is necessarily a no-op. Persist it as soon as init
            # binds the UUID; this is idempotent for backends that pre-bind.
            if agents_db.live_backend_session(agent_id) == backend_session_id:
                self._record_user_message(
                    replace(spec, backend_session_id=backend_session_id)
                )
            return True

        def on_result(event: dict) -> None:
            if state.get("account_recovery") or state.get("outcome_seen"):
                return
            state["outcome_seen"] = True
            if self._superseded(spec):
                self._release_superseded(spec)
                return
            try:
                if state.get("bind_error"):
                    self._handle_failure(
                        spec, attempt, state, error_classify.RUNNER_EXIT,
                        f"backend session bind failed: {state['bind_error']}")
                    return
                category = judgment_sites.classify_error_or_unknown(
                    _result_error_text(event), error_classify.classify_result(event))
                if category != error_classify.CLEAN:
                    self._handle_failure(spec, attempt, state, category,
                                         _result_error_text(event))
                    return
                detail = _result_detail(event, trace_id=trace_id)
                # DONE is the durable completion signal consumed by the
                # user-facing push and unread/badge policy. It is non-busy,
                # just like IDLE, but preserves the completion edge.
                turn_lifecycle.try_transition(
                    agent_id, TurnEvent.PROCESS_EXITED_OK, detail)
                # Local usage accounting. The CLI's own numbers, for every
                # dispatch mode — this is what feeds /backend-usage now that
                # the statusline source is gone (it never ran under `-p`).
                try:
                    from . import turn_usage
                    turn_usage.record(
                        backend=spec.backend or backends.CLAUDE,
                        agent_id=agent_id, detail=detail, trace_id=trace_id)
                except Exception as e:  # noqa: BLE001
                    log_exception("turnUsageRecordFail", e, detail=trace_id)
                if spec.origin == "dreaming":
                    _record_dreaming_result(agent_id, event)
                if spec.origin == "oracle":
                    # Finalize the canonical terminal payload before applying
                    # the empty/tool-only fallback. Streaming runners persist
                    # provisional live text but do not all finalize it.
                    from . import message_store, oracle_delegations
                    final_text = _result_assistant_text(event)
                    if final_text:
                        message_store.finalize_live_assistant_message(
                            agent_id=spec.agent_id,
                            backend_session_id=str(
                                state.get("backend_session_id")
                                or spec.backend_session_id),
                            trace_id=spec.trace_id,
                            text=final_text,
                        )
                    oracle_delegations.fail_for_trace(
                        spec.trace_id, "Agent completed without a text result")
                eventlog.emit("server", "clarpTurnDone", context=context,
                              detail={
                                  "tokens_in": detail.get("tokens_in"),
                                  "tokens_out": detail.get("tokens_out"),
                                  "cost_usd": detail.get("cost_usd"),
                                  "duration_ms": detail.get("duration_ms"),
                              })
                # Terminal: clean completion — run whatever queued behind it.
                self._finish_turn(spec)
            except Exception as e:
                log_exception("clarpOnResultFail", e, detail=trace_id)
                self._finish_turn(spec)

        def on_error(message: str) -> None:
            if state.get("account_recovery") or state.get("outcome_seen"):
                return
            state["outcome_seen"] = True
            if self._superseded(spec):
                self._release_superseded(spec)
                return
            try:
                category = (error_classify.RUNNER_EXIT if state.get("bind_error")
                            else judgment_sites.classify_error_or_unknown(
                                message, error_classify.classify_error(message)))
                if state.get("bind_error"):
                    message = f"backend session bind failed: {state['bind_error']}"
                self._handle_failure(spec, attempt, state, category, message)
            except Exception as e:
                log_exception("clarpOnErrorFail", e, detail=trace_id)

        if spec.janitor_run_id:
            def guarded(callback):
                def invoke(*args):
                    with _TURN_LOCK:
                        return callback(*args)
                return invoke
            return guarded(on_init), guarded(on_result), guarded(on_error)
        wrap = backends.by_id(spec.backend).wrap_turn_callback
        return (wrap(on_init, _TURN_LOCK), wrap(on_result, _TURN_LOCK),
                wrap(on_error, _TURN_LOCK))
