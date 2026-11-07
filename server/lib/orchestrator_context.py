"""Moved out of server/lib/orchestrator.py by tools/refactor/split_apply.py (split 'orchestrator', recipe tools/refactor/splits/s9-orchestrator.toml).
orchestrator.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import asdict
from typing import Any
from . import agents as agents_db
from . import identity
from .db import conn, now_ms
from .orchestrator_types import CLARIFY_CONFIDENCE, HIGH_CONFIDENCE, MESSAGE_CONTEXT_CHARS, OrchestratorSettings, PENDING_CONTEXT_CHARS, RECENT_AGENT_WINDOW_MS, RECENT_MESSAGES_PER_ROLE, STATE_DETAIL_CONTEXT_CHARS, STICKY_CONFIDENCE


def build_context_packet(
    *,
    utterance: str,
    requested_session: str,
    trace_id: str,
    hands_free: bool,
    settings: OrchestratorSettings,
    context_scope: str = "all",
    fallback_request: bool = False,
    only_sessions: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """The routing packet. ``only_sessions`` limits the candidate agents to a
    group call's reachable participants (docs/group-calls.md)."""
    focus_agent_id = agents_db.get_focus()
    focus = identity.lookup(focus_agent_id)
    focus_session = (focus or {}).get("session") or requested_session
    include_all_context = context_scope == "all"
    detailed_sessions = {requested_session, focus_session}
    agents = []
    message_count = 0
    all_agents = [agent for agent in agents_db.list_agents()
                  if not agent.get("archived_at")
                  and agents_db.interaction_capabilities(agent)["can_voice_target"]]
    if only_sessions is not None:
        all_agents = [agent for agent in all_agents if agent["session"] in only_sessions]
    if fallback_request:
        cutoff = now_ms() - RECENT_AGENT_WINDOW_MS
        all_agents = [
            agent for agent in all_agents
            if agent["session"] in detailed_sessions
            or _agent_context_activity(agent) >= cutoff
        ]
    for agent in all_agents:
        agent_id = agent["agent_id"]
        include_detail = include_all_context or agent["session"] in detailed_sessions
        user_messages = (
            _recent_messages(agent_id, role="user", limit=RECENT_MESSAGES_PER_ROLE)
            if include_detail else []
        )
        assistant_messages = (
            _recent_messages(agent_id, role="assistant", limit=RECENT_MESSAGES_PER_ROLE)
            if include_detail else []
        )
        message_count += len(user_messages) + len(assistant_messages)
        state = _latest_state(agent_id) if include_detail else None
        agents.append({
            "session": agent["session"],
            "persona": agent["persona"],
            "voice_id": agent["voice_id"],
            "cwd": agent["cwd"],
            "backend": agent.get("backend"),
            "is_requested": agent["session"] == requested_session,
            "is_sticky_focus": bool(focus and focus["agent_id"] == agent_id),
            "state": _compact_state(state),
            "recent_user_messages": user_messages,
            "recent_agent_messages": assistant_messages,
        })
    pending = _pending_utterances(limit=3)
    candidates = _name_candidates(utterance, agents)
    stable = {
        "utterance": utterance,
        "requested_session": requested_session,
        "focus_session": focus_session,
        "context_scope": "all" if include_all_context else "focused",
        "fallback_request": fallback_request,
        "agents": agents,
        "pending": pending,
        "candidate_name_matches": candidates,
    }
    context_hash = hashlib.sha1(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return {
        **stable,
        "trace_id": trace_id,
        "hands_free": hands_free,
        "settings": asdict(settings),
        "context_summary": {
            "hash": context_hash,
            "scope": "all" if include_all_context else "focused",
            "agent_count": len(agents),
            "message_count": message_count,
        },
        "routing_policy": {
            "wrong_agent_is_worse_than_clarification": True,
            "sticky_bias_session": focus_session,
            "high_confidence": HIGH_CONFIDENCE,
            "sticky_confidence": STICKY_CONFIDENCE,
            "clarify_confidence": CLARIFY_CONFIDENCE,
            "automatic_confidence_threshold": settings.confidence_threshold,
            "recent_agent_window_minutes": 30 if fallback_request else None,
        },
    }


def _agent_context_activity(agent: dict[str, Any]) -> int:
    latest_state = _latest_state(agent["agent_id"])
    return max(
        int(agent.get("created_at") or 0),
        int(agents_db.last_activity(agent["agent_id"]) or 0),
        int(latest_state["ts"] or 0) if latest_state else 0,
    )


def _recent_messages(agent_id: str, *, role: str, limit: int) -> list[dict[str, Any]]:
    rows = conn().execute(
        """SELECT role, text, trace_id, source, created_at
             FROM (
                   SELECT role, text, trace_id, source, created_at
                     FROM agent_routing_messages
                    WHERE agent_id = ? AND role = ?
                   UNION ALL
                   SELECT role, text, '' AS trace_id, 'transcript' AS source, updated_at AS created_at
                     FROM messages
                    WHERE agent_id = ? AND role = ?
             )
            ORDER BY created_at DESC
            LIMIT ?""",
        (agent_id, role, agent_id, role, max(1, limit)),
    ).fetchall()
    out = [dict(r) for r in reversed(rows)]
    for item in out:
        item["text"] = _compact_text(item.get("text"), MESSAGE_CONTEXT_CHARS)
    return out


def _latest_state(agent_id: str):
    return conn().execute(
        """SELECT kind, detail, ts FROM state_log
            WHERE agent_id = ?
            ORDER BY ts DESC, state_id DESC LIMIT 1""",
        (agent_id,),
    ).fetchone()


def _compact_state(row) -> dict[str, Any] | None:
    if not row:
        return None
    state = dict(row)
    state["detail"] = _compact_text(state.get("detail"), STATE_DETAIL_CONTEXT_CHARS)
    return state


def _compact_text(value: Any, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _pending_utterances(limit: int) -> list[dict[str, Any]]:
    now = now_ms()
    rows = conn().execute(
        """SELECT pending_id, trace_id, utterance, requested_session,
                  candidate_session, speak_as_session, reason, created_at,
                  expires_at
             FROM orchestrator_pending_utterances
            WHERE status = 'pending' AND expires_at > ?
            ORDER BY created_at DESC
            LIMIT ?""",
        (now, max(1, limit)),
    ).fetchall()
    out = [dict(r) for r in rows]
    for item in out:
        item["utterance"] = _compact_text(item.get("utterance"), PENDING_CONTEXT_CHARS)
        item["reason"] = _compact_text(item.get("reason"), PENDING_CONTEXT_CHARS)
    return out


def _name_candidates(utterance: str, agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tokens = re.findall(r"[A-Za-z0-9]+", utterance.lower())[:12]
    out = []
    for agent in agents:
        names = {agent["session"].lower(), agent["persona"].lower()}
        best = 0.0
        matched = ""
        for token in tokens:
            for name in names:
                score = difflib.SequenceMatcher(None, token, name).ratio()
                if score > best:
                    best = score
                    matched = token
        if best >= 0.62:
            out.append({
                "session": agent["session"],
                "persona": agent["persona"],
                "matched_token": matched,
                "score": round(best, 3),
            })
    return sorted(out, key=lambda x: x["score"], reverse=True)
