"""Short rotation bookkeeping under the dispatcher's ownership boundary.

Counts are lifetimes, not held locks: SQLite, callbacks and provider waits run
outside TURN. A durable pending journal also fences a newly started runtime.
The handoff owner must retain selected tokens across deferred publication.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field

from . import native_rotation_journal as journal
from .turn_dispatch_state import _SLOTS, _TURN_LOCK


@dataclass
class _Owner:
    operation: str | None = None
    admissions: int = 0
    selected: set[object] = field(default_factory=set)
    override: int = 0


_owners: dict[str, _Owner] = {}
_MAX_OWNERS = 8192


def _cell(agent_id):
    owner = _owners.get(agent_id)
    if owner is None:
        if len(_owners) >= _MAX_OWNERS:
            raise journal.RotationConflict("rotation owner capacity exhausted")
        owner = _owners[agent_id] = _Owner()
    return owner


def _forget_empty(agent_id, owner):
    if owner.operation is None and owner.admissions == 0 and not owner.selected:
        if _owners.get(agent_id) is owner:
            del _owners[agent_id]


def _refused():
    from .turn_command import DispatchError
    return DispatchError(503, "Conversation rotation is protected; the message was not admitted")


@contextmanager
def admitting(agent_id):
    """Count the whole normal/recovery/native admission, including cleanup."""
    if not agent_id:
        yield
        return
    with _TURN_LOCK:
        owner = _cell(agent_id)
        if owner.operation is not None:
            raise _refused()
        owner.admissions += 1
    try:
        try:
            pending = journal.blocked(agent_id)
        except journal.RotationConflict as exc:
            raise _refused() from exc
        if pending:
            raise _refused()
        yield
    finally:
        with _TURN_LOCK:
            owner.admissions -= 1
            _forget_empty(agent_id, owner)


def begin_selected(agent_id):
    """Under TURN, retain a selected owner before publishing its callback."""
    with _TURN_LOCK:
        owner = _cell(agent_id)
        if owner.operation is not None:
            return None
        token = object()
        owner.selected.add(token)
        return token


def finish_selected(agent_id, token):
    with _TURN_LOCK:
        owner = _owners.get(agent_id)
        if owner is None or token not in owner.selected:
            raise journal.RotationConflict("selected owner token is not current")
        owner.selected.remove(token)
        _forget_empty(agent_id, owner)


def closed(agent_id):
    with _TURN_LOCK:
        owner = _owners.get(agent_id)
        return owner is not None and owner.operation is not None


def reserve(agent_id, expected_old_thread_id, operation_id):
    """Close only an idle dispatcher; native quiescence is a later phase.

    This is not usable as complete rotation until every selected/deferred
    handoff publishes its token at the owner seam.
    """
    with _TURN_LOCK:
        owner = _cell(agent_id)
        if owner.operation is not None and owner.operation != operation_id:
            raise journal.RotationConflict("another rotation holds admission")
        if owner.operation is None:
            if (owner.admissions or owner.selected or _SLOTS.get(agent_id) or
                    _SLOTS.queue_depth(agent_id) or _SLOTS.launch_running(agent_id)):
                _forget_empty(agent_id, owner)
                raise journal.RotationConflict("agent has admitted or selected work")
            owner.operation = operation_id
    try:
        return journal.reserve(agent_id, expected_old_thread_id, operation_id)
    except BaseException:
        # Only proved absence releases a failed durable reservation. Read
        # errors and any partial/uncertain record keep admission closed.
        try:
            absent = journal.get(agent_id) is None
        except Exception:
            absent = False
        if absent:
            with _TURN_LOCK:
                if _owners.get(agent_id) is owner and owner.operation == operation_id:
                    owner.operation = None
                    _forget_empty(agent_id, owner)
        raise


def release_completed(agent_id, operation_id):
    record = journal.get(agent_id)
    if record is None or record["operation_id"] != operation_id or record["phase"] != "complete":
        raise journal.RotationConflict("rotation has not durably completed")
    with _TURN_LOCK:
        owner = _owners.get(agent_id)
        if owner is not None:
            if owner.operation != operation_id:
                raise journal.RotationConflict("a newer rotation owns admission")
            owner.operation = None
            _forget_empty(agent_id, owner)


def note_override(agent_id):
    """Nonblocking user-intent bookkeeping; never wait for a native answer."""
    with _TURN_LOCK:
        owner = _owners.get(agent_id)
        if owner is not None and owner.operation is not None:
            owner.override += 1
            return owner.operation, owner.override
    return None


def override_generation(agent_id, operation_id):
    with _TURN_LOCK:
        owner = _owners.get(agent_id)
        if owner is None or owner.operation != operation_id:
            raise journal.RotationConflict("rotation admission owner disappeared")
        return owner.override
