"""Carried logical counters remain bound to one acknowledged native epoch."""
from __future__ import annotations

import copy
import json
import threading

import pytest

from lib import agent_goals, agents, db, native_rotation_accounting as accounting
from lib import native_rotation_goals as carried, native_rotation_journal as journal, settings_store
from tests.unit.test_agent_goals import _agent, _client


def _raw(thread, **changes):
    return {"threadId": thread, "objective": "Finish task", "status": "paused",
            "tokenBudget": 600, "tokensUsed": 0, "timeUsedSeconds": 0,
            "createdAt": 300, "updatedAt": 300, **changes}


def _bound(tmp_path, *, budget=1000):
    agent = _agent(tmp_path)
    old = agents.live_backend_session(agent)
    transfer = accounting.plan_transfer(agent, old, _raw(old, status="active", tokenBudget=budget,
                                                        tokensUsed=400, timeUsedSeconds=25,
                                                        createdAt=100, updatedAt=200))
    journal.reserve(agent, old, "rotation-a")
    for before, after in (("reserved", "quiescing"), ("quiescing", "quiescent"),
                          ("quiescent", "create_pending"), ("create_pending", "created"),
                          ("created", "final_imported"), ("final_imported", "chats_carried"),
                          ("chats_carried", "plans_carried")):
        journal.advance(agent, "rotation-a", before, after,
                        **({"new_thread_id": "new-thread"} if after == "created" else {}))
    journal.bind(agent, "rotation-a", goal_transfer=transfer,
                 new_goal=None if transfer["defer_native_goal"] else _raw("new-thread"),
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
    # Only a correlated explicit new-goal response retires the old budget;
    # an old raw update after clear cannot resurrect its offset or goal.
    client._notification("thread/goal/updated", {"threadId": "new-thread", "goal": update})
    assert agent_goals.get(agent) is None
    agent_goals.mirror_codex("new-thread", update, incarnation="transport-unique-a",
                            user_action="start")
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


@pytest.mark.parametrize("budget", [0, 300, 400])
def test_exhausted_goal_is_carried_nonrunning_without_a_native_allowance(tmp_path, budget):
    agent, _, client = _bound(tmp_path, budget=budget)
    row = agent_goals.get(agent)
    assert (row["token_budget"], row["tokens_used"], row["status"]) == (budget, 400, "budget_limited")
    assert carried.get(agent)["offset"]["defer_native_goal"] is True
    client._mirror_goal("new-thread", None)
    assert agent_goals.get(agent) == row
    with pytest.raises(accounting.AccountingUnavailable):
        carried.require_activation(agent, "new-thread", "transport-unique-a")
    # Explicit clear has its own correlated owner authority; an empty native
    # goal snapshot on its own did not erase the exhausted logical goal.
    agent_goals.mirror_codex("new-thread", None, incarnation="transport-unique-a", user_action="clear")
    assert agent_goals.get(agent) is None and carried.get(agent)["state"] == "retired"


@pytest.mark.parametrize("invalid_budget", [False, 0.0, "0"])
def test_malformed_exhausted_native_budget_is_not_valid_rotation_authority(tmp_path, invalid_budget):
    agent, _, _ = _bound(tmp_path, budget=0)
    original_goal = agent_goals.get(agent)
    record = carried.get(agent)
    record["offset"]["native_budget"] = invalid_budget
    settings_store.set_text(carried.key(agent), json.dumps(record))
    with pytest.raises(accounting.AccountingUnavailable):
        carried.require_activation(agent, "new-thread", "transport-unique-a")
    # Failure must be at stored-proof decoding as well, not only the existing
    # exhausted-goal activation refusal (which would conceal a permissive read).
    with pytest.raises(accounting.AccountingUnavailable):
        carried.get(agent)
    assert agent_goals.get(agent) == original_goal


@pytest.mark.parametrize("loaded", [False, True])
def test_lost_epoch_refuses_implicit_resume_even_when_thread_was_loaded(tmp_path, loaded):
    agent, _, client = _bound(tmp_path)
    client.rotation_incarnation = "different-transport"
    if loaded:
        client._loaded_threads.add("new-thread")
    requests = []

    def unexpected(method, params, **kwargs):
        requests.append(method)
        raise RuntimeError("native request should not have been issued")

    client.request = unexpected
    with pytest.raises(accounting.AccountingUnavailable):
        client._ensure_loaded(agents.get_by_agent_id(agent), "new-thread")
    assert requests == []


@pytest.mark.parametrize("action", ["start", "resume"])
def test_exhausted_carried_goal_cannot_be_activated_by_an_ordinary_goal_request(tmp_path, action):
    agent, _, client = _bound(tmp_path, budget=0)
    before = agent_goals.get(agent)
    client._loaded_threads.add("new-thread")
    requests = []

    def unexpected(method, params, **kwargs):
        requests.append(method)
        raise RuntimeError("native activation should not have been issued")

    client.request = unexpected
    with pytest.raises(accounting.AccountingUnavailable):
        client._goal_request(agents.get_by_agent_id(agent), action, "new-thread", "New objective")
    assert requests == [] and agent_goals.get(agent) == before


def test_explicit_clear_of_a_protected_epoch_never_resumes_the_native_goal_first(tmp_path):
    from tests.codex_request import result_reply
    agent, _, client = _bound(tmp_path)
    client.rotation_incarnation = "different-transport"
    requests = []

    @result_reply
    def request(method, params, **kwargs):
        requests.append((method, params))
        return {}

    client.request = request
    client._goal_request(agents.get_by_agent_id(agent), "clear", "new-thread", "")
    assert requests == [("thread/goal/clear", {"threadId": "new-thread"})]
    assert agent_goals.get(agent) is None and carried.get(agent)["state"] == "retired"


def test_direct_turn_start_refuses_lost_epoch_after_releasing_the_pool_claim(tmp_path):
    from lib.codex_app_server import AppTurnHandle
    agent, _, client = _bound(tmp_path)
    client.rotation_incarnation = "different-transport"
    client._claims_lock = threading.RLock()
    handle = AppTurnHandle(client, agent_id=agent)
    claimed, requests = [], []

    def unexpected(method, params, **kwargs):
        requests.append(method)
        raise RuntimeError("native request should not have been issued")

    client.request = unexpected
    with pytest.raises(accounting.AccountingUnavailable):
        client.start(text="User request", cwd=tmp_path, backend_session_id="new-thread",
                     is_new_session=False, session="theo", trace_id="new-user-trace", model="", effort="",
                     voice=False, persona="Theo", handle=handle, on_session_init=None,
                     on_result=None, on_error=None, stream=None, enqueue=None, agent_id=agent,
                     on_claimed=lambda: claimed.append(True))
    assert claimed == [True] and requests == []
    assert handle._done.is_set() and client._actives == {} and client.active is None
