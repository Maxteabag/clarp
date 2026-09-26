"""Whether a completed turn pages the user.

``user_notifications.classify_completed_turn`` waits for the turn's final
assistant row, gathers the facts below and persists whatever this policy
decides. The invariant is deliberately small: a turn notifies when it has
deliberate user-facing content; push follows that decision unless the agent is
muted. Nothing here touches the database.

Decisions:

* ``Notify(reason, kind, push, muted)``  badge and mark unread; ``push`` is
  false for a muted agent and ``reason`` then carries the ``-muted`` suffix.
* ``Suppress(reason)``  stay silent; ``reason`` is the durable audit string.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import origins

USER_FACING_ORIGINS = origins.USER_FACING_ORIGINS
SUPPRESSED_ORIGINS = origins.SUPPRESSED_ORIGINS

# Content kinds a completion may carry, as ``_select_content_source`` names them.
CONTENT_SPEAK = "speak"
CONTENT_TEXT_REPLY = "text-reply"
NOTIFYING_CONTENT = frozenset({CONTENT_SPEAK, CONTENT_TEXT_REPLY})


@dataclass(frozen=True)
class CompletedTurn:
    """What the completion looked like once the transcript settled."""
    agent_id: str
    has_cause: bool          # a causing user row was found
    content: str = ""        # "speak", "text-reply" or "" when nothing addressed the user
    interrupted: bool = False  # the causing message carries an interruption marker


@dataclass(frozen=True)
class AgentCapabilities:
    """The completing agent as ``agents.interaction_capabilities`` sees it."""
    present: bool = False
    can_chat: bool = True
    muted: bool = False
    is_team_leader: bool = False


@dataclass(frozen=True)
class NotificationSettings:
    special_automation: bool = False


@dataclass(frozen=True)
class Notify:
    reason: str
    kind: str
    push: bool
    muted: bool


@dataclass(frozen=True)
class Suppress:
    reason: str


Decision = Notify | Suppress


def notify_decision(origin: str, completed_turn: CompletedTurn,
                    capabilities: AgentCapabilities,
                    settings: NotificationSettings,
                    desktop_present: bool = False) -> Decision:
    """Decide whether the completed turn pages the user.

    ``desktop_present`` is accepted so the caller has one place to hand
    presence over; today presence is applied at delivery (``apns``) and does
    not change classification, and a test pins that.
    """
    origin = (origin or "").strip()
    if origin == "janitor" or (capabilities.present and not capabilities.can_chat):
        return Suppress("janitor-maintenance")
    if not completed_turn.agent_id:
        return Suppress("missing-agent")
    if not completed_turn.has_cause:
        return Suppress("missing-causing-row")
    if settings.special_automation and origin not in USER_FACING_ORIGINS:
        return Suppress(f"not-user-facing-origin:{origin or 'unknown'}")
    if (settings.special_automation and origin == "leader_tick"
            and not capabilities.is_team_leader):
        return Suppress("leader-tick-non-leader")
    content = completed_turn.content
    if content in NOTIFYING_CONTENT:
        muted = capabilities.muted
        return Notify(reason=f"{content}-muted" if muted else content,
                      kind=content, push=not muted, muted=muted)
    if content:
        # An unknown content kind is recorded but never pages anyone.
        return Suppress(content)
    if completed_turn.interrupted:
        # The turn was killed before it could say anything; that is not the
        # same event as an agent that had nothing to say.
        return Suppress("turn-interrupted")
    return Suppress("no-user-facing-content")
