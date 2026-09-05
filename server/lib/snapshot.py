"""Agent dashboard read model."""
from __future__ import annotations

from typing import Any

import json

from . import (agents as agents_db, avatar_settings, backends, compaction,
               config, message_store, model_avatars, team_store,
               turn_queue, scheduler)
from . import reconcile
from . import personas as persona_store
from .avatar_urls import versioned_avatar_url
from .log import log_exception
from .activity import state_activity_event
from .transcript_log import context_tokens_from_jsonl, find_latest_jsonl
from .protocol import AgentBackend, AgentState


def build_agent_snapshot(ctx) -> dict[str, Any]:
    """Reconcile liveness and project the dashboard from batched database reads."""
    rows = []
    focus = agents_db.get_focus()
    team_memberships = team_store.memberships_by_agent()
    queue_states = turn_queue.states()
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
    for a in agents_db.list_agents():
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
        status_text = str(a.get("custom_status") or "").strip() or None
        if status_text is None and state.get("kind") == "background":
            status_text = str(_sdetail.get("label") or "").strip() or None
        turn_started_at = int(state.get('turn_started_at') or 0)
        if active and not turn_started_at:
            turn_started_at = int(rt.get('open_turn_started_at') or 0)
        message = messages.get(agent_id, {})
        message_head = message.get('head', {'preview': '', 'message_id': ''})
        # Agree with /log's contract: no bound backend session means an empty
        # conversation at revision 0. Querying with an empty session id
        # dropped the WHERE clause and returned the MAX over every
        # conversation, so the client saw a head it could never reach and
        # reloaded the full transcript on every poll (audit bug D1).
        head_revision = message.get('revisions', {}).get(bsid, 0) if bsid else 0
        # Context-window occupancy from the transcript (Claude only — Codex/agy
        # auto-compact in their own loops, so an empty gauge there correctly
        # signals "managed automatically"). Computed from the last assistant
        # message's usage, not the cumulative result event.
        context_tokens = None
        if backends.normalize(backend) == backends.CLAUDE and bsid:
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
            "avatar_url": versioned_avatar_url(
                "/avatars", agent_id, str(a.get("avatar_path") or "")),
            "model_avatar_url": model_avatar_url,
            "cwd":            a["cwd"],
            "session":        a["session"],
            "backend":        backend,
            "model":          a.get("model") or "",
            "effort":         a.get("effort") or "",
            "mcp_servers":    mcp_servers,
            "schedules":      schedules.get(agent_id, []),
            "heartbeat_enabled": bool(a.get("heartbeat_enabled")),
            "dreaming_enabled": bool(a.get("dreaming_enabled")),
            "muted":          bool(a.get("muted")),
            "archived_at":    a.get("archived_at"),
            "backend_session_id": bsid,
            "alive":          True,
            "busy":           active or state.get('kind') in AgentState.busy_states(),
            "focused":        agent_id == focus,
            "last_activity":  message.get('activity', 0),
            "last_turn_end":  int(state.get('last_turn_end') or 0),
            # Eager last-message preview for the agent-list overview, so the
            # client shows it without opening each chat.
            "last_message":   message_head["preview"],
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
            "team_ids":       team_memberships.get(agent_id, []),
            "latest_state_ts": state.get("ts"),
            "context_tokens": context_tokens,
            # Window the tokens fill. This deployment runs opus-*[1m] (the 1M
            # context beta, per ~/.claude.json), so Claude agents get 1M; the
            # native gauge divides tokens by this. None for codex/agy (they
            # auto-compact, so no gauge).
            "context_window": (1_000_000
                               if backends.normalize(backend) == backends.CLAUDE
                               else None),
            "compacting":     compaction.is_compacting(a["session"]),
            "queued_turn_count": queue_states.get(agent_id, {}).get("count", 0),
            "queued_turn_revision": queue_states.get(agent_id, {}).get("revision", 0),
            "queue_paused": bool(queue_states.get(agent_id, {}).get("paused", False)),
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
        *(str(row["persona"]) for row in rows if row.get("persona")),
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
