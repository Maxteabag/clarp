"""ClaimMixin: methods moved out of lib/turn_dispatch.py::TurnDispatchService by tools/refactor/s3_apply.py
(group "turn_claim"). Generated: change the recipe, not this file."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from . import agents as agents_db
from . import eventlog
from . import tts_queue, turn_queue
from .turn_slots import defer_on
from .log import log, log_exception
from .turn_dispatch_state import _TURN_LOCK, _SLOTS, _INFLIGHT, _STOPPING_SENTINEL, _PROCESS_OWNER_PREFIX, _PROCESS_EXIT_POLL_S, _terminal_live
from .turn_command import DispatchResult, _TurnSpec, _park, _park_at_head, DispatchError, JanitorDispatchError, GoalWakeDispatchError, _validate_goal_dispatch, _is_call_opening, DispatchCommand


class ClaimMixin:
    def dispatch_queued(self, queue_id: str) -> DispatchResult:
        """Explicitly send one durable item while leaving the queue paused."""
        if turn_queue.status(queue_id) == "unknown":
            # Sending a listen-first opening whose launch cannot be known
            # would be a replay (docs/agent-rings.md "Claimed once").
            raise DispatchError(409, "opening launch unknown")
        runtime = getattr(self.ctx, "runtime_client", None)
        if runtime is not None:
            try:
                return runtime.dispatch_queued(queue_id)
            except Exception as exc:
                from .runtime_bridge import RuntimeUnavailable
                if isinstance(exc, RuntimeUnavailable):
                    raise DispatchError(503, str(exc)) from exc
                raise
        row = turn_queue.claim(queue_id)
        if not row:
            raise DispatchError(404, "queued message not found")
        agent_id = str(row["agent_id"])
        self.clear_recovery_backoff(agent_id, reason="send_queued")
        agent = agents_db.get_by_agent_id(agent_id)
        try:
            _validate_goal_dispatch(agent or {}, row["client_msg_id"])
        except GoalWakeDispatchError as exc:
            if exc.permanent:
                self._retire_goal_queue(row, exc)
            else:
                turn_queue.release_claim(queue_id)
            raise
        with _TURN_LOCK:
            busy = agent_id in _INFLIGHT
        if agent and not agents_db.interaction_capabilities(agent)["can_chat"]:
            turn_queue.release_claim(queue_id)
            raise JanitorDispatchError(409, "Janitor queues are managed by their configuration")
        if busy or (agent and self.backends.active_handles(
                self.backends.normalize(agent.get("backend")), agent_id)):
            turn_queue.release_claim(queue_id)
            raise DispatchError(409, "agent is still working")
        try:
            return self.submit(DispatchCommand(
                text=str(row["text"]), requested_session=str(row["session"]),
                trace_id=str(row["trace_id"]),
                synthesize_audio=bool(row["synthesize_audio"]),
                forced_session=str(row["session"]),
                client_msg_id=str(row["client_msg_id"]),
                origin=str(row["origin"]),
                sender_agent_id=str(row["sender_agent_id"]),
                prompt_admission_id=str(row["prompt_admission_id"]),
                queue_if_busy=True, skip_admission=True,
                durable_queue_id=queue_id, allow_paused_queue=True,
            ))
        except GoalWakeDispatchError as exc:
            if exc.permanent:
                self._retire_goal_queue(row, exc)
            else:
                turn_queue.release_claim(queue_id)
            raise
        except BaseException:
            turn_queue.release_claim(queue_id)
            raise

    def _enqueue_if_busy(self, spec: _TurnSpec, *, queue_if_busy: bool = False) -> bool:
        """Decide how to handle a new send for this agent:
        - Stop barrier up → park behind it, return True;
        - terminal attached → queue behind it, return True (caller doesn't spawn);
        - in-flight turn running → PREEMPT it (legacy non-steerable backend);
        - stale slot (no live process) → take it over, return False;
        - idle → claim the slot, return False.
        Returns True when the send waits instead of spawning.

        Only the decision is taken under _TURN_LOCK. The durable queue row is
        written before it (a no-op when admission already wrote it, as it does
        for every queue request), the Stop-park row is written before it and
        withdrawn after it if the barrier had gone, and eventlog rows and the
        preempting interrupt are deferred past the release."""
        if queue_if_busy:
            turn_queue.enqueue(
                queue_id=spec.queue_id, agent_id=spec.agent_id,
                session=spec.session, text=spec.text,
                trace_id=spec.trace_id, client_msg_id=spec.client_msg_id,
                synthesize_audio=spec.synthesize_audio, origin=spec.origin,
                sender_agent_id=spec.sender_agent_id,
            )
        park_id = ""
        if not spec.queue_id and _SLOTS.get(spec.agent_id) == _STOPPING_SENTINEL:
            park_id = _park(spec)
        elif (not spec.queue_id
              and str(_SLOTS.get(spec.agent_id) or "").startswith(_PROCESS_OWNER_PREFIX)):
            # It will wait behind a turn process: durably, like the send
            # that preempted that process (TP-01).
            park_id = _park(spec, prefix="preempt-park")
        parked = False
        preempt: list = []
        try:
            with _TURN_LOCK:
                parked, decision = self._claim_or_queue(
                    spec, queue_if_busy=queue_if_busy, park_id=park_id,
                    preempt=preempt)
        finally:
            if park_id and not parked:
                turn_queue.unpark(park_id)
        if preempt and self._preempt_for(spec, preempt[0]):
            decision = True     # it waits for the old process to exit
        if decision and _SLOTS.fenced:
            # Held behind the release drain: durable before anything else.
            self.persist_held(spec.agent_id)
        return decision

    def _hold_for_drain(self, spec: _TurnSpec, where: str) -> tuple[bool, bool]:
        """Queue a send behind the release drain's admission fence (the caller
        holds _TURN_LOCK). It starts after the handoff, from its durable row,
        or here when the fence is lifted."""
        depth = _SLOTS.enqueue(spec.agent_id, spec)
        defer_on(_TURN_LOCK, lambda: (
            eventlog.emit("server", "turnHeldForUpdate", context=spec.context,
                          detail={"depth": depth}),
            log("turnHeldForUpdate",
                f"agent={spec.agent_id} depth={depth} trace={spec.trace_id or '∅'} "
                f"— {where}; the runtime is draining for an update")))
        return False, True

    def _running_processes(self, spec: _TurnSpec) -> list:
        """The agent's turn processes that are positively still running."""
        try:
            handles = self.backends.active_handles(spec.backend, spec.agent_id)
        except Exception:  # noqa: BLE001
            return []
        running = []
        for handle in handles or ():
            try:
                if getattr(handle, "is_alive", lambda: False)():
                    running.append(handle)
            except Exception:  # noqa: BLE001
                continue
        return running

    def _wait_for_process_exit(self, spec: _TurnSpec, owner: str, depth: int) -> None:
        eventlog.emit("server", "turnQueuedBehindProcess", context=spec.context,
                      detail={"depth": depth})
        log("turnQueuedBehindProcess",
            f"agent={spec.agent_id} depth={depth} trace={spec.trace_id or '∅'} "
            f"— a turn process is still running; waiting for it to exit")
        holder = replace(spec, trace_id=owner)

        def wait() -> None:
            while _SLOTS.get(spec.agent_id) == owner and self._running_processes(spec):
                time.sleep(_PROCESS_EXIT_POLL_S)
            self._finish_turn(holder)

        threading.Thread(target=wait, daemon=True,
                         name=f"turn-wait-{spec.agent_id}").start()

    def _preempt_for(self, spec: _TurnSpec, killed: str) -> bool:
        """Interrupt the running turn for ``spec``. Two processes on one native
        session interleave and corrupt it, so when the old process is still
        running ``spec`` waits behind it, durably (a park row a restart
        recovers), and starts when it exits. Returns whether it waits; a send
        that cannot wait durably is refused."""
        try:
            self.backends.interrupt(spec.backend, spec.agent_id)
        except Exception as e:  # noqa: BLE001
            log_exception("preemptInterruptFail", e, detail=spec.agent_id)
        if killed and killed != spec.trace_id:
            # The preempted turn's speech not yet synthesized is dropped, not
            # spoken late over the new turn (VP-06).
            try:
                tts_queue.cancel(spec.agent_id, killed, reason="turn preempted")
            except Exception as e:  # noqa: BLE001
                log_exception("preemptSpeechCancelFail", e, detail=spec.agent_id)
        eventlog.emit("server", "turnPreempted", context=spec.context,
                      detail={"killed_trace": killed})
        log("turnPreempted",
            f"agent={spec.agent_id} killed={killed or '∅'} "
            f"new={spec.trace_id or '∅'} — busy, preempting and resuming")
        if not self._running_processes(spec):
            return False
        park_id = "" if spec.park_id else _park_at_head(spec)
        owner = f"{_PROCESS_OWNER_PREFIX}{spec.trace_id}"
        with _TURN_LOCK:
            if _SLOTS.get(spec.agent_id) != spec.trace_id:
                # A Stop or another send took the slot meanwhile.
                if park_id:
                    defer_on(_TURN_LOCK, lambda: turn_queue.unpark(park_id))
                return False
            if not (park_id or spec.park_id):
                _SLOTS.release(spec.agent_id, spec.trace_id)
                raise DispatchError(503, "could not durably queue message")
            _SLOTS.claim(spec.agent_id, owner)
            _SLOTS.mark_spawned(spec.agent_id, owner)
            _SLOTS.requeue_front(spec.agent_id, replace(
                spec, park_id=park_id or spec.park_id))
            depth = _SLOTS.queue_depth(spec.agent_id)
        self._wait_for_process_exit(spec, owner, depth)
        return True

    def _call_opening_claimed(self, spec: _TurnSpec) -> bool:
        """The dispatcher's claim of a listen-first initiative before it runs
        (agent_rings.claim_initiative): the queued row becomes claimed and the
        opening's intent started in one transaction, or the initiative was
        withdrawn by a greeting and must not run. Other turns pass."""
        if not _is_call_opening(spec):
            return True
        from . import agent_rings
        return agent_rings.claim_initiative(spec.queue_id or spec.client_msg_id)

    def _has_live_turn(self, spec: _TurnSpec) -> bool:
        """True if the agent currently has a real in-flight turn process. Used
        to detect a leaked in-flight slot (marked busy, but the process is
        gone)."""
        return self._agent_has_live_turn(spec.agent_id, spec.backend)

    def _agent_has_live_turn(self, agent_id: str, backend: str) -> bool:
        if _terminal_live(agent_id):
            return True  # interactive terminal holds the session
        try:
            return bool(self.backends.active_handles(backend, agent_id))
        except Exception:  # noqa: BLE001
            return True  # can't tell → assume live (don't double-spawn)
