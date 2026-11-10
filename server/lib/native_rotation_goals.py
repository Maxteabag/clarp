"""Persisted accounting for one correlated, fresh-thread native goal.

The controller installs this only from its first paused creation response.
Whole-second provider timestamps reject mismatches; they do NOT prove epoch
identity. The acknowledged creation and uninterrupted transport incarnation do.
Loss of that continuity protects the logical goal instead of resetting usage.
All writes here join the canonical goal writer's transaction.
"""
from __future__ import annotations

import json

from . import native_rotation_accounting as accounting, settings_store

_PREFIX = "codex.rotation.v1.goal."
_MAX_BYTES = 32_768
_FIELDS = frozenset({"version", "agent_id", "operation_id", "incarnation", "state", "offset", "reason", "last_status"})
_OFFSET_FIELDS = frozenset({"version", "agent_id", "old_thread_id", "objective", "logical_budget",
                            "tokens_base", "time_base", "logical_created_at", "native_budget",
                            "restore_status", "defer_native_goal", "new_thread_id", "native_created_at",
                            "last_native_tokens", "last_native_seconds", "logical_updated_at", "source_native_created_at"})


def _identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\0" in value:
        raise accounting.AccountingUnavailable("invalid carried goal identity")
    return value


def key(agent_id):
    return _PREFIX + _identity(agent_id)


def _validate(record, agent_id):
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise accounting.AccountingUnavailable("invalid carried goal record")
    if type(record["version"]) is not int or record["version"] != 1 or record["agent_id"] != agent_id:
        raise accounting.AccountingUnavailable("carried goal owner mismatch")
    for field in ("agent_id", "operation_id", "incarnation"):
        _identity(record[field])
    if not isinstance(record["state"], str) or record["state"] not in {"active", "protected", "retired"}:
        raise accounting.AccountingUnavailable("invalid carried goal state")
    if not isinstance(record["reason"], str) or record["reason"] not in {"", "cleared", "new-goal", "continuity", "observation"}:
        raise accounting.AccountingUnavailable("invalid carried goal reason")
    if not isinstance(record["last_status"], str) or record["last_status"] not in accounting._STATUSES:
        raise accounting.AccountingUnavailable("invalid last native goal status")
    offset = record["offset"]
    if not isinstance(offset, dict) or set(offset) != _OFFSET_FIELDS or offset.get("agent_id") != agent_id:
        raise accounting.AccountingUnavailable("invalid carried goal offset")
    if type(offset["version"]) is not int or offset["version"] != 1 or type(offset["defer_native_goal"]) is not bool:
        raise accounting.AccountingUnavailable("unsupported carried goal offset")
    for field in ("old_thread_id", "new_thread_id"):
        _identity(offset[field])
    if offset["old_thread_id"] == offset["new_thread_id"]:
        raise accounting.AccountingUnavailable("carried goal source equals destination")
    if not isinstance(offset["restore_status"], str) or offset["restore_status"] not in accounting._STATUSES:
        raise accounting.AccountingUnavailable("invalid carried goal status")
    if not isinstance(offset["objective"], str) or len(offset["objective"].encode("utf-8")) > 16_384:
        raise accounting.AccountingUnavailable("carried objective exceeds bound")
    for field in ("logical_created_at", "logical_updated_at", "source_native_created_at", "tokens_base", "last_native_tokens"):
        accounting._integer(offset[field])
    for field in ("time_base", "last_native_seconds"):
        accounting._seconds(offset[field])
    if offset["logical_budget"] is not None:
        accounting._integer(offset["logical_budget"])
    if offset["defer_native_goal"]:
        if (offset["native_budget"] != 0 or offset["native_created_at"] is not None or
                offset["logical_budget"] is None or offset["tokens_base"] < offset["logical_budget"] or
                offset["last_native_tokens"] != 0 or offset["last_native_seconds"] != 0):
            raise accounting.AccountingUnavailable("invalid exhausted-goal deferral")
    else:
        accounting._integer(offset["native_created_at"])
        if offset["native_budget"] is not None and accounting._integer(offset["native_budget"]) == 0:
            raise accounting.AccountingUnavailable("exhausted goal must remain deferred")
    return record


def get(agent_id):
    raw = settings_store.get(key(agent_id))
    if raw is None:
        return None
    if len(raw.encode("utf-8")) > _MAX_BYTES:
        raise accounting.AccountingUnavailable("carried goal record exceeds bound")
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise accounting.AccountingUnavailable("unreadable carried goal record") from exc
    return _validate(record, agent_id)


def store(record):
    _validate(record, record["agent_id"])
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"))
    if len(raw.encode("utf-8")) > _MAX_BYTES:
        raise accounting.AccountingUnavailable("carried goal record exceeds bound")
    settings_store.set_text(key(record["agent_id"]), raw)


