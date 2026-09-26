"""Agent dashboard read model."""
from __future__ import annotations

from typing import Any

import json

from . import (background_jobs, agent_goals, agents as agents_db, avatar_settings, backend_usage, backends,
               compaction, db,
               config, message_store, model_avatars, team_store,
               turn_queue, scheduler, janitors)
from . import reconcile
from .clock import now_ms
from .session_models import agent_model
from . import personas as persona_store
from .avatar_urls import (versioned_avatar_url, janitor_avatar_url,
                          static_persona_avatar_url)
from .log import log_exception
from .activity import state_activity_event
from .transcript_log import context_tokens_from_jsonl, find_latest_jsonl
from .protocol import AgentBackend, AgentState


def _exhausted_backends() -> dict[str, dict[str, Any]]:
    try:
        return backend_usage.exhausted_backends()
    except Exception as exc:  # noqa: BLE001
        log_exception("snapshotBackendQuotaFail", exc)
        return {}


def _fallback_chains() -> dict[str, list[dict[str, Any]]]:
    chains: dict[str, list[dict[str, Any]]] = {}
    try:
        for row in db.conn().execute(
                "SELECT agent_id, models_json FROM agent_model_fallbacks"):
            models = json.loads(row["models_json"] or "[]")
            if isinstance(models, list):
                chains[row["agent_id"]] = [m for m in models if isinstance(m, dict)]
    except Exception as exc:  # noqa: BLE001
        log_exception("snapshotFallbackChainsFail", exc)
    return chains


