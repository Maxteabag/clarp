"""RetryMixin: methods moved out of lib/turn_dispatch.py::TurnDispatchService by tools/refactor/s3_apply.py
(group "turn_retry"). Generated: change the recipe, not this file."""

from __future__ import annotations

from dataclasses import replace
from . import agents as agents_db
from . import backends, error_classify, eventlog
from . import turn_lifecycle, turn_queue
from . import events
from .turn_lifecycle import TurnEvent
from .log import log, log_exception
from .protocol import AgentState
from .turn_dispatch_state import MAX_ATTEMPTS, BACKOFF_BASE_SEC, _TURN_LOCK, _SLOTS, account_failover, account_selector, _INFLIGHT
from .turn_command import _TurnSpec, JanitorDispatchError, OPENING_NOT_CONFIRMED, _is_call_opening


class RetryMixin:
    def _handle_failure(self, spec: _TurnSpec, attempt: int, state: dict,
                        category: str, message: str | None) -> None:
        """Decide what a non-clean turn outcome means: silently retry a
        connection drop, notify on an unrecoverable failure, or fall back to
        the legacy idle-flip for an unrecognised error."""
        # Preempted turns die by design when a newer send takes over the slot.
        # Their death must NOT retry, notify, or flip state — that would fight
        # the turn that superseded them. Trace no longer in-flight → ignore.
        with _TURN_LOCK:
            superseded = _INFLIGHT.get(spec.agent_id) != spec.trace_id
        if superseded:
            log("preemptedTurnIgnored",
                f"agent={spec.agent_id} trace={spec.trace_id or '∅'} — "
                f"superseded by a newer turn; ignoring its outcome")
            return
        msg = (message or "")[:300]
        if _is_call_opening(spec):
            # Every failure here came back through this attempt's on_result/on_error, which
            # only spawn_turn receives (the only callers): after spawn_started. A listen-first
            # initiative is launched at most once, so it is never retried, redelivered,
            # recovered or resumed on another account (no second automatic opening).
            self._end_launched_opening(spec, category, message, attempt)
            return
        backend = backends.by_id(spec.backend)
        if category == error_classify.USAGE_LIMIT and not spec.recovery_attempted:
            try:
                recovered = backend.recover_usage_limit(message)
            except Exception as exc:
                log_exception("usageLimitRecoveryFail", exc, detail=spec.trace_id)
                recovered = False
            with _TURN_LOCK:
                if _INFLIGHT.get(spec.agent_id) != spec.trace_id:
                    return  # stop/new message during the quota request
                if recovered:
                    self._schedule_retry(
                        replace(spec, recovery_attempted=True), attempt, state, msg)
                    return
        switch_pending = False
        if category == error_classify.USAGE_LIMIT:
            try:
                switch_pending = bool(backend.credential_switch_pending(message))
            except Exception as exc:  # noqa: BLE001 - unknown: failover decides as before
                log_exception("credentialSwitchCheckFail", exc, detail=spec.trace_id)
        if (category in (error_classify.USAGE_LIMIT, error_classify.AUTH,
                         error_classify.ACCOUNT_PLAN)
                and backend.account_pool()
                and not switch_pending   # switched already; no loop (BACKENDS-06 (c))
                and account_failover(spec.backend).request(
                    spec.agent_id, spec.trace_id, account_selector(spec.backend))):
            eventlog.emit("server", "claudeAccountRecovery", context=spec.context,
                          detail={"reason": category})
            return
        if category == error_classify.CONNECTION and attempt < MAX_ATTEMPTS:
            self._schedule_retry(spec, attempt, state, msg)
            return
        # At-least-once delivery: a timeout means the watchdog killed a turn
        # that never produced a result — either wedged on spawn (no init) or
        # stalled after init before emitting any reply output (a hung
        # model/context call). Either way the message was never answered and is
        # durably recorded, so re-dispatch it a bounded number of times. A
        # transient stall recovers; a persistent one falls through to NOTIFY
        # after MAX_ATTEMPTS. (A turn that was actively streaming resets the
        # watchdog, so it won't time out mid-work.)
        if category == error_classify.TIMEOUT and attempt < MAX_ATTEMPTS:
            phase = "before init" if not state.get("saw_init") else "after init"
            log("turnRedeliver",
                f"agent={spec.agent_id} attempt={attempt + 1}/{MAX_ATTEMPTS} "
                f"trace={spec.trace_id or '∅'} — backend stalled {phase}, "
                f"re-delivering")
            self._schedule_retry(spec, attempt, state, msg)
            return
        # Every failure that is not retried ends the turn visibly, an
        # unrecognised one included. It used to flip to IDLE, which every
        # client renders as a turn that answered: on 2026-10-07 a Codex login
        # that had lost its plan failed each turn at compaction, and the chat
        # showed "Worked for 1s" with the provider's refusal nowhere in sight.
        self._mark_interrupted(spec, category, message, attempts=attempt,
                               quota_confirmed=getattr(message, "quota_confirmed", None),
                               switch_pending=switch_pending)
        if spec.origin == "heartbeat":
            try:
                from . import heartbeat
                heartbeat.record_heartbeat_noop(spec.agent_id, is_interrupted=True)
            except Exception as exc:  # noqa: BLE001
                log_exception("heartbeatFailureNoopFail", exc, detail=spec.agent_id)

    def _schedule_retry(self, spec: _TurnSpec, attempt: int, state: dict,
                        message: str) -> None:
        next_attempt = attempt + 1
        delay = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
        # Keep the agent visibly busy across the gap rather than flicking to
        # idle and back; detail marks it as a reconnect, not fresh work.
        turn_lifecycle.try_transition(
            spec.agent_id, TurnEvent.RETRY_SCHEDULED,
            {"dispatch": spec.backend, "reconnect": next_attempt,
             "of": MAX_ATTEMPTS, "after_ms": int(delay * 1000),
             "summary": "Reconnecting… Your message is saved.",
             "action": "reconnecting"},
        )
        eventlog.emit("server", "turnReconnect", context=spec.context,
                      detail={"attempt": next_attempt, "of": MAX_ATTEMPTS,
                              "delay_ms": int(delay * 1000),
                              "err": message[:200]})
        log("turnReconnect",
            f"agent={spec.agent_id} attempt={next_attempt}/{MAX_ATTEMPTS} "
            f"delay={delay:.1f}s trace={spec.trace_id or '∅'}")
        # On retry of a brand-new session that never initialised, keep
        # --session-id so the backend re-creates it; once init landed, resume.
        next_new = spec.is_new_session and not state["saw_init"]
        next_spec = replace(
            spec, is_new_session=next_new,
            backend_session_id=(state.get("backend_session_id")
                                or spec.backend_session_id),
        )

        def _retry_spawn() -> None:
            # If a newer send preempted this turn during the backoff, abandon the
            # retry — the new turn owns the slot now.
            # The ownership check is atomic with account recovery (which
            # marks state under the coordinator lock before pausing); the
            # spawn itself runs outside _TURN_LOCK. A recovery that starts in
            # between finds the new attempt at register() and parks it.
            with _TURN_LOCK:
                if state.get("account_recovery"):
                    return  # account recovery already owns this continuation
                if not _SLOTS.owns(spec.agent_id, spec.trace_id):
                    log("retryAbandoned",
                        f"agent={spec.agent_id} trace={spec.trace_id or '∅'} — "
                        f"preempted during backoff")
                    return
            try:
                self._spawn_attempt(next_spec, attempt=next_attempt)
            except JanitorDispatchError:
                self._discard_fenced_janitor(next_spec)

        self.retry_scheduler(delay, _retry_spawn)

    def _mark_interrupted(self, spec: _TurnSpec, category: str,
                          message: str | None, *, attempts: int,
                          quota_confirmed: bool | None = None,
                          call_opening: bool = False,
                          switch_pending: bool = False) -> None:
        human = {
            error_classify.CONNECTION: "Connection lost — retries exhausted",
            error_classify.TRANSIENT: "API unavailable (overloaded / rate limited)",
            error_classify.INTERRUPTED: "Turn interrupted",
            error_classify.USAGE_LIMIT: "Usage limit reached",
            error_classify.AUTH: "Sign-in expired — could not refresh the account",
            error_classify.RUNNER_EXIT: "Agent process exited unexpectedly",
            error_classify.TIMEOUT: "Turn timed out — backend stopped responding",
            error_classify.ACCOUNT_PLAN: (
                f"This account's plan does not include {spec.model or 'this model'}. "
                "Switch to an account that has it or choose another model"),
        }.get(category, "Turn failed")
        human = error_classify.explain(message) or human
        limit_event = None
        if category == error_classify.USAGE_LIMIT and quota_confirmed is False:
            human = "Codex could not complete this request. Try again"
        if switch_pending:
            human = (f"{human} — the account switch waits until no Codex agent is "
                     "busy; send again once they finish")
        if category == error_classify.USAGE_LIMIT:
            try:
                limit_event = backends.by_id(spec.backend).classify_usage_limit(
                    message or "", quota_confirmed=quota_confirmed)
                if limit_event:
                    for related in limit_event.get("_additional_events") or []:
                        events.broadcast(self.ctx.stream, events.as_event(related))
                    if limit_event.get("_new"):
                        events.broadcast(self.ctx.stream, events.as_event({
                            key: value for key, value in limit_event.items()
                            if not key.startswith("_")
                        }))
            except Exception as exc:  # noqa: BLE001
                log_exception("providerLimitRecordFail", exc, detail=spec.trace_id)
        provider = error_classify.provider_message(message)
        state_detail = {
            "dispatch": spec.backend, "reason": category, "message": human,
            "error": (message or "")[:200], "provider_message": provider[:500],
            "attempts": attempts, "summary": human + ". Your message is saved.",
        }
        if limit_event:
            state_detail["provider_limit_event_id"] = limit_event[
                "provider_limit_event_id"]
        if call_opening:
            # The Host's own initiative, not a user's message: nothing was saved to resend.
            state_detail["summary"] = OPENING_NOT_CONFIRMED
            state_detail["trace_id"] = spec.trace_id
            written = self._end_opening_state(spec, state_detail)
        else:
            turn_lifecycle.try_transition(
                spec.agent_id, TurnEvent.PROCESS_EXITED_FAILED, state_detail)
            written = True
        if written:
            # Interruptions surface as agent state and the turnInterrupted event;
            # they are not spoken aloud (hearing raw failure text was jarring).
            # A call opening's refused end shows neither.
            eventlog.emit("server", "turnInterrupted", context=spec.context,
                          detail={"reason": category, "attempts": attempts,
                                  "err": (message or "")[:300]})
            log("turnInterrupted",
                f"agent={spec.agent_id} reason={category} attempts={attempts} "
                f"trace={spec.trace_id or '∅'} err={provider[:300]!r}")
        if category != error_classify.INTERRUPTED and not call_opening:
            # A call opening has no user message to put the failure under.
            _TURN_LOCK.defer(lambda: self._record_failure_row(spec, human, provider))
        from . import oracle_delegations
        oracle_delegations.fail_for_trace(
            spec.trace_id, (message or human)[:500])
        self._retire_unstarted_goal_wake(spec)
        # Terminal: a killed/interrupted turn still drains anything queued
        # behind it (e.g. an explicit stop, then your next message runs).
        self._finish_turn(spec)

    def _retire_unstarted_goal_wake(self, spec: _TurnSpec) -> None:
        """A goal wake whose turn failed before its queue receipt started.

        A turn parked for an account before its spawn leaves the receipt
        ``queued``. settle_receipt ends only a started one, and recovery never
        relaunches a row whose launch is unproven, so the row stayed for good
        and every autonomous wake read it as the owner's queued work
        (2026-11-10: goals silent for up to 32 hours). The goal scheduler
        regenerates its wake, so the row is retired like any wake that may no
        longer run; a message's row is not touched here."""
        from . import task_goal_recovery
        try:
            if (spec.queue_id
                    and str(spec.client_msg_id or "").startswith(task_goal_recovery.PREFIX)
                    and turn_queue.status(spec.queue_id) == "queued"):
                self._retire_goal_queue(
                    {"queue_id": spec.queue_id, "agent_id": spec.agent_id,
                     "session": spec.session, "client_msg_id": spec.client_msg_id},
                    task_goal_recovery.WakeRejected(
                        "turn_failed", "goal wake turn failed before it started"))
        except Exception as exc:  # noqa: BLE001 - the goal pass retires it later
            log_exception("goalQueueRetireFail", exc, detail=spec.session)

    def _end_opening_state(self, spec: _TurnSpec, state_detail: dict) -> bool:
        """The visible end of a call opening, written only while nothing newer touched the
        agent. A0 (its newest state row) is read FIRST, outside any transaction, then the slot
        ownership; the write itself re-checks, in one transaction, that A0 is still the newest
        row, the live trace is the opening's, its turn is unsettled and no hook prompt
        admission landed after ``spec.s_open`` (turn_lifecycle's conditional transition). A
        first dispatch that never ran leaves the agent SPAWNED, where only
        CALL_OPENING_UNCONFIRMED is legal. Returns whether the end was written.

        Accepted limits: a Claude opening that reached its own hook prompt ends refused
        (no OPENING_NOT_CONFIRMED and no turnInterrupted); the opening stays durably unknown
        with no retry, and a later IDLE from the reconciler is not that receipt. Interactive
        sessions admitted before s_open are unsupported; after a dispatcher restart no end is
        written here (no spec survives); the protection is one-sided (a later ordinary writer
        may still replace it); the turn's settlement is not atomic with the state row."""
        anchor = turn_lifecycle.state_generation(spec.agent_id)
        with _TURN_LOCK:
            owned = _SLOTS.owns(spec.agent_id, spec.trace_id)
        if not owned:
            log("callOpenEndSuperseded",
                f"agent={spec.agent_id} trace={spec.trace_id or '∅'} — "
                "ownership lost; its interrupted state is not written")
            return False
        event = (TurnEvent.CALL_OPENING_UNCONFIRMED
                 if turn_lifecycle.current_kind(spec.agent_id) == AgentState.SPAWNED
                 else TurnEvent.PROCESS_EXITED_FAILED)
        try:
            turn_lifecycle.transition(spec.agent_id, event, state_detail,
                                      expect_trace=spec.trace_id, anchor=anchor,
                                      s_open=spec.s_open)
        except turn_lifecycle.TransitionNotWritten as refused:
            name = {"write_busy": "callOpenEndWriteBusy",
                    "write_lost": "callOpenEndWriteLost"}.get(refused.reason, "callOpenEndRefused")
            log(name, f"agent={spec.agent_id} trace={spec.trace_id or '∅'} "
                      f"reason={refused.reason} — its interrupted state is not written")
            return False
        except turn_lifecycle.IllegalTransition as illegal:
            log("callOpenEndRefused",
                f"agent={spec.agent_id} trace={spec.trace_id or '∅'} reason=illegal "
                f"from={illegal.from_state or 'none'} — its interrupted state is not written")
            return False
        return True

    def _record_failure_row(self, spec: _TurnSpec, human: str, provider: str) -> None:
        """Put the failure in the chat, under the message that caused it.

        The agent's state carries it too, but that is replaced by the next
        turn; this row stays where the user reads the conversation. Routine
        automation nobody is waiting on gets none, as after a restart.
        """
        from . import interrupted_turns, message_store
        if spec.origin in interrupted_turns.SILENT_ORIGINS:
            return
        try:
            cause = agents_db.conn().execute(
                """SELECT message_id, backend_session_id FROM messages
                    WHERE agent_id = ? AND trace_id = ? AND role = 'user'
                    ORDER BY updated_at DESC LIMIT 1""",
                (spec.agent_id, spec.trace_id)).fetchone()
            if cause is None:
                log("turnFailureRowSkipped",
                    f"agent={spec.agent_id} trace={spec.trace_id or '∅'} — "
                    f"no user row carries this trace")
                return
            text = human + "."
            if provider and provider not in human:
                text += "\n\n" + provider[:1000]
            row = message_store.record_interruption_marker(
                agent_id=spec.agent_id,
                backend_session_id=str(cause["backend_session_id"] or ""),
                cause_message_id=str(cause["message_id"]), text=text,
                trace_id=spec.trace_id)
            if row is not None and getattr(self.ctx, "stream", None) is not None:
                events.broadcast(self.ctx.stream, events.transcript_updated(
                    agent_id=spec.agent_id, session=spec.session,
                    backend_session_id=str(cause["backend_session_id"] or "")))
        except Exception as exc:  # noqa: BLE001 - the state already carries it
            log_exception("turnFailureRowFail", exc, detail=spec.trace_id)
