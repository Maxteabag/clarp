"""Text cleaning for stored messages.

Classifies automated rows (heartbeat, leader tick, dreaming, watcher), renders
their display text, and strips the server-injected prompt context that the
backend transcript copies back into user turns.
"""
from __future__ import annotations

from . import dreaming, heartbeat, origins, team_leader
from .voice_markup import clean_for_display, strip_hidden_blocks
from .message_text import (
    CALL_OPENING_CLOSE,
    CALL_OPENING_OPEN,
    FALLBACK_CONTEXT_CLOSE,
    FALLBACK_CONTEXT_OPEN,
    RESTORED_CONTEXT_CLOSE,
    RESTORED_CONTEXT_OPEN,
    TEAM_CONTEXT_CLOSE,
    TEAM_CONTEXT_OPEN,
    _strip_block,
    _strip_voice_markup,
    strip_injected_context,
    strip_injected_team_context,
)


def _automation_kind(*, role: str, origin: str | None,
                     text: str | None) -> str:
    raw_origin = (origin or "user").strip() or "user"
    # External watchers are user-facing, but their trigger and resulting turn
    # are still automated chat activity. Keep display classification separate
    # from notification policy so they can be collapsed without silencing them.
    if raw_origin == "watcher":
        return "watcher"
    if raw_origin in origins.ROUTINE_AUTOMATION_ORIGINS:
        return raw_origin
    if (
        role == "user"
        and raw_origin == "schedule"
        and team_leader.should_skip_leader_tick_prompt(text or "")
    ):
        return "leader_tick"
    return ""


def _display_text_for_message(*, role: str, origin: str | None,
                              text: str | None) -> str:
    raw = strip_hidden_blocks(str(text or ""))
    kind = _automation_kind(role=role, origin=origin, text=raw)
    if not kind:
        return raw
    if role == "user":
        return {
            "heartbeat": "Automated heartbeat check",
            "leader_tick": "Automated leader check",
            "dreaming": "Automated dreaming run",
            "watcher": "Automated watcher event",
        }.get(kind, raw)
    if kind == "heartbeat" and heartbeat.HEARTBEAT_OK in raw:
        return "Heartbeat check: no action needed."
    if kind == "leader_tick" and team_leader.LEADER_NOOP in raw:
        return "Leader check: no action needed."
    if kind == "dreaming" and dreaming.DREAMING_OK in raw:
        return "Dreaming check: no action needed."
    return raw
