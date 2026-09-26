"""How a helper agent's lifecycle moves, and who may be whose parent.

A helper is an agent created by another agent (its parent) to do one piece of
work. ``agents.helper_state`` tracks that work:

    running   the helper is working
    reported  its report reached the parent as an agent-origin message
    done      the parent or the user accepted the result
    failed    the work ended in a terminal failure
    abandoned the parent was deleted or archived before the work was done

``next_state`` is the whole transition table; the store asks it before every
write and ignores events it refuses. ``archive_due`` picks the finished
helpers the maintenance worker archives, and ``parent_refusal`` rejects
self-parenting and cycles. No IO happens here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping, Sequence


class Role(StrEnum):
    AGENT = "agent"
    HELPER = "helper"
    JANITOR = "janitor"


class HelperState(StrEnum):
    RUNNING = "running"
    REPORTED = "reported"
    DONE = "done"
    FAILED = "failed"
    ABANDONED = "abandoned"


class HelperEvent(StrEnum):
    REPORTED = "reported"            # the helper messaged its parent
    PARENT_PROMPTED = "parent_prompted"  # the parent sent it more work
    MARKED_DONE = "marked_done"      # the parent or the user accepted it
    MARKED_RUNNING = "marked_running"  # the parent or the user reopened it
    FAILED = "failed"                # terminal failure
    PARENT_GONE = "parent_gone"      # the parent was deleted or archived


# Roles an ordinary create may ask for. Janitors have their own creation path.
CREATABLE_ROLES = frozenset({Role.AGENT, Role.HELPER})
# States a caller may set by hand, mapped to the event that means it.
MARKABLE = {
    HelperState.DONE: HelperEvent.MARKED_DONE,
    HelperState.RUNNING: HelperEvent.MARKED_RUNNING,
    HelperState.FAILED: HelperEvent.FAILED,
}
FINISHED = frozenset({HelperState.DONE, HelperState.FAILED, HelperState.ABANDONED})
DEFAULT_ARCHIVE_GRACE_MS = 24 * 60 * 60 * 1000

_S, _E = HelperState, HelperEvent
TRANSITIONS: Mapping[tuple[HelperState, HelperEvent], HelperState] = {
    (_S.RUNNING, _E.REPORTED): _S.REPORTED,
    (_S.REPORTED, _E.PARENT_PROMPTED): _S.RUNNING,
    (_S.RUNNING, _E.MARKED_DONE): _S.DONE,
    (_S.REPORTED, _E.MARKED_DONE): _S.DONE,
    (_S.FAILED, _E.MARKED_DONE): _S.DONE,
    (_S.ABANDONED, _E.MARKED_DONE): _S.DONE,
    (_S.REPORTED, _E.MARKED_RUNNING): _S.RUNNING,
    (_S.DONE, _E.MARKED_RUNNING): _S.RUNNING,
    (_S.FAILED, _E.MARKED_RUNNING): _S.RUNNING,
    (_S.ABANDONED, _E.MARKED_RUNNING): _S.RUNNING,
    (_S.RUNNING, _E.FAILED): _S.FAILED,
    (_S.REPORTED, _E.FAILED): _S.FAILED,
    (_S.RUNNING, _E.PARENT_GONE): _S.ABANDONED,
    (_S.REPORTED, _E.PARENT_GONE): _S.ABANDONED,
}


@dataclass(frozen=True)
class Transition:
    state: HelperState
    completed: bool  # stamp helper_completed_at (True) or clear it (False)


def next_state(current: str | None, event: str) -> Transition | None:
    """The state ``event`` moves a helper in ``current`` to, or None to ignore it.

    None covers non-helpers (no state), unknown values and events that do not
    apply in the current state, such as a second report or a done helper
    losing its parent.
    """
    try:
        target = TRANSITIONS.get((HelperState(current or ""), HelperEvent(event)))
    except ValueError:
        return None
    if target is None:
        return None
    return Transition(target, target in FINISHED)


def initial_state(role: str) -> HelperState | None:
    return HelperState.RUNNING if role == Role.HELPER else None


def parse_role(raw: object) -> Role | None:
    """A creatable role from a request value; '' and None mean ``agent``."""
    value = str(raw or "").strip().lower() or Role.AGENT
    try:
        role = Role(value)
    except ValueError:
        return None
    return role if role in CREATABLE_ROLES else None


def parent_refusal(child_id: str, parent_id: str,
                   parent_ancestors: Sequence[str]) -> str:
    """Why ``parent_id`` cannot parent ``child_id``, or '' when it can.

    ``parent_ancestors`` is the parent's own chain upward (its parent first).
    A brand-new agent has no id yet and passes ``child_id=''``.
    """
    if not parent_id:
        return ""
    if child_id and parent_id == child_id:
        return "self_parent"
    if child_id and child_id in parent_ancestors:
        return "parent_cycle"
    return ""


def archive_due(rows: Iterable[Mapping], *, now_ms: int,
                grace_ms: int = DEFAULT_ARCHIVE_GRACE_MS) -> list[str]:
    """Agent ids of ``done`` helpers finished longer than ``grace_ms`` ago
    and not yet archived."""
    cutoff = now_ms - max(0, int(grace_ms))
    return [str(row["agent_id"]) for row in rows
            if row.get("role") == Role.HELPER
            and row.get("helper_state") == HelperState.DONE
            and row.get("archived_at") is None
            and int(row.get("helper_completed_at") or 0) <= cutoff]
