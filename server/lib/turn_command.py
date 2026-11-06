"""Moved out of server/lib/turn_dispatch.py by tools/refactor/split_apply.py (split 'turn-dispatch-module', recipe tools/refactor/splits/s4-turn-dispatch.toml).
turn_dispatch.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import dataclasses
import pathlib
import json
import threading
import functools
import weakref
from dataclasses import dataclass, replace
from typing import Any, Callable
from . import agents as agents_db
from . import backends, config, db
from . import team_store, turn_queue
from . import launch_receipts
from .policies import admission as admission_policy
from .log import log, log_exception
from .runtime_contract import PromptAdmission, DispatchResult, DispatchError, DispatchCommand
from .turn_dispatch_state import _JANITOR_SPAWN_LOCKS, _TURN_LOCK


def _with_delivery(command: "DispatchCommand", result: DispatchResult) -> DispatchResult:
    """F4 S1, computed where turns are owned, after the decision: never from an
    exception and never authority. A bookkeeping failure leaves it unknown."""
    if not (command.client_msg_id and launch_receipts.eligible(
            origin=command.origin, client_msg_id=command.client_msg_id, call_opening=False)):
        return result
    try:
        agent = agents_db.get_by_session(result.session) if result.session else None
        if not agent:
            return result
        shown = launch_receipts.delivery_for_send(
            command.client_msg_id, agent_id=agent["agent_id"], origin=command.origin,
            sender_agent_id=command.sender_agent_id or "")
    except Exception as e:  # noqa: BLE001 - delivery state is a projection, never a failure
        log_exception("deliveryStateReadFail", e, detail=result.session)
        return result
    return replace(result, delivery=shown) if shown is not None else result


def _resolve_llm(agent: dict, backend: str) -> tuple[str, str]:
    """Effective (model, effort) for a turn: the per-agent override wins, else
    the global [agents] config default, else the CLI's own default ("").
    Effort is validated against what the backend's CLI accepts."""
    cfg = config.load()
    if agent.get("is_janitor"):
        from .janitor_design_policy import effective_chain
        chain = effective_chain(agent["session"])
        if chain["source"] == "global" and chain["chain"]:
            primary = chain["chain"][0]
            if primary["provider"] == backend:
                return primary["model"], agent.get("effort") or ""
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
    recovery_attempted: bool = False
    # Private runtime capability; never sourced from an ordinary send payload.
    janitor_run_id: str = ""
    # queued_turns row (status 'parked') holding a send admitted behind the
    # Stop barrier, so it survives a runtime restart; "" when not parked.
    park_id: str = ""
    # Held behind a Stop barrier as newer work (``_claim_or_queue``): Stop has
    # already decided it runs, so its launch receipt may not refuse it.
    after_stop: bool = False
    # Launches of this spec that failed before the backend started; spaces
    # out the retries of a queued handoff (see _requeue_failed_handoff).
    handoff_failures: int = 0
    # Whether a Stop pause could not hold this request when it arrived (a
    # normal send, fresh user intent, an explicit send-now). The release drain
    # writes it to the durable row so the next runtime honours it too.
    bypass_pause: bool = False
    # The release drain already made this held spec durable.
    drain_persisted: bool = False
    # The release drain's hand-over parked this spec's own queue row
    # (``queue_id``) in place: a cancel ends that exact row, and lifting
    # the fence queues it again.
    taken_over: bool = False
    # The agent's state generation read just before this turn set its live trace (call-open end:
    # hook prompt admissions after it refuse the end's visible write). In memory only.
    s_open: int | None = None


def _pause_exempt_on_arrival(command: "DispatchCommand", effective) -> bool:
    """Whether a Stop pause could not hold this request when it arrived, so
    the release drain may persist that right (``queued_turns.allow_paused``).
    The same reading admission makes: a normal send and a peer message are
    never paused; fresh user intent bypasses; an explicit send-now is allowed.
    A goal wake is left out: recovery re-checks its right each time."""
    from . import task_goal_recovery
    if str(effective.client_msg_id or "").startswith(task_goal_recovery.PREFIX):
        return False
    peer_waits = effective.protected_peer and not command.queue_if_busy
    return bool(not effective.queue_if_busy or peer_waits
                or command.allow_paused_queue or effective.paused_bypass)


