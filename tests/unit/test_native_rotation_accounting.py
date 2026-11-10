"""Fresh native counters never reset the logical goal's lifetime budget."""
from __future__ import annotations

import pytest

from lib import native_rotation_accounting as accounting


def _goal(**changes):
    return {"threadId": "old", "objective": "Finish the requested work", "status": "active",
            "tokenBudget": 1000, "tokensUsed": 400, "timeUsedSeconds": 25,
            "createdAt": 100, "updatedAt": 200, **changes}


def _new(**changes):
    values = dict(threadId="new", status="paused", tokenBudget=600, tokensUsed=0,
                  timeUsedSeconds=0, createdAt=300, updatedAt=300)
    values.update(changes)
    return _goal(**values)


def test_remaining_native_budget_and_cumulative_projection_do_not_double_count():
    transfer = accounting.plan_transfer("agent", "old", _goal())
    assert transfer["native_budget"] == 600
    assert transfer["logical_budget"] == 1000
    assert transfer["tokens_base"] == 400 and transfer["time_base"] == 25
    offset = accounting.bind_transfer(transfer, "new", _new())
    observed = _new(tokensUsed=30, timeUsedSeconds=4, status="active", updatedAt=310)
    first = accounting.project("agent", "new", observed, offset)
    second = accounting.project("agent", "new", observed, offset)
    assert first == second
    assert first["tokenBudget"] == 1000 and first["tokensUsed"] == 430
    assert first["timeUsedSeconds"] == 29 and first["createdAt"] == 100
    # A second rotation carries the cumulative total, not the base twice.
    again = accounting.plan_transfer("agent", "new", observed, prior=offset)
    assert again["tokens_base"] == 430 and again["time_base"] == 29
    assert again["logical_budget"] == 1000 and again["native_budget"] == 570


@pytest.mark.parametrize("budget,used", [(0, 0), (400, 400), (300, 400)])
def test_exhausted_budget_never_becomes_unlimited_one_or_active(budget, used):
    transfer = accounting.plan_transfer("agent", "old", _goal(tokenBudget=budget, tokensUsed=used))
    assert transfer["native_budget"] == 0
    assert transfer["defer_native_goal"] is True
    assert transfer["restore_status"] == "budgetLimited"
    with pytest.raises(accounting.AccountingUnavailable):
        accounting.bind_transfer(transfer, "new", _new(tokenBudget=None))


@pytest.mark.parametrize("status", ["paused", "blocked", "usageLimited", "budgetLimited", "complete"])
def test_rotation_does_not_resume_nonrunning_goal(status):
    transfer = accounting.plan_transfer("agent", "old", _goal(status=status))
    assert transfer["restore_status"] == status
    assert transfer["native_budget"] == 600


def test_unlimited_remains_unlimited_without_losing_usage():
    transfer = accounting.plan_transfer("agent", "old", _goal(tokenBudget=None))
    assert transfer["native_budget"] is None and transfer["logical_budget"] is None
    offset = accounting.bind_transfer(transfer, "new", _new(tokenBudget=None))
    result = accounting.project("agent", "new", _new(tokenBudget=None, tokensUsed=10), offset)
    assert result["tokenBudget"] is None and result["tokensUsed"] == 410


@pytest.mark.parametrize("damage", ["agent", "thread", "epoch", "reset", "bool", "missing"])
def test_unprovable_or_reset_native_accounting_is_protected(damage):
    offset = accounting.bind_transfer(accounting.plan_transfer("agent", "old", _goal()), "new", _new())
    observed = _new(tokensUsed=30, timeUsedSeconds=4)
    offset = {**offset, "last_native_tokens": 20, "last_native_seconds": 3}
    agent, thread = "agent", "new"
    if damage == "agent":
        agent = "other"
    elif damage == "thread":
        thread = "old"
    elif damage == "epoch":
        observed["createdAt"] = 301
    elif damage == "reset":
        observed["tokensUsed"] = 0
    elif damage == "bool":
        observed["tokensUsed"] = True
    elif damage == "missing":
        del observed["timeUsedSeconds"]
    with pytest.raises(accounting.AccountingUnavailable):
        accounting.project(agent, thread, observed, offset)