def _backend_quota(backend: str, exhausted: dict[str, dict[str, Any]],
                   fallbacks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Why this agent's next turn is expected to fail, or None when it is not."""
    quota = exhausted.get(backends.normalize(backend))
    if not quota:
        return None
    rescue = next(
        (m for m in fallbacks
         if backends.normalize(m.get("backend")) not in exhausted), None)
    return {
        **quota,
        "fallback_backend": rescue.get("backend") if rescue else None,
        "fallback_model": rescue.get("model") if rescue else None,
    }


def _compacting_check() -> Any:
    """One ``(session, observed_kind) -> bool`` for the whole snapshot.

    ``compaction.is_compacting`` asks the external runtime for its status on
    every call, which made the snapshot one socket round trip per agent. The
    runtime's status is already fetched (and briefly cached) for the liveness
    check, so read the compacting set from it once. When the runtime cannot
    answer, fall back to the persisted state exactly as ``is_compacting``
    does; when this process owns its turns, ask ``compaction`` directly.
    """
    if getattr(backends, "_RUNTIME_CLIENT", None) is None:
        return lambda session, kind: compaction.is_compacting(session)
    try:
        compacting = set(backends.runtime_status().get("compactions") or ())
    except Exception:  # noqa: BLE001 - logged once per window by backends
        return lambda session, kind: kind == AgentState.COMPACTING
    return lambda session, kind: session in compacting


def build_agent_snapshot(ctx) -> dict[str, Any]:
    """Reconcile liveness and project the dashboard from batched database reads."""
    rows = []
    is_compacting = _compacting_check()
    focus = agents_db.get_focus()
    team_memberships = team_store.memberships_by_agent()
    queue_states = turn_queue.states()
    goals = agent_goals.by_agent()
    active_jobs = background_jobs.active_by_agent()
    states = agents_db.dashboard_states()
    runtimes = agents_db.dashboard_runtimes()
    messages = message_store.dashboard_messages()
    schedules: dict[str, list] = {}
    for schedule in scheduler.list_schedules():
        schedules.setdefault(schedule['agent_id'], []).append(schedule)
    # Model portraits are projected whether or not the preference is on, so
    # toggling it in an app is instant instead of waiting for a snapshot.
    cfg = config.load()
    # Without a server context there is no bundled art to point at, so the
    # projection simply offers no model portraits.
    static_root = getattr(ctx, "static", None)
    model_avatar_root = (static_root / "avatars" / "models") if static_root else None
    default_models: dict[str, str] = {}
    visible_labels = janitors.visible_labels()
    janitor_templates = {row["agent_id"]: row["template_id"] for row in
                         db.conn().execute("SELECT agent_id,template_id FROM janitor_configs")}
    exhausted = _exhausted_backends()
    fallback_chains = _fallback_chains() if exhausted else {}
    agent_rows = agents_db.list_agents()
    # Helper tree counts come from the rows already read: no extra query.
    child_count: dict[str, int] = {}
    running_children: dict[str, int] = {}
    for a in agent_rows:
        parent_id = a.get("parent_agent_id")
        if parent_id:
            child_count[parent_id] = child_count.get(parent_id, 0) + 1
            if a.get("helper_state") == "running":
                running_children[parent_id] = running_children.get(parent_id, 0) + 1
    for a in agent_rows:
        agent_id = a["agent_id"]
        backend = a.get("backend") or AgentBackend.CLAUDE
        # Re-derive truth from reality before reading derived state (INV1-3):
        # a stuck busy row, a ghost session or a phantom in-flight slot is
        # repaired here, at read time, not on the next send.
        state = states.get(agent_id, {})
        rt = runtimes.get(agent_id, {})
        bsid = rt.get('backend_session_id') or ''
        try:
            repaired = reconcile.reconcile_agent(
                agent_id, backend, observed_state=state, bound_session=bsid)
            if 'state' in repaired:
                # Repairs are rare writes. Re-read their clocks so this very
                # response still reflects recovery, including unread timing.
                state = agents_db.latest_state(agent_id) or {}
                state['turn_started_at'] = agents_db.turn_started_at(agent_id)
                state['last_turn_end'] = agents_db.last_turn_end(agent_id)
            if 'ghost_session' in repaired:
                bsid = ''
        except Exception as e:  # noqa: BLE001
            log_exception("snapshotReconcileFail", e, detail=agent_id)
        active = bool(backends.active_handles(backend, agent_id))
        latest_state = state.get("kind")
        if active and latest_state not in {"thinking", "tool", "compacting", "background"}:
            latest_state = "thinking"
        # Agent-authored free-text status persists separately from run state so
        # it survives leader turn transitions. Older BACKGROUND rows may still
        # carry a detail label, so keep that as a compatibility fallback.
        _sdetail = state.get("detail")
        if not isinstance(_sdetail, dict):
            _sdetail = {}
        status_text = str(visible_labels.get(agent_id, a.get("custom_status")) or "").strip() or None
        if status_text is None and agent_id not in visible_labels and state.get("kind") == "background":
            status_text = str(_sdetail.get("label") or "").strip() or None
        # A durable background job (or a Clarp sub-agent, which is one) means
        # the agent is waiting on work even though its turn ended: show it as
        # background so the apps draw the running indicator, not idle.
        jobs = active_jobs.get(agent_id, [])
        sub_agents = sum(1 for job in jobs if job["kind"] == "sub-agent")
        if jobs and latest_state not in {"thinking", "tool", "compacting", "background"}:
            latest_state = "background"
        if jobs and status_text is None:
            if len(jobs) == 1:
                status_text = jobs[0]["title"] or ("Sub-agent running" if sub_agents else "Background job running")
            elif sub_agents == len(jobs):
                status_text = f"{len(jobs)} sub-agents running"
            else:
                status_text = f"{len(jobs)} background jobs running"
        turn_started_at = int(state.get('turn_started_at') or 0)
        if active and not turn_started_at:
            turn_started_at = int(rt.get('open_turn_started_at') or 0)
        if active and turn_started_at <= 0:
            # Busy with no recorded turn start (a handle adopted mid-turn):
            # "since now" is honest; 0 read as 1970 in a client's timer.
            turn_started_at = now_ms()
        message = messages.get(agent_id, {})
        message_head = message.get('head', {'preview': '', 'message_id': ''})
        # Agree with /log's contract: no bound backend session means an empty
        # conversation at revision 0. Querying with an empty session id
        # dropped the WHERE clause and returned the MAX over every
        # conversation, so the client saw a head it could never reach and
        # reloaded the full transcript on every poll (audit bug D1).
        head_revision = message.get('revisions', {}).get(bsid, 0) if bsid else 0
        # Context-window occupancy from the transcript, only for an adapter
        # that declares a context window (Claude). Codex/agy auto-compact in
        # their own loops, so an empty gauge there correctly signals "managed
        # automatically". Computed from the last assistant message's usage,
        # not the cumulative result event.
        context_tokens = None
        context_window = backends.adapter_for(backend).context_window
        if context_window is not None and bsid:
            j = find_latest_jsonl(bsid)
            if j is not None:
                context_tokens = context_tokens_from_jsonl(j)
        mcp_servers = _agent_mcp_list(a.get("mcp_servers"))
        # Only an Agent still wearing its bundled persona portrait can wear a
        # model variant of it; an uploaded or generated portrait is the
        # user's own choice and is never swapped out from under them.
        model_avatar_url = ""
        if model_avatar_root is not None and not str(a.get("avatar_path") or ""):
            resolved_backend = backends.normalize(backend)
            if resolved_backend not in default_models:
                default_models[resolved_backend] = backends.default_model_effort(
                    resolved_backend, cfg)[0]
            model_avatar_url = model_avatars.url_for(
                a["persona"], backend, a.get("model") or "",
                root=model_avatar_root,
                default_model=default_models[resolved_backend])
        rows.append({
            "agent_id":       agent_id,
            "persona":        a["persona"],
            "voice_id":       a["voice_id"],
            "avatar_symbol":  a.get("avatar_symbol") or "",
            "avatar_url": janitor_avatar_url(bool(a.get("is_janitor")),
                janitor_templates.get(agent_id), static_root=static_root) or versioned_avatar_url(
                "/avatars", agent_id, str(a.get("avatar_path") or "")) or (
                "" if a.get("avatar_symbol") else static_persona_avatar_url(
                    a["persona"], backend, static_root=static_root)),
            "model_avatar_url": model_avatar_url,
            "cwd":            a["cwd"],
            "session":        a["session"],
            "backend":        backend,
            "model":          agent_model(a, bsid),
            "effort":         a.get("effort") or "",
            "mcp_servers":    mcp_servers,
            "schedules":      schedules.get(agent_id, []),
            "is_janitor": bool(a.get("is_janitor")),
            "parent_agent_id": a.get("parent_agent_id") or None,
            "role": a.get("role") or "agent",
            "helper_state": a.get("helper_state") or None,
            "child_count": child_count.get(agent_id, 0),
            "running_children": running_children.get(agent_id, 0),
            "interaction_capabilities": agents_db.interaction_capabilities(a),
            "heartbeat_enabled": bool(a.get("heartbeat_enabled")),
            "dreaming_enabled": bool(a.get("dreaming_enabled")),
            "muted":          bool(a.get("muted")),
            "archived_at":    a.get("archived_at"),
            "backend_session_id": bsid,
            "alive":          True,
            "busy":           active or state.get('kind') in AgentState.busy_states(),
            "focused":        agent_id == focus,
            # An agent with no user messages yet has no message activity, and
            # a bare 0 sorted it below every idle chat — so an agent spawned by
            # another agent was invisible in Chats while it was already working.
            # Floor the value at creation time: a new agent enters the list at
            # its own age and decays normally, and operational state still never
            # reorders an established conversation.
            # Chat ordering uses its own presentation clock. ``activity`` is
            # retained as the scheduler's user-engagement clock for callers
            # that need it; never let routine/tool/import state reorder chats.
            "last_activity":  max(int(message.get('chat_activity', 0) or 0),
                                  int(a.get("created_at") or 0)),
            "last_turn_end":  int(state.get('last_turn_end') or 0),
            # Eager last-message preview for the agent-list overview, so the
            # client shows it without opening each chat.
            "last_message":   message_head["preview"],
            "last_completed_message": message.get("completed_head", {}).get("preview", ""),
            # Version the eager preview against the same canonical conversation
            # used by /log. Clients can now prove that an instant cached chat is
            # behind the overview instead of displaying two silently divergent
            # projections.
            "conversation_id": bsid,
            "head_revision": head_revision,
            "last_message_id": message_head["message_id"],
            "turn_started_at": turn_started_at,
            "latest_state":   latest_state,
            "status_text":    status_text,
            "background_jobs": {"count": len(jobs), "sub_agents": sub_agents},
            "team_ids":       team_memberships.get(agent_id, []),
            "latest_state_ts": state.get("ts"),
            "context_tokens": context_tokens,
            # Window the tokens fill, as the adapter declares it; the native
            # gauge divides tokens by this. None for codex/agy (they
            # auto-compact, so no gauge).
            "context_window": context_window,
            "compacting":     is_compacting(a["session"], state.get("kind")),
            "queued_turn_count": queue_states.get(agent_id, {}).get("count", 0),
            "queued_turn_revision": queue_states.get(agent_id, {}).get("revision", 0),
            "queue_paused": bool(queue_states.get(agent_id, {}).get("paused", False)),
            "goal":           agent_goals.public(goals.get(agent_id)),
            # Advisory only: a send is never refused, because a fallback
            # model or an account switch may still serve it.
            "backend_quota": _backend_quota(
                backend, exhausted, fallback_chains.get(agent_id, [])),
            "activity":       state_activity_event(
                agent_id=agent_id,
                session=a["session"],
                persona=a["persona"],
                kind=state.get("kind") or "",
                ts=int(state.get("ts") or 0),
                detail=state.get("detail") if isinstance(state.get("detail"), dict) else {},
            ) if state.get("kind") else None,
        })
    persona_rows = persona_store.list_all()
    roster = []
    roster_keys = set()
    for name in [
        *(row["name"] for row in persona_rows),
        # Janitors are maintenance identities, never chat targets, so their
        # personas must not appear in the roster clients offer for switching
        # or starting an agent.
        *(str(row["persona"]) for row in rows
          if row.get("persona") and not row.get("is_janitor")),
    ]:
        key = name.strip().casefold()
        if key and key not in roster_keys:
            roster.append(name.strip())
            roster_keys.add(key)
    return {
        "agents": rows,
        "focus": focus,
        "roster": roster,
        "personas": [persona_store.public(row) for row in persona_rows],
        # Whether clients should prefer the model portrait where one exists.
        "model_avatars": avatar_settings.get()["model_avatars"],
        # The menu of MCP servers an agent can be granted (from ~/.claude.json).
        "available_mcp_servers": sorted(config.read_global_mcp_servers().keys()),
    }


def _agent_mcp_list(raw: str | None) -> list[str]:
    from .mcp_selection import decode
    return decode(raw)[1]
