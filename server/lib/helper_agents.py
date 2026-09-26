"""Side effects of the helper-agent lifecycle.

``policies.helper_state`` decides how a helper moves; ``agents`` stores the
result. This module connects the two to what happens on the Host:

* an agent-origin message from a helper to its parent means it reported,
  and one from the parent to a reported helper means it has more work;
* the parent or the user marks a helper done, failed or running again;
* the maintenance worker archives helpers that have been done for longer
  than the grace period.

Every change sends ``agent-roster`` with ``kind: "helper-state"`` so the apps
refetch the snapshot. Deleting or archiving a parent is handled inside the
store (``agents.abandon_children``), because every path that does it goes
through there.
"""
from __future__ import annotations

from . import agents as agents_db
from . import events, identity
from .log import log
from .policies import helper_state as helper_policy


class HelperStateError(RuntimeError):
    def __init__(self, status: int, code: str, message: str = ""):
        self.status = status
        self.code = code
        super().__init__(message or code)


def _announce(stream, agent_id: str) -> None:
    if stream is None:
        return
    row = agents_db.get_by_agent_id(agent_id)
    if row:
        events.broadcast(stream, events.agent_roster("helper-state", session=row["session"]))


def note_agent_message(stream, *, sender_agent_id: str, target_agent_id: str) -> str | None:
    """Advance a helper after an agent-origin message was admitted.

    Returns the helper's new state, or None when the message did not move
    one (the two agents are not parent and helper, or the event does not
    apply in the helper's current state)."""
    if not sender_agent_id or not target_agent_id or sender_agent_id == target_agent_id:
        return None
    sender = agents_db.get_by_agent_id(sender_agent_id) or {}
    target = agents_db.get_by_agent_id(target_agent_id) or {}
    if sender.get("parent_agent_id") == target_agent_id:
        helper, event = sender_agent_id, helper_policy.HelperEvent.REPORTED
    elif target.get("parent_agent_id") == sender_agent_id:
        helper, event = target_agent_id, helper_policy.HelperEvent.PARENT_PROMPTED
    else:
        return None
    state = agents_db.apply_helper_event(helper, event)
    if state:
        log("helperState", f"{helper} -> {state} ({event})")
        _announce(stream, helper)
    return state


def mark(stream, helper: str, state: str, *, by: str = "") -> str:
    """Set a helper's state by hand. ``by`` is the marking agent, when an
    agent does it; only the helper's parent may. No ``by`` means the user.

    Marking a helper with the state it already has is a no-op that succeeds.
    """
    try:
        wanted = helper_policy.HelperState(state)
        event = helper_policy.MARKABLE[wanted]
    except (ValueError, KeyError):
        raise HelperStateError(400, "invalid_state",
                               "state must be done, failed or running") from None
    row = identity.lookup(helper)
    if not row:
        raise HelperStateError(404, "agent_not_found", f"No live agent: {helper}")
    row = agents_db.get_by_agent_id(row["agent_id"]) or row
    if row.get("role") != helper_policy.Role.HELPER:
        raise HelperStateError(409, "not_a_helper", f"{row['session']} is not a helper")
    if by:
        marker = identity.resolve(by)
        if marker is None:
            raise HelperStateError(404, "agent_not_found", f"No live agent: {by}")
        if marker.agent_id != row.get("parent_agent_id"):
            raise HelperStateError(403, "not_parent",
                                   "Only the helper's parent or the user may mark it")
    if row.get("helper_state") == wanted:
        return wanted.value
    new = agents_db.apply_helper_event(row["agent_id"], event)
    if new is None:
        raise HelperStateError(409, "illegal_transition",
                               f"{row['session']} is {row.get('helper_state')}; cannot mark {state}")
    log("helperState", f"{row['agent_id']} -> {new} (marked by {by or 'user'})")
    _announce(stream, row["agent_id"])
    return new


def archive_finished(*, now_ms: int, grace_ms: int, stream=None) -> list[str]:
    """Archive helpers that have been done for longer than ``grace_ms``."""
    due = helper_policy.archive_due(agents_db.helper_rows(), now_ms=now_ms, grace_ms=grace_ms)
    for agent_id in due:
        agents_db.set_archived(agent_id, True)
        _announce(stream, agent_id)
    return due
