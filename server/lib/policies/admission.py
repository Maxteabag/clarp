"""Turn admission: which origin may wake an agent, and how.

This is the single home for the per-origin rules that ``turn_dispatch.dispatch``
and ``_spawn_attempt_claimed`` used to spell inline. The dispatcher gathers the
facts (the agent row, the team list, host settings, the request, the queue
state) and acts on the decision; nothing here reads a database or a clock.

Decisions:

* ``Allow(effective)``   start or queue the turn as the dispatcher sees fit.
* ``Queue(reason, effective)``  the durable queue is paused and this request
  may not bypass it: record the receipt, then return ``queued``.
* ``Reject(status, reason, janitor=...)``  refuse. ``janitor`` tells the
  caller to raise ``JanitorDispatchError`` rather than ``DispatchError``.

``effective`` carries the request after the origin rewrite a maintenance run
imposes (``janitor`` origin, muted audio, queued delivery) plus the per-origin
flags that the rest of dispatch consults: whether the herald is pinged, whether
the paused queue is bypassed, whether a live turn may be steered, and whether a
peer message is protected.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .. import origins

JANITOR_DEMAND_PREFIX = "janitor-demand-"

# The reason strings are the HTTP messages the dispatcher raised before this
# module existed. Keep them byte-identical: clients and tests match on them.
HEARTBEAT_AUTHORITY_CHANGED = "Heartbeat decision authority changed"
JANITOR_RUNS_ONLY = "Janitors accept configured maintenance runs only"
JANITOR_RUN_INACTIVE = "Maintenance run is not active for this configuration"
JANITOR_RUN_NOT_JANITOR = "Maintenance run does not target a Janitor"
JANITOR_REQUEST_ID_MISMATCH = "Maintenance request ID must match its run"
JANITOR_QUEUE_ID_MISMATCH = "Maintenance queue ID must match its run"
JANITOR_ORIGIN_NEEDS_RUN = "Janitor origin requires an admitted run"
HEARTBEATS_DISABLED = "Heartbeats are disabled on this Host"
LEADER_NUDGE_DISABLED = "team leader nudging is disabled"

# Origins whose fresh intent lifts a Stop-paused queue for this one request.
PAUSED_QUEUE_BYPASS_ORIGINS = frozenset({"user", "oracle"})


@dataclass(frozen=True)
class HostSettings:
    """Host-wide switches the admission consults."""
    heartbeats_disabled: bool = False


@dataclass(frozen=True)
class LiveWork:
    """The request being admitted, as the dispatcher received it.

    ``janitor_demand_valid`` and ``janitor_run_active`` are the two lookups the
    caller performs only when ``needs_janitor_demand_check`` / a maintenance
    run apply; ``None`` means "not looked up" and is treated as valid so the
    policy never invents a refusal the caller did not verify.
    """
    client_msg_id: str = ""
    durable_queue_id: str = ""
    janitor_run_id: str = ""
    sender_agent_id: str = ""
    queue_if_busy: bool = False
    skip_admission: bool = False
    allow_paused_queue: bool = False
    janitor_demand_valid: bool | None = None
    janitor_run_active: bool | None = None


@dataclass(frozen=True)
class QueueState:
    paused: bool = False


@dataclass(frozen=True)
class Effective:
    """The request after admission rewrote it, plus the per-origin flags."""
    origin: str
    client_msg_id: str
    queue_if_busy: bool
    # A maintenance run never speaks and never counts as unheard audio.
    mute_audio: bool
    notify_herald: bool
    paused_bypass: bool
    steer_allowed: bool
    protected_peer: bool


@dataclass(frozen=True)
class Allow:
    effective: Effective


@dataclass(frozen=True)
class Queue:
    reason: str
    effective: Effective


@dataclass(frozen=True)
class Reject:
    status: int
    reason: str
    janitor: bool = False


Decision = Allow | Queue | Reject


@dataclass(frozen=True)
class OriginTraits:
    """Per-origin hooks the dispatcher runs after admission.

    These are the comparisons that survive in ``_steer_if_supported``,
    ``_spawn_attempt_claimed`` and the result handlers; keeping them in one
    table means a new origin is described once.
    """
    tracks_oracle_delegation: bool = False   # oracle: attach/mark/finalize
    records_dreaming_result: bool = False    # dreaming: _record_dreaming_result
    records_heartbeat_noop_on_failure: bool = False  # heartbeat: NOTIFY failures


_TRAITS: dict[str, OriginTraits] = {
    "oracle": OriginTraits(tracks_oracle_delegation=True),
    "dreaming": OriginTraits(records_dreaming_result=True),
    "heartbeat": OriginTraits(records_heartbeat_noop_on_failure=True),
}


def traits(origin: str | None) -> OriginTraits:
    return _TRAITS.get((origin or "").strip(), OriginTraits())


def needs_janitor_demand_check(origin: str, client_msg_id: str) -> bool:
    """A heartbeat carrying a janitor-demand id must be re-authorised."""
    return origin == "heartbeat" and (client_msg_id or "").startswith(JANITOR_DEMAND_PREFIX)


def leader_nudge_allowed(agent_id: str, teams: Sequence[Mapping]) -> bool:
    """The gate ``dispatch`` and ``_spawn_attempt_claimed`` both apply."""
    return any(
        team.get("leader_enabled") and team.get("nudge_enabled")
        and team.get("leader_agent_id") == agent_id
        for team in teams
    )


def can_chat(agent: Mapping) -> bool:
    """Mirror of ``agents.interaction_capabilities(agent)["can_chat"]``."""
    return not bool(agent.get("is_janitor"))


def before_routing(origin: str, client_msg_id: str,
                   janitor_demand_valid: bool | None) -> Reject | None:
    """The one rule that needs no agent: a forged or stale Janitor demand is
    refused before the dispatcher routes the text to anyone."""
    if (needs_janitor_demand_check((origin or "").strip(), client_msg_id or "")
            and janitor_demand_valid is False):
        return Reject(409, HEARTBEAT_AUTHORITY_CHANGED)
    return None


def admission(origin: str, agent: Mapping, teams: Sequence[Mapping],
              settings: HostSettings, live_work: LiveWork,
              queue: QueueState) -> Decision:
    """Decide whether ``origin`` may wake ``agent`` with ``live_work``.

    The checks run in the order the dispatcher applied them, so the first
    matching refusal is the one the client sees.
    """
    origin = (origin or "").strip()
    agent_id = str(agent.get("agent_id") or "")
    client_msg_id = live_work.client_msg_id or ""
    queue_if_busy = live_work.queue_if_busy
    mute_audio = False

    early = before_routing(origin, client_msg_id, live_work.janitor_demand_valid)
    if early is not None:
        return early

    run_id = live_work.janitor_run_id or ""
    if not can_chat(agent):
        if not run_id:
            return Reject(409, JANITOR_RUNS_ONLY, janitor=True)
        if live_work.janitor_run_active is False:
            return Reject(409, JANITOR_RUN_INACTIVE, janitor=True)
    elif run_id:
        return Reject(409, JANITOR_RUN_NOT_JANITOR, janitor=True)

    if run_id:
        if client_msg_id and client_msg_id != run_id:
            return Reject(409, JANITOR_REQUEST_ID_MISMATCH, janitor=True)
        if live_work.durable_queue_id and live_work.durable_queue_id != run_id:
            return Reject(409, JANITOR_QUEUE_ID_MISMATCH, janitor=True)
        client_msg_id = run_id
        origin = "janitor"
        queue_if_busy = True
        mute_audio = True
    elif origin == "janitor":
        return Reject(409, JANITOR_ORIGIN_NEEDS_RUN, janitor=True)

    if origin == "heartbeat" and settings.heartbeats_disabled:
        return Reject(409, HEARTBEATS_DISABLED)

    notify_herald = origin not in origins.ROUTINE_AUTOMATION_ORIGINS

    if origin == "leader_tick" and not leader_nudge_allowed(agent_id, teams):
        return Reject(409, LEADER_NUDGE_DISABLED)

    paused_bypass = False
    parked = False
    if queue_if_busy and queue.paused and not live_work.allow_paused_queue:
        if origin in PAUSED_QUEUE_BYPASS_ORIGINS and not live_work.skip_admission:
            paused_bypass = True
        else:
            parked = True

    effective = Effective(
        origin=origin,
        client_msg_id=client_msg_id,
        queue_if_busy=queue_if_busy,
        mute_audio=mute_audio,
        notify_herald=notify_herald,
        paused_bypass=paused_bypass,
        steer_allowed=not queue_if_busy or origin == "oracle",
        protected_peer=origin == "agent" and bool(live_work.sender_agent_id),
    )
    if parked:
        return Queue("paused-queue", effective)
    return Allow(effective)
