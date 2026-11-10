"""Pure accounting for native epochs; persistence and binding CAS belong to owner.

Native counters cannot be assigned. A new epoch receives only the remaining
budget; the logical mirror retains cumulative usage and the original budget.
"""
from __future__ import annotations

import math

_MAX = (1 << 63) - 1
_STATUSES = frozenset({"active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"})


class AccountingUnavailable(ValueError):
    pass


def _integer(value):
    if type(value) is not int or not 0 <= value <= _MAX:
        raise AccountingUnavailable("invalid native counter")
    return value


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= _MAX:
        raise AccountingUnavailable("invalid native elapsed time")
    return value


def _identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise AccountingUnavailable("invalid native identity")
    return value


def _goal(thread, raw):
    if not isinstance(raw, dict) or raw.get("threadId") != thread:
        raise AccountingUnavailable("native thread mismatch")
    if not isinstance(raw.get("objective"), str) or not raw["objective"].strip():
        raise AccountingUnavailable("missing native objective")
    if raw.get("status") not in _STATUSES:
        raise AccountingUnavailable("unknown native status")
    _integer(raw.get("createdAt"))
    _integer(raw.get("updatedAt"))
    _integer(raw.get("tokensUsed"))
    _seconds(raw.get("timeUsedSeconds"))
    if "tokenBudget" not in raw:
        raise AccountingUnavailable("missing native budget")
    if raw["tokenBudget"] is not None:
        _integer(raw["tokenBudget"])


def plan_transfer(agent_id, old_thread_id, rawgoal, *, prior=None):
    _identity(agent_id)
    _identity(old_thread_id)
    _goal(old_thread_id, rawgoal)
    logical = project(agent_id, old_thread_id, rawgoal, prior) if prior is not None else rawgoal
    budget, used = logical["tokenBudget"], logical["tokensUsed"]
    remaining = None if budget is None else max(0, budget - used)
    return {"version": 1, "agent_id": agent_id, "old_thread_id": old_thread_id,
            "objective": logical["objective"], "logical_budget": budget,
            "tokens_base": used, "time_base": logical["timeUsedSeconds"],
            "logical_created_at": logical["createdAt"], "native_budget": remaining,
            "restore_status": "budgetLimited" if remaining == 0 else logical["status"],
            "defer_native_goal": remaining == 0}


def bind_transfer(transfer, new_thread_id, raw_new_goal):
    _identity(new_thread_id)
    _goal(new_thread_id, raw_new_goal)
    if (transfer["defer_native_goal"] or transfer["old_thread_id"] == new_thread_id or
            raw_new_goal["tokenBudget"] != transfer["native_budget"] or
            raw_new_goal["objective"] != transfer["objective"] or
            raw_new_goal["tokensUsed"] != 0 or raw_new_goal["timeUsedSeconds"] != 0 or
            raw_new_goal["status"] != "paused"):
        raise AccountingUnavailable("new epoch is not an unused paused goal")
    return {**transfer, "new_thread_id": new_thread_id,
            "native_created_at": raw_new_goal["createdAt"],
            "last_native_tokens": 0, "last_native_seconds": 0}


def project(agent_id, thread_id, observed, offset):
    _goal(thread_id, observed)
    if (offset.get("version") != 1 or offset.get("agent_id") != agent_id or
            offset.get("new_thread_id") != thread_id or
            offset.get("native_created_at") != observed["createdAt"] or
            offset.get("objective") != observed["objective"] or
            offset.get("native_budget") != observed["tokenBudget"]):
        raise AccountingUnavailable("native goal epoch mismatch")
    tokens, seconds = observed["tokensUsed"], observed["timeUsedSeconds"]
    if (tokens < _integer(offset.get("last_native_tokens")) or
            seconds < _seconds(offset.get("last_native_seconds"))):
        raise AccountingUnavailable("native accounting reset")
    total_tokens = _integer(offset.get("tokens_base")) + tokens
    total_seconds = _seconds(offset.get("time_base")) + seconds
    _integer(total_tokens)
    _seconds(total_seconds)
    return {**observed, "tokensUsed": total_tokens, "timeUsedSeconds": total_seconds,
            "tokenBudget": offset["logical_budget"],
            "createdAt": offset["logical_created_at"]}
