"""Application service for routing and spawning one agent turn."""
from __future__ import annotations

import pathlib
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable

from . import agents as agents_db
from . import backend_usage, backends, config, db, error_classify, eventlog, origins
from . import message_store, team_store, tts_queue, turn_queue
from .log import log, log_exception
from .protocol import AgentState, SSEType
from .prompt_admissions import PromptAdmission
from .claude_failover import Attempt as ClaudeAttempt, ClaudeFailover
from . import prompt_admissions
from .transcript_log import find_latest_jsonl
from .send_service import (
    SendTarget,
    resolve_send_target,
    source_marker_path,
    source_marker_text,
)

# A turn gets this many attempts total before we give up and flip the agent
# to INTERRUPTED. Only connection-class failures consume retries; transient
# API errors and deliberate aborts notify immediately.
MAX_ATTEMPTS = 3
# Exponential backoff between attempts: 1s, then 2s.
BACKOFF_BASE_SEC = 1.0

# Per-agent turn serialization. A normal send to an agent that already has an
# in-flight turn QUEUES behind it and runs when that turn finishes — it does
# NOT SIGTERM the running turn (that lost the new message if its turn hadn't
# started, and abandoned the agent's work). Turns for one agent run strictly in
# arrival order. Only an explicit stop / barge-in (backends.interrupt, a
# separate path) preempts a running turn.
#   _INFLIGHT: agent_id -> trace_id of the turn currently running
#   _QUEUED:   agent_id -> list of _TurnSpec waiting to run, in order
_TURN_LOCK = threading.RLock()
_CLAUDE_FAILOVER = ClaudeFailover(_TURN_LOCK)
_INFLIGHT: dict[str, str] = {}
_QUEUED: dict[str, list] = {}
_CLAIMED_AT: dict[str, float] = {}
_RECOVERY_LOCK = threading.Lock()
_RUNTIME_CLIENT: Any | None = None

# Placeholder trace owning the in-flight slot while an interactive terminal is
# attached to an agent. A normal turn routed to that agent queues behind it
# (two processes resuming one session would corrupt the transcript); the queue
# drains via drain_after_terminal() when the terminal closes.
_TERMINAL_SENTINEL = "terminal"
_STOPPING_SENTINEL = "stopping"


def configure_runtime_client(client: Any | None) -> None:
    global _RUNTIME_CLIENT
    _RUNTIME_CLIENT = client


def runtime_status() -> dict[str, Any]:
    """Serializable ownership snapshot served to replaceable HTTP processes."""
    from . import compaction
    with _TURN_LOCK:
        return {
            "active": {
                agent_id: trace_id
                for agent_id, trace_id in _INFLIGHT.items()
                if trace_id not in {_TERMINAL_SENTINEL, _STOPPING_SENTINEL}
            },
            "terminals": sorted(
                agent_id for agent_id, trace_id in _INFLIGHT.items()
                if trace_id == _TERMINAL_SENTINEL
            ),
            "spawning": sorted(_CLAIMED_AT),
            "queued": {
                agent_id: len(items) for agent_id, items in _QUEUED.items()
                if items
            },
            "compactions": compaction.active_sessions(),
            "claude_account_recovery": _CLAUDE_FAILOVER.status(),
        }


def _terminal_live(agent_id: str) -> bool:
    try:
        from . import terminal_ws
        return terminal_ws.has_live_terminal(agent_id)
    except Exception:  # noqa: BLE001
        return False


def _slot_is_spawning(agent_id: str) -> bool:
    if _RUNTIME_CLIENT is not None:
        return agent_id in set(
            _RUNTIME_CLIENT.status().get("spawning") or ())
    return agent_id in _CLAIMED_AT


def free_stale_slot(agent_id: str) -> str | None:
    """INV3 (lib.reconcile): free an in-flight slot that has no live turn and
    nothing queued behind it. Returns the dead trace id, or None if nothing
    was freed. Spawning slots and terminal sentinels are left alone."""
    with _TURN_LOCK:
        if agent_id in _CLAIMED_AT or agent_id not in _INFLIGHT:
            return None
        if _QUEUED.get(agent_id):
            return None  # the next send / finish drains these; don't orphan them
        trace = _INFLIGHT.get(agent_id)
        if trace == _TERMINAL_SENTINEL:
            return None
        _INFLIGHT.pop(agent_id, None)
        return trace or ""


def _reset_hint(message: str) -> str:
    msg = message or ""
    patterns = (
        r"(?:try again|resets?)\s+(?:at|in)?\s*([^.\n,;]+(?:\([^)]+\))?)",
        r"(?:try again|resets?)\s+([^.\n,;]+(?:\([^)]+\))?)",
    )
    for pattern in patterns:
        m = re.search(pattern, msg, re.I)
        if m:
            value = " ".join(m.group(1).split())
            return value.strip(" .")
    return ""


def _spoken_failure_text(
    *,
    persona: str,
    category: str,
    human: str,
    message: str,
) -> str:
    name = (persona or "This agent").strip()
    if category == error_classify.USAGE_LIMIT:
        reset = _reset_hint(message)
        if reset:
            return f"{name} is out of usage. Try again at {reset}."
        return f"{name} is out of usage or credits right now."
    if category == error_classify.RUNNER_EXIT:
        return (
            f"{name} stopped before returning a reply. "
            "The command exited without usable output."
        )
    if category == error_classify.TRANSIENT:
        return f"{name} hit a temporary API error. Try again in a moment."
    if category == error_classify.CONNECTION:
        return f"{name} lost connection and retries were exhausted."
    return f"{name} was interrupted. {human}."


@dataclass(frozen=True)
class DispatchResult:
    session: str
    backend: str
    queued: bool = False
    queue_depth: int = 0
    queue_revision: int = 0


def _resolve_llm(agent: dict, backend: str) -> tuple[str, str]:
    """Effective (model, effort) for a turn: the per-agent override wins, else
    the global [agents] config default, else the CLI's own default ("").
    Effort is validated against what the backend's CLI accepts."""
    cfg = config.load()
    model = (agent.get("model") or "").strip()
    effort = (agent.get("effort") or "").strip()
    default_model, default_effort = backends.default_model_effort(backend, cfg)
    model = model or default_model
    effort = effort or default_effort
    return model.strip(), backends.clean_effort(backend, effort)


@dataclass(frozen=True)
class _TurnSpec:
    """Everything needed to (re)spawn one turn — captured once so a retry
    fires the identical dispatch from a timer thread."""
    backend: str
    text: str
    cwd: pathlib.Path
    backend_session_id: str
    is_new_session: bool
    session: str
    agent_id: str
    trace_id: str
    context: Any
    synthesize_audio: bool
    model: str = ""
    effort: str = ""
    client_msg_id: str = ""
    team_digest: str = ""
    team_inbox_ids: tuple[str, ...] = ()
    team_protocol: str = ""
    origin: str = "user"
    sender_agent_id: str = ""
    prompt_admission_id: str = ""
    queue_id: str = ""
    unheard_audio: bool = False
    # Provider-only continuation; the admitted user message keeps its original
    # text and client ID across an account change.
    recovery_text: str = ""


