"""Retire work labels nobody is doing any more.

The apps draw an agent as still working while it has a running job, a
running helper or a status line of its own. ``policies.stale_work`` decides
when one of those has outlived the work behind it; this module gathers the
facts and acts through the owning stores:

* jobs: ``background_jobs.reconcile_stale`` applies the heartbeat rule on
  every watcher pass;
* helpers: a running helper whose own session has been idle too long moves
  to ``reported`` through ``helper_agents.note_idle``;
* statuses: an idle agent's own status line older than its TTL is cleared
  through ``agents.clear_stale_custom_status``, and a declared ``background``
  state it left behind settles to ``idle``.

Every change is logged. A helper's change is also noted on the timeline of
the job that mirrors it, when there is one, so it shows in
``GET /background-jobs/<id>``.
"""
from __future__ import annotations

from typing import Any

from . import agents as agents_db
from . import background_jobs, backends, config, db, events, helper_agents, turn_lifecycle, turn_queue
from .log import log, log_exception
from .policies import stale_work as policy
from .protocol import AgentBackend, AgentState
from .turn_lifecycle import TurnEvent

HELPER_IDLE_NOTE = "reconcile: helper {session} idle {minutes} min; moved to reported"


def thresholds() -> policy.Thresholds:
    return policy.from_config(config.load())


def _last_job_change() -> dict[str, int]:
    """Newest job change per agent: finishing a job is activity too."""
    return {str(row["agent_id"]): int(row["at"] or 0) for row in db.conn().execute(
        "SELECT agent_id, MAX(updated_at) AS at FROM background_jobs "
        "WHERE COALESCE(agent_id, '') != '' GROUP BY agent_id")}


def activities(agent_rows: list[dict[str, Any]] | None = None) -> dict[str, policy.Activity]:
    """Each live agent's activity, counted the way the dashboard counts it."""
    rows = agents_db.list_agents() if agent_rows is None else agent_rows
    states = agents_db.dashboard_states()
    jobs = background_jobs.active_by_agent()
    queues = turn_queue.states()
    job_changes = _last_job_change()
    running_children: dict[str, int] = {}
    helper_sessions: dict[str, set[str]] = {}
    for a in rows:
        parent = a.get("parent_agent_id")
        if not parent:
            continue
        if (a.get("role") or "agent") == "helper":
            helper_sessions.setdefault(parent, set()).add(str(a.get("session") or ""))
        if a.get("helper_state") == "running":
            running_children[parent] = running_children.get(parent, 0) + 1
    out: dict[str, policy.Activity] = {}
    for a in rows:
        agent_id = a["agent_id"]
        state = states.get(agent_id, {})
        backend = a.get("backend") or AgentBackend.CLAUDE
        busy = (state.get("kind") in turn_lifecycle.BUSY
                or bool(backends.active_handles(backend, agent_id))
                or bool(queues.get(agent_id, {}).get("count")))
        processes = background_jobs.background_processes(
            jobs.get(agent_id, []), helper_sessions.get(agent_id, set()))
        out[agent_id] = policy.Activity(
            busy=busy, active_jobs=len(processes),
            running_children=running_children.get(agent_id, 0),
            idle_since_ms=max(int(state.get("ts") or 0),
                              int(state.get("last_turn_end") or 0),
                              job_changes.get(agent_id, 0),
                              int(a.get("created_at") or 0)))
    return out


def _janitor_owned() -> set[str]:
    """Agents whose status is the label a Janitor wrote (it expires on its own)."""
    from . import janitor_store
    return {str(r["target_agent_id"]) for r in janitor_store.label_ownership_with_status()
            if r["label"] == r["custom_status"]}


def _note_on_mirror_job(parent_id: str, helper_session: str, note: str) -> None:
    """Record a helper's change on the job that mirrors it, if one is active."""
    for job in background_jobs.active_by_agent().get(parent_id, []):
        if background_jobs.mirrored_helper_session(job, {helper_session}):
            background_jobs.add_note(job["job_id"], note)


def sweep(stream=None, *, now_ms: int | None = None,
          limits: policy.Thresholds | None = None) -> dict[str, int]:
    """Apply the helper and status rules once. Returns what changed."""
    now = db.now_ms() if now_ms is None else int(now_ms)
    limits = limits or thresholds()
    rows = agents_db.list_agents()
    acts = activities(rows)
    counts = {"helpers_reported": 0, "statuses_cleared": 0}
    owned: set[str] | None = None
    for a in rows:
        agent_id = a["agent_id"]
        activity = acts.get(agent_id)
        if activity is None or a.get("is_janitor"):
            continue
        if policy.helper_idle_due(helper_state=a.get("helper_state"), activity=activity,
                                  now_ms=now, thresholds=limits):
            idle_ms = now - activity.idle_since_ms
            if helper_agents.note_idle(stream, agent_id, idle_ms=idle_ms):
                counts["helpers_reported"] += 1
                if a.get("parent_agent_id"):
                    try:
                        _note_on_mirror_job(a["parent_agent_id"], str(a["session"]),
                                            HELPER_IDLE_NOTE.format(
                                                session=a["session"], minutes=idle_ms // 60000))
                    except Exception as exc:  # noqa: BLE001 - the move itself stands
                        log_exception("staleWorkMirrorNoteFail", exc, detail=agent_id)
        status = str(a.get("custom_status") or "")
        if not status.strip():
            continue
        if owned is None:
            owned = _janitor_owned()
        if a.get("custom_status_at") is None:
            continue  # no recorded time: its age is not guessed at
        status_at = int(a["custom_status_at"])
        if not policy.custom_status_due(
                status=status, status_at=status_at, janitor_owned=agent_id in owned,
                activity=activity, now_ms=now, thresholds=limits):
            continue
        if not agents_db.clear_stale_custom_status(agent_id, status=status,
                                                   status_at=status_at):
            continue
        counts["statuses_cleared"] += 1
        age_min = (now - status_at) // 60000
        log("staleWorkStatusCleared", f"{a['session']}: {status!r} ({age_min} min old)")
        # The status came with a declared background state; leave it idle too,
        # as `clarp-agent-bg off` would.
        if (agents_db.latest_state(agent_id) or {}).get("kind") == AgentState.BACKGROUND:
            turn_lifecycle.try_transition(agent_id, TurnEvent.BACKGROUND_EXPIRED, {
                "source": "stale_work", "cleared_status": status[:200],
                "status_age_min": age_min})
        if stream is not None:
            events.broadcast(stream, events.agent_roster("stale-work", session=a["session"]))
    if any(counts.values()):
        log("staleWorkSwept", str(counts))
    return counts
