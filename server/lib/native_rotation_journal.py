"""Durable, per-agent rotation intent; no native effect is retried here.

Pending effect phases survive process loss and keep admission closed. Only the
controller's correlated result or explicit reconciliation may finish them.
Canonical chat/task carries have their own transactions and remain separate
phases; the native binding and this journal's bound phase commit together.
"""
from __future__ import annotations

import json
import re
import hashlib
import secrets
from contextlib import contextmanager

from . import agents, db, settings_store

_PREFIX = "codex.rotation.v1.owner."
_MAX_BYTES = 32_768
_PHASES = ("reserved", "quiescing", "quiescent", "create_pending", "created",
           "final_imported", "chats_carried", "plans_carried", "bound",
           "archive_pending", "archived", "goal_pending", "ready", "complete")
_FIELDS = frozenset({"version", "agent_id", "operation_id", "old_thread_id",
                     "runtime_id", "phase", "revision", "created_at", "new_thread_id", "generation"})
_RESOURCE_PREFIX = "codex.rotation.v1.thread."
_RESOURCE_FIELDS = frozenset({"version", "backend", "native_id", "agent_id", "operation_id",
                              "runtime_id", "generation", "role"})
_MAX_OPERATIONS = 4
_PERMIT_KEY = object()


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
    if not isinstance(record.get("generation"), str) or re.fullmatch(r"[0-9a-f]{32}", record["generation"]) is None:
        raise RotationConflict("invalid rotation generation")
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