def install(agent_id, operation_id, new_thread_id, transfer, raw_new_goal, incarnation):
    """Join binding+mirror commit; caller owns correlated creation authority."""
    _identity(incarnation)
    if transfer.get("agent_id") != agent_id:
        raise accounting.AccountingUnavailable("carried goal plan owner mismatch")
    if transfer["defer_native_goal"]:
        if raw_new_goal is not None:
            raise accounting.AccountingUnavailable("exhausted goal must not create a native allowance")
        _identity(new_thread_id)
        offset = {**transfer, "new_thread_id": new_thread_id, "native_created_at": None,
                  "last_native_tokens": 0, "last_native_seconds": 0}
    else:
        offset = accounting.bind_transfer(transfer, new_thread_id, raw_new_goal)
    record = {"version": 1, "agent_id": agent_id, "operation_id": operation_id,
              "incarnation": incarnation, "state": "active", "reason": "",
              "last_status": "budgetLimited" if transfer["defer_native_goal"] else "paused",
              "offset": offset}
    store(record)
    return (_deferred_goal(record) if transfer["defer_native_goal"] else
            accounting.project(agent_id, new_thread_id, raw_new_goal, record["offset"]))


def require_epoch(agent_id, thread_id, incarnation):
    record = get(agent_id)
    if record is None or record["state"] == "retired":
        return
    if (record["state"] != "active" or record["incarnation"] != incarnation or
            record["offset"]["new_thread_id"] != thread_id):
        raise accounting.AccountingUnavailable("carried goal continuity is protected")


def require_activation(agent_id, thread_id, incarnation):
    require_epoch(agent_id, thread_id, incarnation)
    record = get(agent_id)
    if record is not None and record["state"] != "retired":
        if record["offset"]["defer_native_goal"] or record["last_status"] == "complete":
            raise accounting.AccountingUnavailable("clear the exhausted or completed carried goal before starting anew")


def _deferred_goal(record):
    offset = record["offset"]
    return {"threadId": offset["new_thread_id"], "objective": offset["objective"],
            "status": "budgetLimited", "tokenBudget": offset["logical_budget"],
            "tokensUsed": offset["tokens_base"], "timeUsedSeconds": offset["time_base"],
            "createdAt": offset["logical_created_at"], "updatedAt": offset["logical_updated_at"]}


def observation(agent_id, thread_id, raw_goal, incarnation, *, user_action=None):
    """Commit protected state before reporting a failed continuity proof.

    Only correlated explicit user start/clear results retire a protected old
    budget. Mere absence cannot erase an intentionally deferred logical goal.
    """
    record = get(agent_id)
    if record is None:
        return raw_goal, None, None
    if user_action in ("start", "clear"):
        if record["offset"]["new_thread_id"] != thread_id:
            raise accounting.AccountingUnavailable("explicit goal response has a stale native binding")
        if user_action == "start":
            accounting._goal(thread_id, raw_goal)
        elif raw_goal is not None:
            raise accounting.AccountingUnavailable("clear response is not empty")
        reason = "new-goal" if user_action == "start" else "cleared"
        return raw_goal, {**record, "state": "retired", "reason": reason}, None
    if user_action is not None:
        raise accounting.AccountingUnavailable("unsupported explicit goal authority")
    if record["state"] == "retired":
        if record["reason"] == "new-goal" or raw_goal is None:
            return raw_goal, None, None
        error = accounting.AccountingUnavailable("an unattributed old goal cannot undo a clear")
        return None, {**record, "state": "protected", "reason": "observation"}, error
    try:
        require_epoch(agent_id, thread_id, incarnation)
    except accounting.AccountingUnavailable as exc:
        return None, {**record, "state": "protected", "reason": "continuity"}, exc
    if record["offset"]["defer_native_goal"]:
        if raw_goal is None:
            return _deferred_goal(record), None, None
        error = accounting.AccountingUnavailable("deferred goal received an unowned native epoch")
        return None, {**record, "state": "protected", "reason": "observation"}, error
    if not raw_goal:
        return None, {**record, "state": "retired", "reason": "cleared"}, None
    if record["last_status"] == "complete" and raw_goal.get("status") != "complete":
        error = accounting.AccountingUnavailable("completed epoch cannot authorize a new goal")
        return None, {**record, "state": "protected", "reason": "observation"}, error
    try:
        projected = accounting.project(agent_id, thread_id, raw_goal, record["offset"])
    except accounting.AccountingUnavailable as exc:
        return None, {**record, "state": "protected", "reason": "observation"}, exc
    offset = {**record["offset"], "last_native_tokens": raw_goal["tokensUsed"],
              "last_native_seconds": raw_goal["timeUsedSeconds"]}
    return projected, {**record, "offset": offset, "last_status": raw_goal["status"]}, None
