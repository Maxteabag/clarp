"""Agents whose activity never claims the owner's attention by itself.

Janitors are maintenance workers. Their turns, replies, decisions and
artifacts stay readable when the owner opens the Janitor's chat, but nothing
they do pushes to the phone, badges or marks a chat unread, enters Updates /
``GET /attention``, or shows in the "agents working" Live Activity. The one
exception is ``janitor_attention``'s ``janitor_failure`` items: they are how a
broken Janitor surfaces, and they come through their own path.

Every attention path asks this module rather than reading ``is_janitor``
itself, so the rule has one definition. Snapshot rows carry it as ``quiet``
(docs/notification-policy.md, "Quiet agents").
"""
from __future__ import annotations

from typing import Any


def is_quiet(agent: dict[str, Any] | None) -> bool:
    """Whether this agent row (``agents`` columns) is a quiet agent."""
    return bool(agent and agent.get("is_janitor"))


def is_quiet_id(agent_id: str) -> bool:
    """``is_quiet`` for an agent id; an unknown agent is not quiet."""
    from . import agents
    agent_id = str(agent_id or "").strip()
    return bool(agent_id) and is_quiet(agents.get_by_agent_id(agent_id))


def loud_sql(alias: str) -> str:
    """A SQL condition, on the ``agents`` table aliased ``alias``, that keeps
    only agents that are not quiet."""
    return f"COALESCE({alias}.is_janitor, 0) = 0"