def _stop_holds(spec: _TurnSpec) -> bool:
    """Whether the agent's Stop pause holds this queued spec from starting.

    The in-memory handover (a turn's end, the end of a Stop, a retried or
    ownerless head, a lifted drain fence, a closed terminal) applies the same
    rule as admission and durable recovery: work the pause could not hold on
    arrival (``bypass_pause``), a row the release drain kept that right on
    (``allow_paused``) and an allowed goal wake run; everything else waits for
    Resume. A Stop takes the arrival right back from work already queued
    (``prepare_queued_for_finish``), as ``turn_queue.set_paused`` does for rows.
    """
    if spec.bypass_pause or not turn_queue.is_paused(spec.agent_id):
        return False
    from . import task_goal_recovery
    if (spec.client_msg_id.startswith(task_goal_recovery.PREFIX)
            and task_goal_recovery.allows_paused_queue(spec.client_msg_id)):
        return False
    row = turn_queue.get(spec.queue_id) if spec.queue_id else None
    return not (row and row.get("allow_paused"))


def _park(spec: _TurnSpec, *, prefix: str = "stop-park") -> str:
    """Persist a send that is about to wait behind the Stop barrier (or, with
    ``prefix="drain-park"``, behind the release drain's admission fence)."""
    park_id = f"{prefix}-{spec.trace_id}"
    try:
        turn_queue.park(
            queue_id=park_id, agent_id=spec.agent_id, session=spec.session,
            text=spec.text, trace_id=spec.trace_id,
            client_msg_id=spec.client_msg_id,
            synthesize_audio=spec.synthesize_audio, origin=spec.origin,
            sender_agent_id=spec.sender_agent_id,
            prompt_admission_id=spec.prompt_admission_id,
            allow_paused=(prefix in {"drain-park", "preempt-park"}
                          and spec.bypass_pause))
    except Exception as exc:  # noqa: BLE001 - memory still holds the send
        log_exception(f"{prefix.split('-')[0]}ParkPersistFail",
                      exc, detail=spec.agent_id)
        return ""
    return park_id


def _park_at_head(spec: _TurnSpec) -> str:
    """Persist a preempting send ahead of everything its agent has open, as
    it runs before the work already queued (TP-01)."""
    park_id = f"preempt-park-{spec.trace_id}"
    con = db.conn()
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            turn_queue.park_at_head(
                queue_id=park_id, agent_id=spec.agent_id, session=spec.session,
                text=spec.text, trace_id=spec.trace_id,
                client_msg_id=spec.client_msg_id,
                synthesize_audio=spec.synthesize_audio, origin=spec.origin,
                sender_agent_id=spec.sender_agent_id,
                prompt_admission_id=spec.prompt_admission_id)
            if spec.bypass_pause:
                turn_queue.allow_paused_for(park_id)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    except Exception as exc:  # noqa: BLE001 - the caller refuses the send
        log_exception("preemptParkPersistFail", exc, detail=spec.agent_id)
        return ""
    return park_id


def _refuse_oversized(registry, backend: str, text: str) -> None:
    """Before admission, so nothing is stored: a message longer than the
    backend's CLI can take is refused with 413 (BACKENDS-08)."""
    limit_of = getattr(registry, "prompt_limit_bytes", None)
    limit = limit_of(backend) if limit_of is not None else None
    size = len((text or "").encode())
    if limit is not None and size > limit:
        raise DispatchError(
            413, f"This message is {size // 1024} KiB, more than the {limit // 1024} KiB "
                 "this agent can take in one message. Shorten it or split it into "
                 "several messages.")


class JanitorDispatchError(DispatchError):
    """A maintenance run lost authorization; retrying cannot restore it."""


class _LaunchReceiptUnrecorded(DispatchError):
    """The launch receipt could not leave ``pending``, so the backend was never
    handed the prompt: a same-id retry may still launch it exactly once."""


UNCONFIRMED_DELIVERY = "message is saved but delivery is unconfirmed; reconciliation required"


