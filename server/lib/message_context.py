"""Text cleaning for stored messages.

Classifies automated rows (heartbeat, leader tick, dreaming, watcher), renders
their display text, and strips the server-injected prompt context that the
backend transcript copies back into user turns.
"""
from __future__ import annotations

from . import dreaming, heartbeat, origins, team_leader
from .voice_markup import clean_for_display, strip_hidden_blocks


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


def _strip_voice_markup(text: str | None) -> str:
    """Normalized, markup-free single-line form of a reply — used to compare a
    streamed turn (markup intact) against its durable copy when superseding the
    live row. Delegates to the canonical cleaner so the rules never drift."""
    return clean_for_display(text, oneline=True)


# The server appends a team-context block to the prompt the backend sees (see
# turn_dispatch._with_team_context). The backend's transcript copies the whole
# augmented prompt back, so on import we must strip the block from user turns —
# otherwise it leaks into the chat as if User typed it. Marker shared with
# turn_dispatch.
TEAM_CONTEXT_OPEN = "--- Clarp team context ---"
TEAM_CONTEXT_CLOSE = "--- End Clarp team context ---"
FALLBACK_CONTEXT_OPEN = "--- Clarp fallback context ---"
FALLBACK_CONTEXT_CLOSE = "--- End Clarp fallback context ---"


def strip_injected_team_context(text: str) -> str:
    """Drop server-injected team-context blocks from a user turn.

    Older turns appended the block after the user's text; newer turns prepend it
    for prompt-cache friendliness. Preserve the real user text in either order.
    """
    if not text or TEAM_CONTEXT_OPEN not in text:
        return text
    out = text
    while TEAM_CONTEXT_OPEN in out:
        before, rest = out.split(TEAM_CONTEXT_OPEN, 1)
        if TEAM_CONTEXT_CLOSE not in rest:
            return before.rstrip()
        _, after = rest.split(TEAM_CONTEXT_CLOSE, 1)
        out = (before + after).strip()
    return out


def strip_injected_context(text: str) -> str:
    """Drop every server-injected block from a user turn.

    A block appended without delimiters was imported as part of the user's own
    words, so the chat showed the user's message twice: once as they typed it
    and once several kilobytes long with the injection attached.
    """
    return _strip_block(strip_injected_team_context(text),
                        FALLBACK_CONTEXT_OPEN, FALLBACK_CONTEXT_CLOSE)


def _strip_block(text: str, opening: str, closing: str) -> str:
    if not text or opening not in text:
        return text
    out = text
    while opening in out:
        before, rest = out.split(opening, 1)
        if closing not in rest:
            return before.rstrip()
        _, after = rest.split(closing, 1)
        out = (before + after).strip()
    return out
