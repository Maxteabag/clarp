"""Moved out of server/lib/turn_dispatch.py by tools/refactor/split_apply.py (split 'turn-dispatch-module', recipe tools/refactor/splits/s4-turn-dispatch.toml).
turn_dispatch.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import contextlib
import threading
import weakref
import time
from typing import Any
from . import backends, config
from . import launch_receipts, recovery_backoff, turn_slots
from .turn_slots import OwnershipLock
from .log import log, log_exception
from .account_failover import AccountFailover


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
# The slots live in lib.turn_slots.TurnSlots behind one OwnershipLock; the
# module names below are views of that one object:
#   _INFLIGHT: agent_id -> trace_id of the turn currently running
#   _QUEUED:   agent_id -> list of _TurnSpec waiting to run, in order
# Under _TURN_LOCK only ownership is decided. SQLite writes, eventlog rows and
# backend spawns that follow from a decision are deferred with
# ``_TURN_LOCK.defer`` and run after the outermost release.
_TURN_LOCK = OwnershipLock()
_SLOTS = turn_slots.TurnSlots(lock=_TURN_LOCK)
# Each account-failover coordinator has its own lock. Lock order is always
# _TURN_LOCK -> coordinator: the coordinator never takes _TURN_LOCK while it
# holds its own; the callbacks it runs (owned, pause, resume) read slots
# without the lock or defer their work past the coordinator's release.
# One coordinator per account pool, keyed by ``backend.account_pool()``.
_FAILOVERS: dict[str, AccountFailover] = {
    pool: AccountFailover(OwnershipLock())
    for pool in dict.fromkeys(backends.by_id(b).account_pool() for b in backends.ids())
    if pool
}

def _pool_backend(backend):
    """The backend whose pool runs ``backend``'s failover. Backends without
    a pool of their own book-keep in the default backend's, as they always
    have."""
    owner = backends.by_id(backend)
    return owner if owner.account_pool() else backends.by_id(backends.DEFAULT)

def account_failover(backend):
    return _FAILOVERS[_pool_backend(backend).account_pool()]

def account_selector(backend):
    return getattr(config.load(), _pool_backend(backend).config_account_switch_field)

_INFLIGHT: dict[str, str] = _SLOTS.inflight
_QUEUED: dict[str, list] = _SLOTS.queued
_CLAIMED_AT: dict[str, float] = _SLOTS.claimed_at
_RECOVERY_LOCK = threading.Lock()
_RECOVERY_BACKOFF = recovery_backoff.RecoveryBackoff()
# A fresh send from these asks for the agent again and clears a parked or
# backing-off queued recovery (lib.recovery_backoff); automation does not.
_RECOVERY_RESUMING_ORIGINS = frozenset({"user", "agent", "oracle"})
_NO_LOCK = contextlib.nullcontext()


# Weak values: a lock lives only while a caller holds it (`with` keeps a strong
# reference for the duration), so the map cannot grow with every Janitor ever
# seen. Concurrent callers still receive the same object while it is in use.
_JANITOR_SPAWN_LOCKS: "weakref.WeakValueDictionary[str, Any]" = weakref.WeakValueDictionary()

# Placeholder trace owning the in-flight slot while an interactive terminal is
# attached to an agent. A normal turn routed to that agent queues behind it
# (two processes resuming one session would corrupt the transcript); the queue
# drains via drain_after_terminal() when the terminal closes.
_TERMINAL_SENTINEL = turn_slots.TERMINAL_SENTINEL
_STOPPING_SENTINEL = turn_slots.STOPPING_SENTINEL
# Slot owner while a turn process the Host already finished is still running;
# sends wait behind it (see _claim_or_queue).
_PROCESS_OWNER_PREFIX = "process:"
_PROCESS_EXIT_POLL_S = 0.25
# A settled turn whose slot is still held this long after it settled leaked
# (see TurnDispatchService.release_leaked_slots).
LEAKED_SLOT_GRACE_MS = 60_000
# A slot still "spawning" this long after its claim, with no launch running
# and no account recovery holding it, was left by a launch that failed without
# giving it back (Pebble, 2026-10-10: "database is locked" in a queued
# handoff stranded the slot for 41 minutes).
ORPHAN_SPAWN_GRACE_S = 300.0
# An explicit repair trusts a younger claim: a launch that is really running
# is in _SLOTS.launches, and the claim-to-launch window is well under this.
REPAIR_SPAWN_GRACE_S = 30.0
_launching = _SLOTS.launching
_launch_running = _SLOTS.launch_running


def runtime_status() -> dict[str, Any]:
    """Serializable ownership snapshot served to replaceable HTTP processes."""
    from . import compaction
    status = _SLOTS.snapshot()
    from . import live_hub
    hub = live_hub.current()
    status.update({
        # The release drain (runtime_release): agents whose next turn waits on
        # the admission fence, and held work not yet durable.
        "held": _SLOTS.held(),
        "held_order": held_order(),
        "unpersisted": unpersisted_held(),
        "provider_turns": provider_turns(),
        "compactions": compaction.active_sessions(),
        # Sends being admitted: a reconciler must leave their sessions bound
        # (TP-05b); the release drain does not read it.
        "admitting": _SLOTS.admitting_agents(),
        # The status line per agent (docs/live-items.md §1.3), for the
        # HTTP process's /agents/snapshot.
        "live_activities": hub.activities() if hub is not None else {},
        **{f"{pool}_account_recovery": coordinator.status()
           for pool, coordinator in _FAILOVERS.items()},
    })
    return status


def held_order() -> dict[str, list[dict[str, str]]]:
    """While fenced, every agent's held work in FIFO order by stable ids:
    the durable row (queue or park id; "" until the drain persists it), the
    trace and the origin. Never message text."""
    if not _SLOTS.fenced:
        return {}
    return {agent_id: [{"row": spec.queue_id or spec.park_id,
                        "trace": spec.trace_id, "origin": spec.origin}
                       for spec in specs]
            for agent_id, specs in _SLOTS.queued_specs().items()}


def log_held_order(event: str) -> None:
    """Journal the held order at a seal or revert: the evidence that the work
    recovered or started afterwards kept it (``queuedRecoveredOrder``)."""
    for agent_id, entries in sorted(held_order().items()):
        rows = ",".join(entry["row"] or f"memory:{entry['trace']}"
                        for entry in entries)
        log("drainHeldOrder", f"event={event} agent={agent_id} "
                              f"n={len(entries)} rows={rows}")


RECOVERY_PARK_PREFIX = "recovery-park"


def _transfer_refusal(spec: "_TurnSpec", number: int, facts: dict) -> str:
    """Why parked account-recovery work cannot become durable held work, or
    "" when it can. Anything uncertain refuses; the work then keeps blocking
    the handoff exactly as before (Solu re7d614160304)."""
    if number != 1:
        return "retried"
    if spec.recovery_text or spec.recovery_attempted:
        return "continuation"
    if spec.janitor_run_id:
        return "maintenance"
    if spec.unheard_audio:
        return "unheard_audio"
    if spec.is_new_session or not spec.backend_session_id:
        return "new_session"
    if facts.get("binding") != spec.backend_session_id:
        return "binding_changed"
    if facts.get("paused"):
        return "queue_paused"
    if spec.queue_id and not facts.get("queued_row"):
        return "queued_row_uncertain"
    if facts.get("excluded"):
        return str(facts["excluded"])
    if _SLOTS.get(spec.agent_id) != spec.trace_id:
        return "not_slot_owner"
    if _launch_running(spec.agent_id):
        return "launching"
    return ""


def account_recovery_carry(wall: float | None = None,
                           handed_over: dict[str, set[str]] | None = None
                           ) -> dict[str, dict]:
    """Per account pool in recovery, when its work was first parked and
    whose parked work the drain handed over."""
    wall = time.time() if wall is None else wall
    carry = {}
    for pool, coordinator in _FAILOVERS.items():
        value = coordinator.carry_over(wall, (handed_over or {}).get(pool, ()))
        if value is not None:
            carry[pool] = value
    return carry


def resume_account_recovery(carry: dict, wall: float | None = None) -> list[str]:
    """At boot after a handoff, continue the pools that were recovering with
    their original start (the park bound does not restart). Returns pools."""
    wall = time.time() if wall is None else wall
    resumed = []
    for pool, value in (carry or {}).items():
        coordinator = _FAILOVERS.get(pool)
        if coordinator is None or not isinstance(value, dict):
            continue
        backend = next((b for b in backends.ids()
                        if backends.by_id(b).account_pool() == pool), "")
        command = account_selector(backend) if backend else None
        try:
            since = int(value.get("parked_since_ms") or 0)
        except (TypeError, ValueError):
            continue
        agents = value.get("agents") or ()
        if not isinstance(agents, (list, tuple)):
            agents = ()
        if since and coordinator.resume_recovery(command, since, wall, agents):
            resumed.append(pool)
            log("accountRecoveryCarried",
                f"pool={pool} parked_for={max(0.0, wall - since / 1000):.0f}s")
    return resumed


def unpersisted_held() -> list[str]:
    """Agents with queued work that would not survive a runtime handoff:
    no durable queue row and no park row."""
    return sorted(agent_id for agent_id, specs in _SLOTS.queued_specs().items()
                  if any(not spec.queue_id and not spec.park_id for spec in specs))


def provider_turns() -> list[str]:
    """Agents with a turn a shared provider runs without a Clarp slot (a Codex
    app-server goal continuation). A handoff would end it with the process."""
    try:
        from . import codex_app_server
        return sorted(codex_app_server.busy_agents())
    except Exception as exc:  # noqa: BLE001 - unknown is reported, not idle
        log_exception("providerTurnsCheckFail", exc)
        return ["<unknown>"]


UPDATE_HOLD_HEADLINE = "Waiting for the Clarp update"


def refresh_hold_headlines() -> None:
    """Say why an agent with work does not start: its next turn waits for the
    update (live state ``limited``). Cleared when the fence is lifted, at the
    handoff seal, or when the agent's next turn begins."""
    from . import live_hub
    hub = live_hub.current()
    if hub is not None:
        hub.set_update_holds(set(_SLOTS.held()), UPDATE_HOLD_HEADLINE)


# The admission in progress on this thread (TurnDispatchService.submit), so
# the agent can be marked as admitting once it is known (TP-05b).
_ADMISSION = threading.local()


def live_work(agent_id: str, *, session: str = "") -> turn_slots.LiveWork:
    """Every busy fact for one agent: slot, queue, terminal, compaction."""
    return _SLOTS.live_work(agent_id, session=session)


def reset_for_tests() -> None:
    _SLOTS.reset_for_tests()
    _RECOVERY_BACKOFF.reset_for_tests()
    launch_receipts.reset_for_tests()


def _terminal_live(agent_id: str) -> bool:
    return _SLOTS.live_work(agent_id).terminal


def _alive(handles) -> list:
    """The handles that positively report a running process."""
    running = []
    for handle in handles:
        try:
            if getattr(handle, "is_alive", lambda: False)():
                running.append(handle)
        except Exception:  # noqa: BLE001
            continue
    return running
