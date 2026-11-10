"""Carried logical counters remain bound to one acknowledged native epoch."""
from __future__ import annotations

import copy

import pytest

from lib import agent_goals, agents, db, native_rotation_accounting as accounting
from lib import native_rotation_goals as carried, native_rotation_journal as journal, settings_store
from tests.unit.test_agent_goals import _agent, _client


def _raw(thread, **changes):
    return {"threadId": thread, "objective": "Finish task", "status": "paused",
            "tokenBudget": 600, "tokensUsed": 0, "timeUsedSeconds": 0,
            "createdAt": 300, "updatedAt": 300, **changes}


def _bound(tmp_path):
    agent = _agent(tmp_path)
    old = agents.live_backend_session(agent)
    transfer = accounting.plan_transfer(agent, old, _raw(old, status="active", tokenBudget=1000,
                                                        tokensUsed=400, timeUsedSeconds=25,
                                                        createdAt=100, updatedAt=200))
    journal.reserve(agent, old, "rotation-a")
    for before, after in (("reserved", "quiescing"), ("quiescing", "quiescent"),
                          ("quiescent", "create_pending"), ("create_pending", "created"),
                          ("created", "final_imported"), ("final_imported", "chats_carried"),
                          ("chats_carried", "plans_carried")):
        journal.advance(agent, "rotation-a", before, after,
                        **({"new_thread_id": "new-thread"} if after == "created" else {}))
    journal.bind(agent, "rotation-a", goal_transfer=transfer, new_goal=_raw("new-thread"),
                 incarnation="transport-unique-a")
    client = _client()
    client.rotation_incarnation = "transport-unique-a"
    return agent, old, client


def test_real_native_mirror_projects_once_and_old_callbacks_cannot_reset_it(tmp_path):
    agent, old, client = _bound(tmp_path)
    assert agent_goals.get(agent)["tokens_used"] == 400
    update = _raw("new-thread", tokensUsed=30, timeUsedSeconds=4, status="active", updatedAt=310)
    for _ in range(2):
        client._notification("thread/goal/updated", {"threadId": "new-thread", "goal": update})
    row = agent_goals.get(agent)
    assert (row["tokens_used"], row["time_used_seconds"], row["token_budget"], row["created_at"]) == (
        430, 29, 1000, 100000)
    client._notification("thread/goal/updated", {"threadId": old, "goal": _raw(old)})
    client._notification("thread/goal/cleared", {"threadId": old})
    assert agent_goals.get(agent) == row
    record = carried.get(agent)
    assert record["offset"]["last_native_tokens"] == 30
    assert record["offset"]["last_native_seconds"] == 4
    # A same-second clear+new goal cannot borrow the old epoch's allowance.
    client._notification("thread/goal/cleared", {"threadId": "new-thread"})
    assert agent_goals.get(agent) is None and carried.get(agent)["state"] == "retired"
    client._notification("thread/goal/updated", {"threadId": "new-thread", "goal": update})
    assert agent_goals.get(agent)["tokens_used"] == 30


@pytest.mark.parametrize("damage", ["incarnation", "epoch", "reset"])
def test_lost_continuity_or_reset_preserves_usage_and_protects_native_admission(tmp_path, damage):
    agent, _, client = _bound(tmp_path)
    update = _raw("new-thread", tokensUsed=30, timeUsedSeconds=4)
    client._mirror_goal("new-thread", update)
    if damage == "incarnation":
        client.rotation_incarnation = "transport-unique-b"
    elif damage == "epoch":
        update["createdAt"] = 301
    else:
        update["tokensUsed"] = 0
    client._mirror_goal("new-thread", update)
    row = agent_goals.get(agent)
    assert (row["tokens_used"], row["time_used_seconds"], row["token_budget"]) == (430, 29, 1000)
    assert row["status"] == agent_goals.BLOCKED
    assert carried.get(agent)["state"] == "protected"
    with pytest.raises(accounting.AccountingUnavailable):
        carried.require_epoch(agent, "new-thread", client.rotation_incarnation)


def test_joined_goal_and_offset_failure_roll_back_only_the_mirror_unit(tmp_path, monkeypatch):
    agent, _, _ = _bound(tmp_path)
    original_row, original_offset = agent_goals.get(agent), copy.deepcopy(carried.get(agent))
    original_write = settings_store.set_text
    failure = RuntimeError("offset commit denied")

    def denied(key, value, **kwargs):
        if key == carried.key(agent):
            raise failure
        return original_write(key, value, **kwargs)

    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    settings_store.set_text("caller-owned", "keep")
    monkeypatch.setattr(settings_store, "set_text", denied)
    with pytest.raises(RuntimeError) as caught:
        agent_goals.mirror_codex("new-thread", _raw("new-thread", tokensUsed=5),
                                incarnation="transport-unique-a")
    assert caught.value is failure and con.in_transaction
    con.execute("COMMIT")
    assert settings_store.get("caller-owned") == "keep"
    assert agent_goals.get(agent) == original_row and carried.get(agent) == original_offset
