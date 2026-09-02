"""Agent dashboard read model."""
from __future__ import annotations

from typing import Any

import json

from . import (agents as agents_db, backends, compaction, config,
               message_store, team_store, turn_queue)
from . import reconcile
from . import personas as persona_store
from .avatar_urls import versioned_avatar_url
from .log import log_exception
from .activity import state_activity_event
from .transcript_log import context_tokens_from_jsonl, find_latest_jsonl
from .protocol import AgentBackend


def build_agent_snapshot(ctx) -> dict[str, Any]:
    """Return the UI's full agent snapshot without mutating agent state."""
    rows = []
    focus = agents_db.get_focus()
    team_memberships = team_store.memberships_by_agent()
    queue_states = turn_queue.states()
    for a in agents_db.list_agents():
        agent_id = a["agent_id"]
        backend = a.get("backend") or AgentBackend.CLAUDE
        # Re-derive truth from reality before reading derived state (INV1-3):
        # a stuck busy row, a ghost session or a phantom in-flight slot is
        # repaired here, at read time, not on the next send.
        try:
            reconcile.reconcile_agent(agent_id, backend)
        except Exception as e:  # noqa: BLE001
            log_exception("snapshotReconcileFail", e, detail=agent_id)
        state = agents_db.latest_state(agent_id) or {}
        active = bool(backends.active_handles(backend, agent_id))
        rt = agents_db.conn().execute(
            """SELECT backend_session_id FROM runtimes
                WHERE agent_id = ? AND ended_at IS NULL
                ORDER BY started_at DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()
        open_turn = agents_db.conn().execute(
            """SELECT started_at FROM turns
                WHERE agent_id = ? AND ended_at IS NULL
                ORDER BY started_at DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()
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
        turn_started_at = agents_db.turn_started_at(agent_id)
        if active and not turn_started_at and open_turn:
            turn_started_at = int(open_turn["started_at"] or 0)
        bsid = (rt and rt["backend_session_id"]) or ""
        message_head = message_store.last_message_head(agent_id=agent_id)
        # Agree with /log's contract: no bound backend session means an empty
        # conversation at revision 0. Querying with an empty session id
        # dropped the WHERE clause and returned the MAX over every
        # conversation, so the client saw a head it could never reach and
        # reloaded the full transcript on every poll (audit bug D1).
        head_revision = (message_store.latest_revision(
            agent_id=agent_id, backend_session_id=bsid) if bsid else 0)
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
        rows.append({
            "agent_id":       agent_id,
            "persona":        a["persona"],
            "voice_id":       a["voice_id"],
            "avatar_symbol":  a.get("avatar_symbol") or "",
            "avatar_url": versioned_avatar_url(
                "/avatars", agent_id, str(a.get("avatar_path") or "")),
            "cwd":            a["cwd"],
            "session":        a["session"],
            "backend":        backend,
            "model":          a.get("model") or "",
            "effort":         a.get("effort") or "",
            "mcp_servers":    mcp_servers,
            "heartbeat_enabled": bool(a.get("heartbeat_enabled")),
            "dreaming_enabled": bool(a.get("dreaming_enabled")),
            "muted":          bool(a.get("muted")),
            "archived_at":    a.get("archived_at"),
            "backend_session_id": bsid,
            "alive":          True,
            "busy":           active or bool(agents_db.is_busy(agent_id)),
            "focused":        agent_id == focus,
            "last_activity":  agents_db.last_activity(agent_id),
            "last_turn_end":  agents_db.last_turn_end(agent_id),
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
        # The menu of MCP servers an agent can be granted (from ~/.claude.json).
        "available_mcp_servers": sorted(config.read_global_mcp_servers().keys()),
    }


def _agent_mcp_list(raw: str | None) -> list[str]:
    from .mcp_selection import decode
    return decode(raw)[1]