class _LaunchReceiptRefused(DispatchError):
    """The receipt was fenced by Stop or superseded by a relaunch: this prompt
    is never handed to a backend. Clients get the existing unconfirmed 503:
    a superseded send may still run under its newer attempt, and a stopped one
    is not something this response can name until the delivery contract does.
    ``reason`` stays internal, for diagnostics."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(503, UNCONFIRMED_DELIVERY)


DELIVERY_HINT_BUSY_MS = 50   # N4 B: a hint never waits longer for the writer lock


def _record_launch_attempt(spec: "_TurnSpec") -> None:
    """Fail closed immediately before a backend receives a user prompt."""
    if not launch_receipts.eligible(origin=spec.origin, client_msg_id=spec.client_msg_id,
                                    call_opening=_is_call_opening(spec)):
        return
    # A spec launched from a durable queue or park row, or held behind Stop as
    # newer work, is governed by that decision, not by a racing fence.
    decided = bool(spec.queue_id or spec.park_id or spec.after_stop)
    try:
        launch_receipts.mark_attempted(spec.trace_id, override_fence=decided)
    except launch_receipts.HandoffRefused as e:
        log("launchReceiptRefused",
            f"agent={spec.agent_id} trace={spec.trace_id or '∅'} state={e.state}")
        raise _LaunchReceiptRefused(e.state) from e
    except Exception as e:  # noqa: BLE001
        log_exception("launchReceiptAttemptFail", e, detail=spec.session)
        raise _LaunchReceiptUnrecorded(
            503, "could not record the launch; the message was not sent") from e


class GoalWakeDispatchError(DispatchError):
    def __init__(self, rejection):
        self.permanent = rejection.permanent
        self.code = rejection.code
        super().__init__(409, str(rejection))


def _validate_goal_dispatch(agent, request_id, *, native_session_id=None):
    from . import task_goal_recovery

    try:
        task_goal_recovery.validate_dispatch(
            agent, request_id, native_session_id=native_session_id)
    except task_goal_recovery.WakeRejected as exc:
        raise GoalWakeDispatchError(exc) from exc


def _janitor_run_active(agent: dict, run_id: str, trace_id: str) -> bool | None:
    """The one lookup the Janitor rules need; None when they do not apply."""
    if not run_id or admission_policy.can_chat(agent):
        return None
    from . import janitors
    return bool(janitors.validate_dispatch(agent["session"], run_id, trace_id))


def _admission_facts(origin: str) -> tuple["admission_policy.HostSettings", list]:
    """The host facts admission needs, read only for the origins that use them."""
    origin = (origin or "").strip()
    settings = admission_policy.HostSettings()
    if origin == "heartbeat":
        from . import heartbeat
        settings = admission_policy.HostSettings(
            heartbeats_disabled=bool(heartbeat.globally_disabled()))
    teams = team_store.list_teams() if origin == "leader_tick" else []
    return settings, teams


def _admit(origin: str, agent: dict, request: "admission_policy.LiveWork", *,
           facts: tuple, queue_paused: bool,
           ) -> "admission_policy.Allow | admission_policy.Queue":
    """Ask the admission policy; raise the matching error on a refusal."""
    settings, teams = facts
    decision = admission_policy.admission(
        origin, agent, teams, settings, request,
        admission_policy.QueueState(paused=queue_paused))
    if isinstance(decision, admission_policy.Reject):
        error = JanitorDispatchError if decision.janitor else DispatchError
        raise error(decision.status, decision.reason)
    return decision


def _validate_janitor_target(agent: dict, run_id: str, trace_id: str) -> None:
    """The Janitor half of admission, re-checked before every later attempt
    (queue drains, retries, recovery) of a turn admitted earlier."""
    request = admission_policy.LiveWork(
        client_msg_id=run_id, janitor_run_id=run_id,
        janitor_run_active=_janitor_run_active(agent, run_id, trace_id))
    decision = admission_policy.admission(
        "janitor" if run_id else "", agent, (), admission_policy.HostSettings(),
        request, admission_policy.QueueState())
    if isinstance(decision, admission_policy.Reject):
        raise JanitorDispatchError(decision.status, decision.reason)


def _recovered_janitor_run(row: dict) -> str:
    """Recover capability from both durable records, never from an origin."""
    agent = agents_db.get_by_agent_id(str(row["agent_id"]))
    if not agent or agents_db.interaction_capabilities(agent)["can_chat"]:
        return ""
    from . import janitors
    trace_id = str(row["trace_id"])
    run = janitors.get_run_for_trace(trace_id)
    if (not run or run["run_id"] != trace_id
            or str(row["queue_id"]) != run["run_id"]
            or str(row["client_msg_id"]) != run["run_id"]
            or str(row["session"]) != run["session"]
            or str(row["agent_id"]) != run["agent_id"]):
        raise JanitorDispatchError(409, "Queued message has no matching maintenance run")
    _validate_janitor_target(agent, run["run_id"], trace_id)
    return run["run_id"]


def _janitor_spawn_lock(agent_id: str):
    # Separate from the global turn lock: Codex's blocking start RPC needs its
    # read-thread callbacks to take _TURN_LOCK before the response arrives.
    with _TURN_LOCK:
        lock = _JANITOR_SPAWN_LOCKS.get(agent_id)
        if lock is None:
            lock = threading.RLock()
            _JANITOR_SPAWN_LOCKS[agent_id] = lock
        return lock


# Background ticks their schedulers regenerate: never readmitted by recovery.
_REGENERATED_ORIGINS = frozenset({"heartbeat", "leader_tick"})

# Background retries of an unlaunched spawn's marker: waits before each.
_UNLAUNCHED_MARKER_RETRY_SEC = (1.0, 5.0)


def _unlaunched_marked(agent_id: str, trace_id: str) -> bool:
    return db.conn().execute(
        """SELECT 1 FROM state_log WHERE agent_id = ?
              AND json_extract(detail, '$.trace_id') = ?
              AND json_extract(detail, '$.dispatch_not_started')
            LIMIT 1""", (agent_id, trace_id)).fetchone() is not None


def _remove_queued_run(queue_id: str) -> None:
    # Use the queue API to preserve revisions and unmaterialized admissions.
    turn_queue.release_claim(queue_id)
    turn_queue.remove(queue_id)


def _default_retry_scheduler(delay: float, fn: Callable[[], None]) -> None:
    """Run `fn` after `delay` seconds on a daemon timer thread."""
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()


OPENING_NOT_CONFIRMED = "The call opening could not be confirmed and will not be sent again"
OPENING_WITHDRAWN = "The call opening was withdrawn before it was sent"


class _AdmissionRefused:
    """What a spawn attempt reports when the final call-open check refused the launch before the
    spawn flag (L2 r4). The refusal has already settled the turn: "cancelled" wrote the withdrawn
    receipt and finished it; "unknown" ended it visibly interrupted. A caller must neither mark
    it spawned nor read it as superseded (409, D2 release, unlaunched receipt).

    It is neither True nor False. A caller compares it by identity before any truth test, and
    bool() raises, so a caller that was never taught about it fails loudly instead of reading a
    refused opening as launched or superseded."""
    __slots__ = ()

    def __bool__(self) -> bool:
        raise TypeError("compare _ADMISSION_REFUSED by identity, not truth")

    def __repr__(self) -> str:
        return "_ADMISSION_REFUSED"


_ADMISSION_REFUSED = _AdmissionRefused()


def _is_call_opening(spec) -> bool:
    """A listen-first initiative (agent_rings): its own turn, never a user row.
    Identified by its Host-issued admission, not the client-suppliable id."""
    queue_id = str(getattr(spec, "queue_id", "") or getattr(spec, "client_msg_id", "") or "")
    if not queue_id.startswith("call-open:"):
        return False
    from . import agent_rings
    # Only positive evidence (a non-call prompt admission) makes it someone's
    # own message; otherwise the opening's gate decides.
    return not agent_rings.is_foreign_message(str(getattr(spec, "prompt_admission_id", "") or ""))


def _mark_backend_invoked(exc: BaseException) -> None:
    """The launch had already handed the turn to the backend when ``exc``
    escaped: no longer evidence that nothing was sent (agent_rings, D2)."""
    try:
        exc.clarp_backend_invoked = True
    except Exception:  # noqa: BLE001 - an exception that takes no attributes
        pass


def _backend_invoked(exc: BaseException | None) -> bool:
    while exc is not None:
        if getattr(exc, "clarp_backend_invoked", False):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


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


_REQUEST_LOCKS = weakref.WeakValueDictionary()
_REQUEST_LOCKS_GUARD = threading.Lock()


def _serialize_client_retry(method):
    """Keep one client's concurrent retries together through admission/launch.

    SQLite admission alone ends before the runtime claims the turn. A second
    request must not reclaim that first request in the intervening gap.
    Weak entries disappear when the last waiting request finishes.
    """
    # Applied to the owning half only; the forwarding half never holds this
    # lock across the RPC (an embedded runtime can execute the same request
    # on another thread).
    @functools.wraps(method)
    def dispatch(self, command):
        key = command.client_msg_id or command.trace_id
        if not key:
            return method(self, command)
        with _REQUEST_LOCKS_GUARD:
            lock = _REQUEST_LOCKS.get(key)
            if lock is None:
                lock = threading.RLock()
                _REQUEST_LOCKS[key] = lock
        with lock:
            return method(self, command)
    return dispatch