class DispatchError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def _default_retry_scheduler(delay: float, fn: Callable[[], None]) -> None:
    """Run `fn` after `delay` seconds on a daemon timer thread."""
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()


def _with_team_context(text: str, *, digest: str = "", protocol: str = "") -> str:
    """Wrap a turn's prompt with team context.

    Keep the stable protocol before per-turn user text and digest so provider
    prompt caching can reuse the repeated leader/team brief.
    """
    from .message_store import TEAM_CONTEXT_CLOSE, TEAM_CONTEXT_OPEN
    digest = (digest or "").strip()
    protocol = (protocol or "").strip()
    if not digest and not protocol:
        return text
    parts = [TEAM_CONTEXT_OPEN]
    if protocol:
        parts.append(protocol)
    if digest:
        if protocol:
            parts.append("")
        parts.append(digest)
    parts.append(TEAM_CONTEXT_CLOSE)
    parts.extend(["", text])
    return "\n".join(parts)


def _with_delivery_context(text: str, *, unheard_audio: bool = False) -> str:
    """Tell the agent what the transport delivered without altering chat text.

    The canonical user row remains exactly what the user said. This notice is
    injected only into the provider prompt so an agent does not assume that a
    synthesized reply was heard merely because it exists in its transcript.
    """
    if not unheard_audio:
        return text
    return (
        "<clarp-delivery-context>\n"
        "The user began a new spoken turn before hearing one or more of your "
        "previous synthesized replies. Treat those replies as unheard. Carry "
        "forward anything still important, but answer the user's newest message "
        "naturally instead of blindly repeating the old response.\n"
        "</clarp-delivery-context>\n\n"
        + text
    )