@contextmanager
def writer():
    """Serialize the guard and mutation; preserve an enclosing caller unit.

    No native call or TURN acquisition belongs in this scope. A joined
    savepoint also prevents a caught late mutation failure from leaving a
    half-written unit for the outer owner to commit.
    """
    con = db.conn()
    joined = con.in_transaction
    con.execute("SAVEPOINT native_rotation_writer" if joined else "BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("RELEASE native_rotation_writer" if joined else "COMMIT")
    except BaseException as primary:
        try:
            if joined:
                con.execute("ROLLBACK TO native_rotation_writer")
                con.execute("RELEASE native_rotation_writer")
            elif con.in_transaction:
                con.execute("ROLLBACK")
        except BaseException as cleanup:
            primary.add_note(f"rotation writer cleanup failed: {type(cleanup).__name__}")
        raise


def _resource_key(native_id):
    return _RESOURCE_PREFIX + hashlib.sha256(_identity(native_id).encode("utf-8")).hexdigest()


def _decode_resource(raw, key):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4096:
        raise RotationConflict("invalid native reservation")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RotationConflict("unreadable native reservation") from exc
    if not isinstance(value, dict) or set(value) != _RESOURCE_FIELDS:
        raise RotationConflict("invalid native reservation fields")
    if type(value["version"]) is not int or value["version"] != 1 or value["backend"] != "codex":
        raise RotationConflict("unsupported native reservation")
    for name in ("native_id", "agent_id"):
        _identity(value[name])
    _operation(value["operation_id"])
    if (not isinstance(value["role"], str) or value["role"] not in {"old", "new"} or
            type(value["runtime_id"]) is not int or not 0 < value["runtime_id"] < (1 << 63) or
            not isinstance(value["generation"], str) or re.fullmatch(r"[0-9a-f]{32}", value["generation"]) is None or
            _resource_key(value["native_id"]) != key):
        raise RotationConflict("native reservation identity mismatch")
    return value


def _resource(native_id):
    key = _resource_key(native_id)
    raw = settings_store.get(key)
    return None if raw is None else _decode_resource(raw, key)


def _resource_value(record, native_id, role):
    return {"version": 1, "backend": "codex", "native_id": native_id,
            "agent_id": record["agent_id"], "operation_id": record["operation_id"],
            "runtime_id": record["runtime_id"], "generation": record["generation"], "role": role}


def _claim_resource(record, native_id, role):
    if not db.conn().in_transaction:
        raise RotationConflict("native reservation needs the owning writer")
    wanted = _resource_value(record, native_id, role)
    existing = _resource(native_id)
    if existing is not None:
        if existing != wanted:
            raise RotationConflict("native identity belongs to another operation")
        return
    owner = agents.get_by_backend_session(native_id)
    if owner and (role == "new" or owner["agent_id"] != record["agent_id"]):
        raise RotationConflict("native identity already has a canonical owner")
    # At most four simultaneous rotations, two immutable resources apiece.
    # Decode even orphan records; malformed ownership never frees capacity.
    entries = settings_store.items_in_range(
        _RESOURCE_PREFIX, _RESOURCE_PREFIX[:-1] + chr(ord(_RESOURCE_PREFIX[-1]) + 1),
        limit=2 * _MAX_OPERATIONS + 1)
    if len(entries) >= 2 * _MAX_OPERATIONS:
        raise RotationConflict("native reservation capacity exhausted")
    groups = set()
    for key, raw in entries:
        item = _decode_resource(raw, key)
        groups.add((item["agent_id"], item["operation_id"], item["generation"]))
    if role == "old" and len(groups) >= _MAX_OPERATIONS:
        raise RotationConflict("rotation operation capacity exhausted")
    settings_store.set_text(_resource_key(native_id), json.dumps(wanted, sort_keys=True, separators=(",", ":")))


def _release_resources(record):
    for role, name in (("old", "old_thread_id"), ("new", "new_thread_id")):
        native_id = record[name]
        if _resource(native_id) != _resource_value(record, native_id, role):
            raise RotationConflict("native reservation changed before completion")
        settings_store.delete(_resource_key(native_id))


def _resources_current(record):
    for role, name in (("old", "old_thread_id"), ("new", "new_thread_id")):
        if name not in record:
            continue
        native_id = record[name]
        if _resource(native_id) != _resource_value(record, native_id, role):
            raise RotationConflict("rotation native reservation is missing or changed")
        owner = agents.get_by_backend_session(native_id)
        if owner is not None and owner["agent_id"] != record["agent_id"]:
            raise RotationConflict("a different canonical agent adopted the reserved native identity")


class _BindingPermit:
    """Private, transaction-bound authority; never minted from request fields."""
    def __init__(self, record):
        self.key = _PERMIT_KEY
        self.record = dict(record)
        self.connection = db.conn()
        self.used = False


def guard_mutation(agent_id, *, native_id=None, permit=None):
    if not db.conn().in_transaction:
        raise RotationConflict("canonical mutation needs its owning writer")
    current = get(agent_id)
    active = current is not None and current["phase"] != "complete"
    resource = _resource(native_id) if native_id else None
    if permit is not None:
        if (type(permit) is not _BindingPermit or permit.key is not _PERMIT_KEY or permit.used or
                permit.connection is not db.conn() or not active or current != permit.record or
                current["phase"] != "plans_carried" or native_id != current["new_thread_id"] or
                resource != _resource_value(current, native_id, "new") or
                _resource(current["old_thread_id"]) != _resource_value(current, current["old_thread_id"], "old")):
            raise RotationConflict("rotation binding authority is stale or foreign")
        _current(current)
        permit.used = True
        return
    if active or resource is not None:
        raise RotationConflict("conversation rotation owns this agent or native identity")


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
    with writer():
        current = get(agent_id)
        if current is not None:
            if (current["operation_id"] == operation_id and
                    current["old_thread_id"] == expected_old_thread_id):
                _current(current)
                if current["phase"] != "complete":
                    _resources_current(current)
                return current
            if current["phase"] != "complete" or current["operation_id"] == operation_id:
                raise RotationConflict("another rotation owns this agent")
        record = {"version": 1, "agent_id": agent_id, "operation_id": operation_id,
                  "old_thread_id": expected_old_thread_id,
                  "runtime_id": agents.current_runtime_id(agent_id), "phase": "reserved",
                  "revision": 1, "created_at": db.now_ms(), "generation": secrets.token_hex(16)}
        _validate(record, agent_id)
        _current(record)
        _claim_resource(record, expected_old_thread_id, "old")
        return _write(record)


def _owned(agent_id, operation_id, expected_phase):
    record = get(agent_id)
    if (record is None or record["operation_id"] != operation_id or
            record["phase"] != expected_phase):
        raise RotationConflict("rotation intent changed")
    _current(record)
    _resources_current(record)
    return record


def advance(agent_id, operation_id, expected_phase, next_phase, *, new_thread_id=None):
    if (expected_phase not in _PHASES or next_phase not in _PHASES or
            _PHASES.index(next_phase) != _PHASES.index(expected_phase) + 1 or
            next_phase == "bound"):
        raise RotationConflict("invalid rotation phase transition")
    with writer():
        current = _owned(agent_id, operation_id, expected_phase)
        extra = {}
        if next_phase == "created":
            extra["new_thread_id"] = _identity(new_thread_id)
            if extra["new_thread_id"] == current["old_thread_id"]:
                raise RotationConflict("rotation did not create a new identity")
            _claim_resource(current, extra["new_thread_id"], "new")
        elif new_thread_id is not None:
            raise RotationConflict("native identity is immutable after create")
        if next_phase == "complete":
            _release_resources(current)
        return _write({**current, **extra, "phase": next_phase,
                       "revision": current["revision"] + 1})


def bind(agent_id, operation_id, *, goal_transfer=None, new_goal=None, incarnation=None):
    with writer():
        current = get(agent_id)
        if current and current["operation_id"] == operation_id and current["phase"] == "bound":
            _current(current)
            _resources_current(current)
            return current
        current = _owned(agent_id, operation_id, "plans_carried")
        agents.bind_backend_session(agent_id, current["new_thread_id"],
                                    _rotation_permit=_BindingPermit(current))
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
