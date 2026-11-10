"""Durable, per-agent rotation intent; no native effect is retried here.

Pending effect phases survive process loss and keep admission closed. Only the
controller's correlated result or explicit reconciliation may finish them.
Canonical chat/task carries have their own transactions and remain separate
phases; the native binding and this journal's bound phase commit together.
"""
from __future__ import annotations

import json
import re

from . import agents, db, settings_store

_PREFIX = "codex.rotation.v1.owner."
_MAX_BYTES = 32_768
_PHASES = ("reserved", "quiescing", "quiescent", "create_pending", "created",
           "final_imported", "chats_carried", "plans_carried", "bound",
           "archive_pending", "archived", "goal_pending", "ready", "complete")
_FIELDS = frozenset({"version", "agent_id", "operation_id", "old_thread_id",
                     "runtime_id", "phase", "revision", "created_at", "new_thread_id"})


class RotationConflict(RuntimeError):
    pass


def _identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\0" in value:
        raise RotationConflict("invalid rotation identity")
    return value


def _operation(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
        raise RotationConflict("invalid durable rotation operation ID")
    return value


def key(agent_id):
    return _PREFIX + _identity(agent_id)


def _validate(record, agent_id):
    if not isinstance(record, dict) or set(record) - _FIELDS:
        raise RotationConflict("invalid rotation record")
    if type(record.get("version")) is not int or record["version"] != 1:
        raise RotationConflict("unsupported rotation record")
    _operation(record.get("operation_id"))
    for name in ("agent_id", "old_thread_id"):
        _identity(record.get(name))
    if record["agent_id"] != agent_id or record.get("phase") not in _PHASES:
        raise RotationConflict("rotation record identity or phase mismatch")
    for name in ("runtime_id", "revision", "created_at"):
        if type(record.get(name)) is not int or not 0 < record[name] < (1 << 63):
            raise RotationConflict("invalid rotation record counter")
    needs_new = _PHASES.index(record["phase"]) >= _PHASES.index("created")
    if "new_thread_id" in record:
        _identity(record["new_thread_id"])
        if record["new_thread_id"] == record["old_thread_id"]:
            raise RotationConflict("rotation did not create a new identity")
    if needs_new != ("new_thread_id" in record):
        raise RotationConflict("rotation native result missing or premature")
    return record


def get(agent_id):
    raw = settings_store.get(key(agent_id))
    if raw is None:
        return None
    if len(raw.encode("utf-8")) > _MAX_BYTES:
        raise RotationConflict("rotation record exceeds bound")
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RotationConflict("unreadable rotation record") from exc
    return _validate(record, agent_id)


def blocked(agent_id):
    record = get(agent_id)
    return record is not None and record["phase"] != "complete"


def _write(record):
    _validate(record, record["agent_id"])
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"))
    if len(raw.encode("utf-8")) > _MAX_BYTES:
        raise RotationConflict("rotation record exceeds bound")
    settings_store.set_text(key(record["agent_id"]), raw)
    return record


def _current(record):
    agent = agents.get_by_agent_id(record["agent_id"])
    expected = (record["new_thread_id"] if _PHASES.index(record["phase"]) >= _PHASES.index("bound")
                else record["old_thread_id"])
    if (not agent or agent.get("backend") != "codex" or
            agents.current_runtime_id(record["agent_id"]) != record["runtime_id"] or
            agents.live_backend_session(record["agent_id"]) != expected):
        raise RotationConflict("rotation owner or native binding changed")


def reserve(agent_id, expected_old_thread_id, operation_id):
    _identity(expected_old_thread_id)
    _operation(operation_id)
    with settings_store.transaction():
        current = get(agent_id)
        if current is not None:
            if (current["operation_id"] == operation_id and
                    current["old_thread_id"] == expected_old_thread_id):
                _current(current)
                return current
            if current["phase"] != "complete" or current["operation_id"] == operation_id:
                raise RotationConflict("another rotation owns this agent")
        record = {"version": 1, "agent_id": agent_id, "operation_id": operation_id,
                  "old_thread_id": expected_old_thread_id,
                  "runtime_id": agents.current_runtime_id(agent_id), "phase": "reserved",
                  "revision": 1, "created_at": db.now_ms()}
        _validate(record, agent_id)
        _current(record)
        return _write(record)


def _owned(agent_id, operation_id, expected_phase):
    record = get(agent_id)
    if (record is None or record["operation_id"] != operation_id or
            record["phase"] != expected_phase):
        raise RotationConflict("rotation intent changed")
    _current(record)
    return record


def advance(agent_id, operation_id, expected_phase, next_phase, *, new_thread_id=None):
    if (expected_phase not in _PHASES or next_phase not in _PHASES or
            _PHASES.index(next_phase) != _PHASES.index(expected_phase) + 1 or
            next_phase == "bound"):
        raise RotationConflict("invalid rotation phase transition")
    with settings_store.transaction():
        current = _owned(agent_id, operation_id, expected_phase)
        extra = {}
        if next_phase == "created":
            extra["new_thread_id"] = _identity(new_thread_id)
        elif new_thread_id is not None:
            raise RotationConflict("native identity is immutable after create")
        return _write({**current, **extra, "phase": next_phase,
                       "revision": current["revision"] + 1})


def bind(agent_id, operation_id, *, goal_transfer=None, new_goal=None, incarnation=None):
    with settings_store.transaction():
        current = get(agent_id)
        if current and current["operation_id"] == operation_id and current["phase"] == "bound":
            _current(current)
            return current
        current = _owned(agent_id, operation_id, "plans_carried")
        agents.bind_backend_session(agent_id, current["new_thread_id"])
        if goal_transfer is not None:
            from . import agent_goals, native_rotation_goals
            if goal_transfer.get("old_thread_id") != current["old_thread_id"]:
                raise RotationConflict("carried goal source mismatch")
            logical = native_rotation_goals.install(
                agent_id, operation_id, current["new_thread_id"], goal_transfer, new_goal, incarnation)
            agent = agents.get_by_agent_id(agent_id)
            agent_goals.upsert(agent_id, session=agent["session"], backend="codex",
                               goal=agent_goals.from_codex(logical))
        elif new_goal is not None or incarnation is not None:
            raise RotationConflict("native goal response has no carried plan")
        return _write({**current, "phase": "bound", "revision": current["revision"] + 1})