class TurnDispatchService:
    def __init__(self, ctx, *, backend_registry=backends,
                 home: pathlib.Path | None = None,
                 uuid_factory: Callable[[], str] | None = None,
                 now: Callable[[], float] = time.time,
                 retry_scheduler: Callable[[float, Callable[[], None]], None]
                 | None = None):
        self.ctx = ctx
        self.backends = backend_registry
        self.home = home or pathlib.Path.home()
        self.uuid_factory = uuid_factory or (lambda: str(uuid.uuid4()))
        self.now = now
        # Injectable so tests can run retries synchronously instead of on a
        # real timer thread.
        self.retry_scheduler = retry_scheduler or _default_retry_scheduler

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

    def _recover_queued_locked(self) -> int:
        turn_queue.reset_stale_claims()
        recovered = 0
        deferred = False
        for row in turn_queue.pending():
            if turn_queue.is_paused(row["agent_id"]):
                continue
            agent = agents_db.get_by_agent_id(row["agent_id"])
            if agent and self.backends.active_handles(
                    self.backends.normalize(agent.get("backend")), row["agent_id"]):
                deferred = True
                continue
            with _TURN_LOCK:
                already_memory_queued = any(
                    spec.queue_id == row["queue_id"]
                    for spec in _QUEUED.get(row["agent_id"], []))
            if already_memory_queued:
                continue
            try:
                self.dispatch(
                    text=row["text"], requested_session=row["session"],
                    trace_id=row["trace_id"],
                    synthesize_audio=bool(row["synthesize_audio"]),
                    forced_session=row["session"],
                    client_msg_id=row["client_msg_id"], origin=row["origin"],
                    sender_agent_id=row["sender_agent_id"],
                    prompt_admission_id=row["prompt_admission_id"],
                    queue_if_busy=True, skip_admission=True,
                    durable_queue_id=row["queue_id"],
                )
                recovered += 1
            except Exception as exc:  # keep ledger row for the next retry/restart
                log_exception("queuedRecoveryFail", exc, detail=row["session"])
        if deferred or turn_queue.claimed_count() > 0:
            self.retry_scheduler(1.0, self.recover_queued)
        return recovered

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
                 allow_paused_queue: bool = False) -> DispatchResult:
        runtime = getattr(self.ctx, "runtime_client", None)
        if runtime is not None:
            try:
                return runtime.dispatch(
                    text=text,
                    requested_session=requested_session,
                    trace_id=trace_id,
                    synthesize_audio=synthesize_audio,
                    forced_session=forced_session,
                    routed_by_orchestrator=routed_by_orchestrator,
                    client_msg_id=client_msg_id,
                    origin=origin,
                    sender_agent_id=sender_agent_id,
                    prompt_admission=prompt_admission,
                    prompt_admission_id=prompt_admission_id,
                    queue_if_busy=queue_if_busy,
                    skip_admission=skip_admission,
                    durable_queue_id=durable_queue_id,
                    unheard_audio_sessions=unheard_audio_sessions,
                    allow_paused_queue=allow_paused_queue,
                )
            except Exception as exc:
                from .runtime_bridge import RuntimeUnavailable
                if isinstance(exc, RuntimeUnavailable):
                    raise DispatchError(503, str(exc)) from exc
                raise
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
        if origin not in origins.ROUTINE_AUTOMATION_ORIGINS:
            self._notify_herald(session)

        agent = agents_db.get_by_session(session)
        if not agent:
            raise DispatchError(404, "unknown agent")
        agent_id = agent["agent_id"]
        if origin == "leader_tick" and not any(
            t.get("leader_enabled") and t.get("nudge_enabled")
            and t.get("leader_agent_id") == agent_id
            for t in team_store.list_teams()
        ):
            raise DispatchError(409, "team leader nudging is disabled")
        # Sticky focus: addressing an agent by name makes them the new default,
        # so subsequent un-named messages keep going to them (hands-free, you
        # don't want to re-say the name every turn).
        if target.routed_by_name or routed_by_orchestrator:
            try:
                agents_db.set_focus(agent_id)
            except Exception as e:
                log_exception("stickyFocusSetFail", e, detail=session)
        backend = self.backends.normalize(agent.get("backend"))
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
        # wedges the agent on every turn forever. If the resume target doesn't
        # exist, drop the binding and start fresh. (Claude only — its --resume is
        # transcript-file based; codex/agy resume differently.)
        if (backend_session_id and backend == self.backends.CLAUDE
                and find_latest_jsonl(
                    backend_session_id,
                    projects_root=self.home / ".claude" / "projects") is None):
            log("ghostSessionReset",
                f"agent={agent_id} bsid={backend_session_id} — resume target "
                f"has no transcript on disk; starting a fresh session")
            eventlog.emit("server", "ghostSessionReset", trace_id=trace_id,
                          session=session, detail={"bsid": backend_session_id})
            agents_db.end_current_runtime(agent_id)
            backend_session_id = ""
        is_new_session = not backend_session_id

        if is_new_session and backend == self.backends.CLAUDE:
            backend_session_id = self._bind_new_claude_session(agent_id, session)

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
        )
        # Queue receipts and ordinary user admissions share one SQLite write
        # transaction, preserving client_msg_id idempotency even if concurrent
        # retries switch between normal and queue delivery.
        if skip_admission:
            admission = True
        else:
            database = db.conn()
            database.execute("BEGIN IMMEDIATE")
            try:
                request_id = spec.client_msg_id or spec.trace_id
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
                        database.execute("COMMIT")
                        return DispatchResult(session=session, backend=backend)
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
                    admission = self._record_user_message(spec)
                database.execute("COMMIT")
            except BaseException:
                database.execute("ROLLBACK")
                raise
        if admission is False:
            eventlog.emit("server", "sendDeduplicated", context=spec.context)
            log("sendDeduplicated",
                f"agent={spec.agent_id} "
                f"client_msg_id={spec.client_msg_id or spec.trace_id}")
            return DispatchResult(session=session, backend=backend)

        if queue_if_busy and turn_queue.is_paused(spec.agent_id) and not allow_paused_queue:
            self._broadcast_queue_state(spec, started=False)
            queue_state = turn_queue.state(spec.agent_id)
            return DispatchResult(
                session=session, backend=backend, queued=True,
                queue_depth=queue_state["count"],
                queue_revision=queue_state["revision"])

        # A live Codex turn accepts follow-ups through the official turn/steer
        # protocol. Other backends retain their existing dispatch behavior.
        if not queue_if_busy and self._steer_if_supported(spec):
            return DispatchResult(session=session, backend=backend)
        if self._enqueue_if_busy(spec, queue_if_busy=queue_if_busy):
            if queue_if_busy:
                self._broadcast_queue_state(spec, started=False)
            queue_state = turn_queue.state(spec.agent_id)
            return DispatchResult(
                session=session, backend=backend, queued=True,
                queue_depth=queue_state["count"],
                queue_revision=queue_state["revision"])
        if queue_if_busy:
            # Idle queue request: it starts immediately, so admit its visible
            # user row now. Recovery may find the row already present.
            try:
                self._record_user_message(spec)
            except DispatchError:
                # The durable queue receipt remains authoritative. Release the
                # claimed slot and retry recovery instead of leaving a phantom
                # in-flight owner or requiring a client restart.
                with _TURN_LOCK:
                    if _INFLIGHT.get(spec.agent_id) == spec.trace_id:
                        _INFLIGHT.pop(spec.agent_id, None)
                        _CLAIMED_AT.pop(spec.agent_id, None)
                self.retry_scheduler(1.0, self.recover_queued)
                queue_state = turn_queue.state(spec.agent_id)
                return DispatchResult(
                    session=session, backend=backend, queued=True,
                    queue_depth=queue_state["count"],
                    queue_revision=queue_state["revision"])
        agents_db.open_turn(
            agent_id=agent_id, source="pwa", trace_id=trace_id,
            synthesize_audio=synthesize_audio)
        # This turn owns the agent's live trace.
        agents_db.set_trace_for_session(session, trace_id)
        try:
            if not self._spawn_attempt(spec, attempt=1):
                raise DispatchError(409, "turn superseded before spawn")
        except DispatchError:
            # Spawn never started: release the in-flight slot (and drain any
            # message that queued behind it) so the agent isn't wedged.
            if turn_queue.contains(spec.queue_id):
                with _TURN_LOCK:
                    if _INFLIGHT.get(spec.agent_id) == spec.trace_id:
                        _INFLIGHT.pop(spec.agent_id, None)
                        _CLAIMED_AT.pop(spec.agent_id, None)
                self.retry_scheduler(1.0, self.recover_queued)
            else:
                self._finish_turn(spec)
            raise
        self._mark_spawned(spec)
        return DispatchResult(session=session, backend=backend)

    def dispatch_queued(self, queue_id: str) -> DispatchResult:
        """Explicitly send one durable item while leaving the queue paused."""
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
        with _TURN_LOCK:
            busy = agent_id in _INFLIGHT
        agent = agents_db.get_by_agent_id(agent_id)
        if busy or (agent and self.backends.active_handles(
                self.backends.normalize(agent.get("backend")), agent_id)):
            turn_queue.release_claim(queue_id)
            raise DispatchError(409, "agent is still working")
        try:
            return self.dispatch(
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
            )
        except BaseException:
            turn_queue.release_claim(queue_id)
            raise

    def _enqueue_if_busy(self, spec: _TurnSpec, *, queue_if_busy: bool = False) -> bool:
        """Decide how to handle a new send for this agent:
        - terminal attached → queue behind it, return True (caller doesn't spawn);
        - in-flight turn running → PREEMPT it (legacy non-steerable backend);
        - stale slot (no live process) → take it over, return False;
        - idle → claim the slot, return False.
        Returns True only in the terminal-queue case."""
        with _TURN_LOCK:
            if _INFLIGHT.get(spec.agent_id) == _STOPPING_SENTINEL:
                # This send was admitted just as Stop acquired the barrier.
                # It is newer than the stopped work, so hold it in memory and
                # start it only after the backend-wide interrupt returns.
                _QUEUED.setdefault(spec.agent_id, []).append(spec)
                return True
            # An interactive terminal is attached to this agent — queue behind
            # it so we never run a -p turn against a session another process is
            # holding open. drain_after_terminal() spawns this when it closes.
            # Other agents are unaffected; this only serializes the same agent.
            if _terminal_live(spec.agent_id):
                if queue_if_busy:
                    turn_queue.enqueue(
                        queue_id=spec.queue_id, agent_id=spec.agent_id,
                        session=spec.session, text=spec.text,
                        trace_id=spec.trace_id, client_msg_id=spec.client_msg_id,
                        synthesize_audio=spec.synthesize_audio, origin=spec.origin,
                        sender_agent_id=spec.sender_agent_id,
                    )
                _QUEUED.setdefault(spec.agent_id, []).append(spec)
                _INFLIGHT.setdefault(spec.agent_id, _TERMINAL_SENTINEL)
                depth = len(_QUEUED[spec.agent_id])
                eventlog.emit("server", "turnQueuedBehindTerminal",
                              context=spec.context, detail={"depth": depth})
                log("turnQueuedBehindTerminal",
                    f"agent={spec.agent_id} depth={depth} "
                    f"trace={spec.trace_id or '∅'} — terminal live, queued")
                return True
            if spec.agent_id in _INFLIGHT:
                if _slot_is_spawning(spec.agent_id):
                    if queue_if_busy:
                        turn_queue.enqueue(
                            queue_id=spec.queue_id, agent_id=spec.agent_id,
                            session=spec.session, text=spec.text,
                            trace_id=spec.trace_id,
                            client_msg_id=spec.client_msg_id,
                            synthesize_audio=spec.synthesize_audio,
                            origin=spec.origin,
                            sender_agent_id=spec.sender_agent_id,
                        )
                    _QUEUED.setdefault(spec.agent_id, []).append(spec)
                    return True
                # Self-heal a leaked slot: if the in-flight turn has no live
                # process (it died without firing its terminal callback — e.g.
                # killed mid-flight by a restart), the slot is stale. Free it
                # and take it over now instead of queuing behind a phantom
                # forever. Checked here, on every send — no timer/interval.
                if not self._has_live_turn(spec):
                    stale = _INFLIGHT.get(spec.agent_id)
                    eventlog.emit("server", "staleInflightCleared",
                                  context=spec.context,
                                  detail={"dead_trace": stale})
                    log("staleInflightCleared",
                        f"agent={spec.agent_id} dead_trace={stale or '∅'} "
                        f"— in-flight turn has no live process; freeing slot")
                    # Surviving queued specs (if any) still drain when this
                    # turn finishes; nothing is dropped.
                    _INFLIGHT[spec.agent_id] = spec.trace_id
                    _CLAIMED_AT[spec.agent_id] = time.monotonic()
                    return False
                if queue_if_busy:
                    turn_queue.enqueue(
                        queue_id=spec.queue_id, agent_id=spec.agent_id,
                        session=spec.session, text=spec.text,
                        trace_id=spec.trace_id, client_msg_id=spec.client_msg_id,
                        synthesize_audio=spec.synthesize_audio, origin=spec.origin,
                        sender_agent_id=spec.sender_agent_id,
                    )
                    _QUEUED.setdefault(spec.agent_id, []).append(spec)
                    depth = len(_QUEUED[spec.agent_id])
                    eventlog.emit("server", "turnQueued", context=spec.context,
                                  detail={"depth": depth})
                    log("turnQueued",
                        f"agent={spec.agent_id} depth={depth} "
                        f"trace={spec.trace_id or '∅'}")
                    return True
                killed = _INFLIGHT.get(spec.agent_id)
                try:
                    self.backends.interrupt(spec.backend, spec.agent_id)
                except Exception as e:  # noqa: BLE001
                    log_exception("preemptInterruptFail", e, detail=spec.agent_id)
                eventlog.emit("server", "turnPreempted", context=spec.context,
                              detail={"killed_trace": killed})
                log("turnPreempted",
                    f"agent={spec.agent_id} killed={killed or '∅'} "
                    f"new={spec.trace_id or '∅'} — busy, preempting and resuming")
                _INFLIGHT[spec.agent_id] = spec.trace_id
                _CLAIMED_AT[spec.agent_id] = time.monotonic()
                return False
            _INFLIGHT[spec.agent_id] = spec.trace_id
            _CLAIMED_AT[spec.agent_id] = time.monotonic()
            return False

    def _steer_if_supported(self, spec: _TurnSpec) -> bool:
        """Append a follow-up to an active steerable turn without replacing it."""
        with _TURN_LOCK:
            busy = spec.agent_id in _INFLIGHT
        if not busy or _terminal_live(spec.agent_id):
            return False
        steer = getattr(self.backends, "steer_turn", None)
        if steer is None:
            return False
        try:
            digest, inbox_ids = team_store.pending_digest(spec.agent_id)
            spec = replace(spec, team_digest=digest, team_inbox_ids=tuple(inbox_ids),
                           team_protocol=team_store.team_protocol_instruction(spec.agent_id, turn_origin=spec.origin))
            steer_text = _with_team_context(
                _with_delivery_context(
                    spec.text, unheard_audio=spec.unheard_audio),
                digest=spec.team_digest, protocol=spec.team_protocol)
            accepted = bool(steer(
                spec.backend, spec.agent_id, steer_text,
                client_msg_id=spec.client_msg_id,
                synthesize_audio=spec.synthesize_audio,
            ))
        except Exception as e:  # noqa: BLE001
            log_exception("turnSteerFail", e, detail=spec.agent_id)
            return False
        if accepted:
            team_store.mark_injected(spec.agent_id, spec.team_inbox_ids)
            eventlog.emit("server", "turnSteered", context=spec.context,
                          detail={"active_trace": _INFLIGHT.get(spec.agent_id)})
            log("turnSteered", f"agent={spec.agent_id} trace={spec.trace_id or '∅'}")
        return accepted

    def _has_live_turn(self, spec: _TurnSpec) -> bool:
        """True if the agent currently has a real in-flight turn process. Used
        to detect a leaked in-flight slot (marked busy, but the process is
        gone)."""
        if _terminal_live(spec.agent_id):
            return True  # interactive terminal holds the session
        try:
            return bool(self.backends.active_handles(spec.backend, spec.agent_id))
        except Exception:  # noqa: BLE001
            return True  # can't tell → assume live (don't double-spawn)

    def _finish_turn(self, spec: _TurnSpec) -> None:
        """A turn reached a terminal state. If a message queued behind it, take
        over the in-flight slot and spawn it; otherwise free the slot. Guarded
        by trace so a duplicate terminal callback (or a superseded turn) is a
        no-op."""
        agent_id = spec.agent_id
        with _TURN_LOCK:
            if _INFLIGHT.get(agent_id) != spec.trace_id:
                return  # not the current turn — already drained / superseded
            _CLAUDE_FAILOVER.discard(agent_id, spec.trace_id)
            queue = _QUEUED.get(agent_id)
            next_spec = queue.pop(0) if queue else None
            if next_spec is None:
                _INFLIGHT.pop(agent_id, None)
                _CLAIMED_AT.pop(agent_id, None)
                _QUEUED.pop(agent_id, None)
                return
            _INFLIGHT[agent_id] = next_spec.trace_id
            _CLAIMED_AT[agent_id] = time.monotonic()
        self._resume_and_spawn(agent_id, next_spec)

    def _resume_and_spawn(self, agent_id: str, next_spec: _TurnSpec) -> None:
        """Spawn a queued spec that just took over the in-flight slot. The prior
        owner (a finished turn, or a closed terminal) may have created/advanced
        the backend session, so re-resolve it before spawning."""
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
        bsid = (agents_db.live_backend_session(agent_id)
                or next_spec.backend_session_id)
        next_spec = replace(next_spec, backend_session_id=bsid,
                            is_new_session=not bsid)
        agents_db.set_trace_for_session(next_spec.session, next_spec.trace_id)
        try:
            if next_spec.queue_id:
                # Queue admission is intentionally invisible. Materialize the
                # ordinary user turn only at the moment execution begins.
                self._record_user_message(next_spec)
            agents_db.open_turn(
                agent_id=next_spec.agent_id, source="pwa",
                trace_id=next_spec.trace_id,
                synthesize_audio=next_spec.synthesize_audio)
            if not self._spawn_attempt(next_spec, attempt=1):
                return
            self._mark_spawned(next_spec)
        except DispatchError as e:
            log_exception("queuedSpawnFail", e, detail=next_spec.session)
            if not next_spec.queue_id:
                # Legacy automatic terminal queue is memory-only; preserve its
                # existing behavior and continue to the next waiting spec.
                self._finish_turn(next_spec)
                return
            # Keep the durable head and retry it before later queue entries.
            # The client already received queued=true, so dropping it here
            # would silently lose acknowledged work.
            with _TURN_LOCK:
                if _INFLIGHT.get(agent_id) == next_spec.trace_id:
                    _INFLIGHT.pop(agent_id, None)
                    _CLAIMED_AT.pop(agent_id, None)
            self.retry_scheduler(1.0, self.recover_queued)

    def _mark_spawned(self, spec: _TurnSpec) -> None:
        with _TURN_LOCK:
            recovering = _CLAUDE_FAILOVER.attempts.get(spec.agent_id)
            if (recovering and recovering.trace_id == spec.trace_id
                    and recovering.state.get("account_recovery")):
                return
            if _INFLIGHT.get(spec.agent_id) == spec.trace_id:
                _CLAIMED_AT.pop(spec.agent_id, None)
        turn_queue.mark_started(spec.queue_id)
        if spec.origin == "oracle":
            from . import oracle_delegations
            oracle_delegations.mark_started_for_trace(spec.trace_id)
        if spec.queue_id:
            self._broadcast_queue_state(spec, started=True)
        try:
            # A very fast backend may complete before spawn_turn returns. Do
            # not overwrite its terminal state with a late THINKING record.
            latest = agents_db.latest_state(spec.agent_id)
            if (latest and latest.get("kind") in {
                    AgentState.DONE, AgentState.IDLE, AgentState.INTERRUPTED}):
                detail = latest.get("detail") or {}
                if (detail.get("trace_id") == spec.trace_id
                        or agents_db.get_trace(spec.agent_id) != spec.trace_id):
                    return
            agents_db.record_state(
                spec.agent_id,
                AgentState.THINKING,
                {
                    "source": "pwa",
                    "dispatch": spec.backend,
                    "origin": spec.origin,
                    "trace_id": spec.trace_id,
                    "backend_session_id": spec.backend_session_id,
                },
            )
            if getattr(self.ctx, "stream", None) is not None:
                self.ctx.stream.broadcast({
                    "type": SSEType.AGENT_STATE,
                    "session": spec.session,
                    "agent_id": spec.agent_id,
                    "kind": AgentState.THINKING,
                    "trace_id": spec.trace_id,
                    "client_msg_id": spec.client_msg_id,
                    "queue_started": bool(spec.queue_id),
                    "queue_remaining": turn_queue.pending_count(spec.agent_id),
                })
        except Exception as e:
            log_exception("spawnStateFail", e, detail=spec.session)

    def _broadcast_queue_state(self, spec: _TurnSpec, *, started: bool) -> None:
        if getattr(self.ctx, "stream", None) is None:
            return
        queue_state = turn_queue.state(spec.agent_id)
        self.ctx.stream.broadcast({
            "type": SSEType.QUEUE_UPDATED,
            "session": spec.session,
            "agent_id": spec.agent_id,
            "client_msg_id": spec.client_msg_id,
            "queue_depth": queue_state["count"],
            "queue_paused": queue_state["paused"],
            "queue_started": started,
            "queue_revision": queue_state["revision"],
        })

    def _record_user_message(self, spec: _TurnSpec) -> bool | None:
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
                self.ctx.stream.broadcast({
                    "type": SSEType.TRANSCRIPT_UPDATED,
                    "agent_id": spec.agent_id,
                    "session": spec.session,
                    "backend_session_id": spec.backend_session_id,
                })
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

    def _write_source_marker(self, *, session: str, trace_id: str,
                             synthesize_audio: bool) -> None:
        try:
            path = source_marker_path(self.home, session)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source_marker_text(
                session=session, trace_id=trace_id, now=self.now(),
                synthesize_audio=synthesize_audio))
        except OSError as e:
            log_exception("sendLastSourceWriteFail", e)

    def _notify_herald(self, session: str) -> None:
        herald = getattr(self.ctx, "herald", None)
        if herald is None:
            return
        try:
            herald.set_awaiting(session)
        except Exception as e:
            log_exception("heraldSetAwaitingFail", e, detail=session)

    def _bind_new_claude_session(self, agent_id: str, session: str) -> str:
        backend_session_id = self.uuid_factory()
        try:
            agents_db.bind_backend_session(agent_id, backend_session_id)
        except agents_db.SessionAlreadyBound as e:
            log_exception("sendPreStampCollision", e, detail=session)
            backend_session_id = self.uuid_factory()
            agents_db.bind_backend_session(agent_id, backend_session_id)
        except Exception as e:
            log_exception("sendPreStampSessionFail", e, detail=session)
            raise DispatchError(500, "could not bind backend session") from e
        return backend_session_id

    def _spawn_attempt(self, spec: _TurnSpec, *, attempt: int) -> bool:
        """Reject a stale attempt; AGY rechecks around state/Popen itself."""
        with _TURN_LOCK:
            if _INFLIGHT.get(spec.agent_id) != spec.trace_id:
                log("spawnAbandoned",
                    f"agent={spec.agent_id} trace={spec.trace_id or '∅'} — "
                    "ownership lost before spawn")
                return False
        self._spawn_attempt_claimed(spec, attempt=attempt)
        return True

    def _resume_after_account_switch(self, spec, attempt, state):
        if (_INFLIGHT.get(spec.agent_id) != spec.trace_id
                or self._superseded(spec)):
            return
        bsid = state.get("backend_session_id") or spec.backend_session_id
        transcript = find_latest_jsonl(
            bsid, projects_root=self.home / ".claude" / "projects") if bsid else None
        interrupted = (state.get("spawn_started") or attempt > 1
                       or bool(spec.recovery_text))
        resume_spec = replace(
            spec, backend_session_id=bsid,
            is_new_session=spec.is_new_session and transcript is None,
            recovery_text=(
                "Clarp recovered from a Claude account usage limit. Continue "
                "the unfinished request from the existing conversation. Check "
                "the results of interrupted operations before retrying them; "
                "do not repeat work or external actions already completed. "
                "If the request is already complete, report that and stop.\n\n"
                f"Original request:\n{spec.text}"
            ) if interrupted and transcript is not None else "",
        )
        try:
            if self._spawn_attempt(resume_spec, attempt=attempt):
                self._mark_spawned(resume_spec)
        except DispatchError as exc:
            self._mark_interrupted(resume_spec, error_classify.RUNNER_EXIT,
                                   str(exc), attempts=attempt)

    def _spawn_attempt_claimed(self, spec: _TurnSpec, *, attempt: int) -> None:
        """Spawn one attempt of a turn. Attempt 1 surfaces spawn failures as
        a DispatchError (so /send returns 500); later attempts run from a
        timer thread and just mark the agent INTERRUPTED on failure."""
        if spec.origin == "leader_tick" and not any(
            t.get("leader_enabled") and t.get("nudge_enabled")
            and t.get("leader_agent_id") == spec.agent_id
            for t in team_store.list_teams()
        ):
            raise DispatchError(409, "team leader nudging is disabled")
        digest, inbox_ids = team_store.pending_digest(spec.agent_id)
        spec = replace(spec, team_digest=digest, team_inbox_ids=tuple(inbox_ids),
                       team_protocol=team_store.team_protocol_instruction(spec.agent_id, turn_origin=spec.origin))
        # Re-arm the Claude pwa-voice source marker for THIS attempt, with a
        # fresh timestamp. The marker is single-use (the UserPromptSubmit hook
        # consumes it), so without re-writing here a retry — or a redispatch
        # after preempting an in-flight turn — would fire its hook with no
        # marker, tag the turn `local`, and the Stop hook would skip TTS.
        if spec.backend == self.backends.CLAUDE:
            self._write_source_marker(
                session=spec.session, trace_id=spec.trace_id,
                synthesize_audio=spec.synthesize_audio)
        # Mutable across this attempt's callbacks: did system.init land?
        # A retry of a never-initialised new session must keep --session-id.
        state = {"saw_init": False, "backend_session_id": spec.backend_session_id}
        account_attempt = None
        if (spec.backend == self.backends.CLAUDE
                and (config.load().claude_account_switch_command
                     or _CLAUDE_FAILOVER.recovering)):
            def pause():
                _CLAIMED_AT[spec.agent_id] = time.monotonic()
                agents_db.record_state(
                    spec.agent_id, AgentState.THINKING,
                    {"dispatch": spec.backend, "trace_id": spec.trace_id,
                     "account_recovery": "waiting",
                     "message": "Waiting for a Claude account with available usage"})

            account_attempt = ClaudeAttempt(
                agent_id=spec.agent_id, trace_id=spec.trace_id, model=spec.model,
                state=state, owned=lambda: (
                    _INFLIGHT.get(spec.agent_id) == spec.trace_id
                    and not self._superseded(spec)), pause=pause,
                resume=lambda: self._resume_after_account_switch(spec, attempt, state))
            if _CLAUDE_FAILOVER.register(account_attempt):
                return
        on_init, on_result, on_error = self._attempt_callbacks(spec, attempt, state)
        def run_if_owned(action) -> bool:
            with _TURN_LOCK:
                if _INFLIGHT.get(spec.agent_id) != spec.trace_id:
                    return False
                action()
                return True
        try:
            prompt = _with_team_context(
                _with_delivery_context(
                    spec.recovery_text or spec.text, unheard_audio=spec.unheard_audio),
                digest=spec.team_digest, protocol=spec.team_protocol)
            if spec.recovery_text:
                # The outer envelope is filtered by the native transcript
                # importer, including when team/delivery context is present.
                prompt = f"<clarp-account-recovery>\n{prompt}\n</clarp-account-recovery>"
            state["spawn_started"] = True
            handle = self.backends.spawn_turn(
                spec.backend,
                text=prompt,
                cwd=spec.cwd,
                backend_session_id=spec.backend_session_id,
                is_new_session=spec.is_new_session,
                session=spec.session,
                agent_id=spec.agent_id,
                on_session_init=on_init,
                on_result=on_result,
                on_error=on_error,
                trace_id=spec.trace_id,
                stream=self.ctx.stream,
                voice_preamble=spec.synthesize_audio,
                synthesize_audio=spec.synthesize_audio,
                model=spec.model,
                effort=spec.effort,
                run_if_owned=run_if_owned,
            )
            team_store.mark_injected(spec.agent_id, spec.team_inbox_ids)
            if account_attempt is not None:
                account_attempt.handle = handle
        except FileNotFoundError as e:
            if attempt == 1:
                log("backendMissing", str(e))
                raise DispatchError(500, str(e)) from e
            log_exception("retrySpawnMissing", e, detail=spec.session)
            self._mark_interrupted(spec, error_classify.CONNECTION, str(e),
                                   attempts=attempt)
        except Exception as e:
            if attempt == 1:
                log_exception("backendSpawnFail", e, detail=spec.session)
                raise DispatchError(500, f"{spec.backend} spawn failed: {e}") from e
            log_exception("retrySpawnFail", e, detail=spec.session)
            self._mark_interrupted(spec, error_classify.CONNECTION, str(e),
                                   attempts=attempt)

        finally:
            if account_attempt is not None:
                account_attempt.spawned.set()

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
            if (self._superseded(spec) or state.get("account_recovery")
                    or state.get("outcome_seen")):
                return
            state["outcome_seen"] = True
            try:
                if state.get("bind_error"):
                    self._handle_failure(
                        spec, attempt, state, error_classify.RUNNER_EXIT,
                        f"backend session bind failed: {state['bind_error']}")
                    return
                category = error_classify.classify_result(event)
                if category != error_classify.CLEAN:
                    self._handle_failure(spec, attempt, state, category,
                                         _result_error_text(event))
                    return
                detail = _result_detail(event, trace_id=trace_id)
                # DONE is the durable completion signal consumed by the
                # user-facing push and unread/badge policy. It is non-busy,
                # just like IDLE, but preserves the completion edge.
                agents_db.record_state(agent_id, AgentState.DONE, detail)
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
            if (self._superseded(spec) or state.get("account_recovery")
                    or state.get("outcome_seen")):
                return
            state["outcome_seen"] = True
            try:
                category = (error_classify.RUNNER_EXIT if state.get("bind_error")
                            else error_classify.classify_error(message))
                if state.get("bind_error"):
                    message = f"backend session bind failed: {state['bind_error']}"
                self._handle_failure(spec, attempt, state, category, message)
            except Exception as e:
                log_exception("clarpOnErrorFail", e, detail=trace_id)

        if spec.backend == self.backends.CLAUDE:
            def guarded(callback):
                def invoke(*args):
                    with _TURN_LOCK:
                        return callback(*args)
                return invoke
            return guarded(on_init), guarded(on_result), guarded(on_error)
        return on_init, on_result, on_error

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
        if (category == error_classify.USAGE_LIMIT
                and spec.backend == self.backends.CLAUDE
                and _CLAUDE_FAILOVER.request(
                    spec.agent_id, spec.trace_id,
                    config.load().claude_account_switch_command)):
            eventlog.emit("server", "claudeAccountRecovery", context=spec.context,
                          detail={"reason": "usage_limit"})
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
        if category in error_classify.NOTIFY:
            self._mark_interrupted(spec, category, msg, attempts=attempt)
            if spec.origin == "heartbeat":
                try:
                    from . import heartbeat
                    heartbeat.record_heartbeat_noop(spec.agent_id, is_interrupted=True)
                except Exception as exc:  # noqa: BLE001
                    log_exception("heartbeatFailureNoopFail", exc, detail=spec.agent_id)
            return
        # Unrecognised failure: keep the old behaviour — flip to IDLE so the
        # UI doesn't hang on THINKING, and log the turn as failed.
        agents_db.record_state(
            spec.agent_id, AgentState.IDLE,
            {"dispatch": spec.backend, "error": msg[:200]},
        )
        eventlog.emit("server", "clarpTurnFail", context=spec.context,
                      detail={"err": msg})
        from . import oracle_delegations
        oracle_delegations.fail_for_trace(spec.trace_id, msg or "Agent turn failed")
        # Terminal (gave up): drain the queue.
        self._finish_turn(spec)

    def _schedule_retry(self, spec: _TurnSpec, attempt: int, state: dict,
                        message: str) -> None:
        next_attempt = attempt + 1
        delay = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
        # Keep the agent visibly busy across the gap rather than flicking to
        # idle and back; detail marks it as a reconnect, not fresh work.
        agents_db.record_state(
            spec.agent_id, AgentState.THINKING,
            {"dispatch": spec.backend, "reconnect": next_attempt,
             "of": MAX_ATTEMPTS, "after_ms": int(delay * 1000)},
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
            with _TURN_LOCK:
                if state.get("account_recovery"):
                    return  # account recovery already owns this continuation
                if _INFLIGHT.get(spec.agent_id) != spec.trace_id:
                    log("retryAbandoned",
                        f"agent={spec.agent_id} trace={spec.trace_id or '∅'} — "
                        f"preempted during backoff")
                    return
                self._spawn_attempt(next_spec, attempt=next_attempt)

        self.retry_scheduler(delay, _retry_spawn)

    def _mark_interrupted(self, spec: _TurnSpec, category: str,
                          message: str | None, *, attempts: int) -> None:
        human = {
            error_classify.CONNECTION: "Connection lost — retries exhausted",
            error_classify.TRANSIENT: "API unavailable (overloaded / rate limited)",
            error_classify.INTERRUPTED: "Turn interrupted",
            error_classify.USAGE_LIMIT: "Usage limit reached",
            error_classify.RUNNER_EXIT: "Agent process exited unexpectedly",
            error_classify.TIMEOUT: "Turn timed out — backend stopped responding",
        }.get(category, "Turn interrupted")
        limit_event = None
        if category == error_classify.USAGE_LIMIT and spec.backend == backends.CODEX:
            try:
                limit_event = backend_usage.record_classified_usage_limit(
                    backends.CODEX)
                if limit_event:
                    for related in limit_event.get("_additional_events") or []:
                        self.ctx.stream.broadcast(related)
                    if limit_event.get("_new"):
                        self.ctx.stream.broadcast({
                            key: value for key, value in limit_event.items()
                            if not key.startswith("_")
                        })
            except Exception as exc:  # noqa: BLE001
                log_exception("providerLimitRecordFail", exc, detail=spec.trace_id)
        state_detail = {
            "dispatch": spec.backend, "reason": category, "message": human,
            "error": (message or "")[:200], "attempts": attempts,
        }
        if limit_event:
            state_detail["provider_limit_event_id"] = limit_event[
                "provider_limit_event_id"]
        agents_db.record_state(
            spec.agent_id, AgentState.INTERRUPTED,
            state_detail,
        )
        self._speak_interruption(spec, category, human, message)
        eventlog.emit("server", "turnInterrupted", context=spec.context,
                      detail={"reason": category, "attempts": attempts,
                              "err": (message or "")[:300]})
        log("turnInterrupted",
            f"agent={spec.agent_id} reason={category} attempts={attempts} "
            f"trace={spec.trace_id or '∅'}")
        from . import oracle_delegations
        oracle_delegations.fail_for_trace(
            spec.trace_id, (message or human)[:500])
        # Terminal: a killed/interrupted turn still drains anything queued
        # behind it (e.g. an explicit stop, then your next message runs).
        self._finish_turn(spec)

    def _speak_interruption(
        self,
        spec: _TurnSpec,
        category: str,
        human: str,
        message: str | None,
    ) -> None:
        # Interruptions are recorded as agent state + a turnInterrupted event
        # (see _mark_interrupted) so the UI can surface them, but they are no
        # longer spoken aloud — hearing raw failure text read out was jarring.
        # Flip this return to re-enable voiced interruption notices.
        return
        if not spec.synthesize_audio:
            return
        try:
            agent = agents_db.get_by_agent_id(spec.agent_id)
            if not agent:
                return
            text = _spoken_failure_text(
                persona=agent.get("persona") or spec.session,
                category=category,
                human=human,
                message=message or "",
            )
            tts_queue.enqueue(
                agent_id=spec.agent_id,
                text=text,
                voice_id=agent.get("voice_id") or "",
                session=spec.session,
                source="turn_interrupted",
                trace_id=spec.trace_id,
                synthesize_audio=True,
            )
            eventlog.emit(
                "server",
                "turnInterruptedSpoken",
                context=spec.context,
                detail={"reason": category, "text": text},
            )
        except Exception as e:  # noqa: BLE001
            log_exception("turnInterruptedSpeakFail", e, detail=spec.session)

    def _sticky_session(self) -> str:
        """Session of the currently-focused agent — the last one addressed by
        name (or selected in the UI). Un-named messages stick to it. Empty if
        there's no live focus, so routing falls back to the client's request."""
        try:
            agent_id = agents_db.get_focus()
            if not agent_id:
                return ""
            agent = agents_db.get_by_agent_id(agent_id)
            return agent["session"] if agent else ""
        except Exception:
            return ""

    def _superseded(self, spec: _TurnSpec) -> bool:
        """True if a newer turn has taken over this agent since `spec` was
        dispatched. A preempted/old turn's drain thread fires its terminal
        callback asynchronously — if we let it record INTERRUPTED/IDLE it would
        clobber the new turn's THINKING and leave the pill looking idle while
        the agent is actually working. The agent's current trace is the newest
        /send's; a stale turn carries an older one. Same-trace retries are NOT
        superseded (they share spec.trace_id), so they still record normally."""
        try:
            current = agents_db.get_trace(spec.agent_id)
        except Exception:
            return False
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


def _existing_cwd(raw: Any) -> pathlib.Path:
    from .launch_paths import existing_workspace_path
    return existing_workspace_path(raw)


def _result_error_text(event: Any) -> str:
    """Best-effort human-readable error text out of an error-result event."""
    if not isinstance(event, dict):
        return ""
    for k in ("result", "error", "message", "subtype"):
        v = event.get(k)
        if v:
            return str(v)
    return "error result"


def _result_detail(event: dict, *, trace_id: str) -> dict:
    usage = (event.get("usage") or {}) if isinstance(event, dict) else {}
    detail: dict = {"dispatch": "clarp", "trace_id": trace_id}
    if usage:
        detail["tokens_in"] = int(
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
        )
        detail["tokens_out"] = int(usage.get("output_tokens") or 0)
    cost = event.get("total_cost_usd") if isinstance(event, dict) else None
    if cost is not None:
        detail["cost_usd"] = float(cost)
    duration = event.get("duration_ms") if isinstance(event, dict) else None
    if duration is not None:
        detail["duration_ms"] = int(duration)
    return detail


def _result_assistant_text(event: dict) -> str:
    return str(
        event.get("_assistant_text")
        or event.get("last_agent_message")
        or event.get("result")
        or event.get("message")
        or ""
    ).strip()


def _record_dreaming_result(agent_id: str, event: dict) -> None:
    text = _result_assistant_text(event)
    if not text.strip():
        return
    try:
        from . import dreaming
        dreaming.process_assistant_text(agent_id, text, live=False)
    except Exception as e:  # noqa: BLE001
        log_exception("dreamingResultParseFail", e, detail=agent_id)


def clear_for_agent(
    agent_id: str, *, preserve_queue: bool = False, pause_queue: bool = False
) -> int:
    """Drop the in-flight slot and optionally queued turns for an agent. Used by /stop:
    a SIGTERM'd turn may die without firing its terminal callback, so the slot
    would otherwise leak until the next send self-heals it. Returns the number of
    queued turns dropped."""
    with _TURN_LOCK:
        # Stop needs pause + in-memory detachment to be atomic with
        # _finish_turn(), otherwise the interrupted callback can drain the
        # next item in between those two operations.
        if pause_queue:
            turn_queue.set_paused(agent_id, True)
        if pause_queue:
            _INFLIGHT[agent_id] = _STOPPING_SENTINEL
        else:
            _INFLIGHT.pop(agent_id, None)
        _CLAIMED_AT.pop(agent_id, None)
        dropped = len(_QUEUED.pop(agent_id, []) or [])
    durable_dropped = 0 if preserve_queue else turn_queue.remove_for_agent(agent_id)
    return max(dropped, durable_dropped)


def owns_inflight_trace(agent_id: str, trace_id: str) -> bool:
    """Whether this server process still owns the exact running turn."""
    if _RUNTIME_CLIENT is not None:
        try:
            active = _RUNTIME_CLIENT.status().get("active") or {}
            return bool(trace_id) and str(active.get(agent_id) or "") == trace_id
        except Exception:
            return bool(trace_id) and agents_db.is_busy(agent_id) and \
                agents_db.get_trace(agent_id) == trace_id
    with _TURN_LOCK:
        return bool(trace_id) and _INFLIGHT.get(agent_id) == trace_id


def snapshot_stop_state(agent_id: str) -> dict:
    with _TURN_LOCK:
        value = _INFLIGHT.get(agent_id)
        return {
            "trace_id": value if value not in {None, _STOPPING_SENTINEL} else "",
            "claimed_at": _CLAIMED_AT.get(agent_id),
            "queued": list(_QUEUED.get(agent_id) or []),
            "account_recovery_parked": _CLAUDE_FAILOVER.parked(agent_id, value),
        }


def begin_stop(agent_id: str) -> tuple[dict, int, bool]:
    """Atomically install the Stop barrier in the process that owns turns."""
    with _TURN_LOCK:
        value = _INFLIGHT.get(agent_id)
        snapshot = {
            "trace_id": value if value not in {None, _STOPPING_SENTINEL} else "",
            "claimed_at": _CLAIMED_AT.get(agent_id),
            "queued": list(_QUEUED.get(agent_id) or []),
            "account_recovery_parked": _CLAUDE_FAILOVER.parked(agent_id, value),
        }
        queue_was_paused = bool(turn_queue.state(agent_id)["paused"])
        turn_queue.set_paused(agent_id, True)
        _INFLIGHT[agent_id] = _STOPPING_SENTINEL
        _CLAIMED_AT.pop(agent_id, None)
        dropped = len(_QUEUED.pop(agent_id, []) or [])
    return snapshot, dropped, queue_was_paused


def restore_stop_state(agent_id: str, snapshot: dict) -> None:
    """Roll back every process-local Stop mutation after interrupt failure."""
    with _TURN_LOCK:
        if _INFLIGHT.get(agent_id) != _STOPPING_SENTINEL:
            return
        trace_id = str(snapshot.get("trace_id") or "")
        if trace_id:
            _INFLIGHT[agent_id] = trace_id
        else:
            _INFLIGHT.pop(agent_id, None)
        claimed_at = snapshot.get("claimed_at")
        if claimed_at is None:
            _CLAIMED_AT.pop(agent_id, None)
        else:
            _CLAIMED_AT[agent_id] = float(claimed_at)
        # Sends admitted while the Stop barrier was active were appended after
        # the snapshot. Preserve them behind the restored pre-Stop queue.
        queued = list(snapshot.get("queued") or []) + list(
            _QUEUED.get(agent_id) or [])
        if queued:
            _QUEUED[agent_id] = queued
        else:
            _QUEUED.pop(agent_id, None)


def prepare_queued_for_finish(
    agent_id: str, snapshot: dict, cancelled_trace_ids: set[str]
) -> None:
    """Restore preserved/new specs except cancelled ones under the barrier."""
    with _TURN_LOCK:
        queue = list(snapshot.get("queued") or []) + list(
            _QUEUED.get(agent_id) or [])
        remaining = [
            spec for spec in queue
            if spec.trace_id not in cancelled_trace_ids
        ]
        if remaining:
            _QUEUED[agent_id] = remaining
        else:
            _QUEUED.pop(agent_id, None)


def complete_stop(
    ctx, agent_id: str, snapshot: dict, cancelled_trace_ids: set[str], *,
    backend_registry=backends,
) -> None:
    prepare_queued_for_finish(agent_id, snapshot, cancelled_trace_ids)
    finish_stop(ctx, agent_id, backend_registry=backend_registry)


def finish_stop(ctx, agent_id: str, *, backend_registry=backends) -> None:
    """Release the barrier and run a normal send admitted during Stop."""
    with _TURN_LOCK:
        if _INFLIGHT.get(agent_id) != _STOPPING_SENTINEL:
            return
        queue = _QUEUED.get(agent_id)
        next_spec = queue.pop(0) if queue else None
        if next_spec is None:
            _INFLIGHT.pop(agent_id, None)
            _QUEUED.pop(agent_id, None)
            return
        _INFLIGHT[agent_id] = next_spec.trace_id
        _CLAIMED_AT[agent_id] = time.monotonic()
    TurnDispatchService(ctx, backend_registry=backend_registry)._resume_and_spawn(
        agent_id, next_spec)


def drain_after_terminal(ctx, agent_id: str) -> None:
    """An agent's interactive terminal just closed. If normal turns queued
    behind it while it was live, take over the in-flight slot and spawn the next
    one now. No-op if nothing queued or another terminal is still attached."""
    with _TURN_LOCK:
        if _terminal_live(agent_id):
            return  # another terminal still attached — keep holding
        queue = _QUEUED.get(agent_id)
        next_spec = queue.pop(0) if queue else None
        if next_spec is None:
            if _INFLIGHT.get(agent_id) == _TERMINAL_SENTINEL:
                _INFLIGHT.pop(agent_id, None)
            _CLAIMED_AT.pop(agent_id, None)
            _QUEUED.pop(agent_id, None)
            return
        _INFLIGHT[agent_id] = next_spec.trace_id
        _CLAIMED_AT[agent_id] = time.monotonic()
    TurnDispatchService(ctx)._resume_and_spawn(agent_id, next_spec)
