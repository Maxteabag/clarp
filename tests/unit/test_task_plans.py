from lib import agents, db, task_plans
import pytest


def _agent(tmp_path):
    agents.create_agent(
        persona="Mike", voice_id="v", cwd=str(tmp_path), session="mike")


def test_hierarchical_plan_progress_and_auto_finish(tmp_path, monkeypatch):
    _agent(tmp_path)
    clock = [1000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    plan = task_plans.create(
        session="mike", plan_id="feature", title="Ship feature", items=[
            {"id": "build", "title": "Build", "subtasks": [
                {"id": "server", "title": "Server"},
                {"id": "ios", "title": "iOS"},
            ]},
        ])
    assert plan["total_count"] == 3
    clock[0] = 1200
    plan_id = plan["plan_id"]
    task_plans.update_item(task_plans.item_key(plan_id, "server"), "in_progress")
    clock[0] = 1500
    updated = task_plans.update_item(
        task_plans.item_key(plan_id, "server"), "completed")
    assert updated["items"][0]["subtasks"][0]["elapsed_ms"] == 300
    clock[0] = 1600
    task_plans.update_item(task_plans.item_key(plan_id, "ios"), "skipped")
    clock[0] = 1700
    done = task_plans.update_item(task_plans.item_key(plan_id, "build"), "completed")
    assert done["completed_count"] == 3
    assert done["status"] == "completed"
    assert task_plans.active_for_session("mike") is None


def test_new_plan_cancels_previous_active_plan(tmp_path):
    _agent(tmp_path)
    old = task_plans.create(
        session="mike", plan_id="old", title="Old", items=[
            {"id": "old-item", "title": "Old item"}])
    new = task_plans.create(
        session="mike", plan_id="old", title="New", items=[
            {"id": "new-item", "title": "New item"}])
    assert task_plans.get(old["plan_id"])["status"] == "cancelled"
    assert task_plans.active_for_session("mike")["plan_id"] == new["plan_id"]


def test_finishing_plan_freezes_running_item_time(tmp_path, monkeypatch):
    _agent(tmp_path)
    clock = [1000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    plan = task_plans.create(
        session="mike", plan_id="blocked", title="Blocked", items=[
            {"id": "wait", "title": "Wait"}])
    task_plans.update_item(task_plans.item_key(plan["plan_id"], "wait"), "in_progress")
    clock[0] = 2500
    closed = task_plans.finish(plan["plan_id"], "blocked")
    assert closed["items"][0]["elapsed_ms"] == 1500
    assert closed["items"][0]["status"] == "blocked"
    clock[0] = 9000
    assert task_plans.get(plan["plan_id"])["items"][0]["elapsed_ms"] == 1500


def test_deleting_agent_cancels_active_plan(tmp_path):
    _agent(tmp_path)
    plan = task_plans.create(
        session="mike", plan_id="work", title="Work", items=[
            {"id": "step", "title": "Step"}])
    agent = agents.get_by_session("mike")
    agents.soft_delete(agent["agent_id"])
    assert task_plans.get(plan["plan_id"])["status"] == "cancelled"
    assert task_plans.active_for_session("mike") is None


def test_long_stable_aliases_keep_unique_suffixes(tmp_path):
    _agent(tmp_path)
    alias = "very-long-plan-" + "x" * 200
    first = task_plans.create(
        session="mike", plan_id=alias, title="One", items=[
            {"id": "y" * 200, "title": "Step"}])
    second = task_plans.create(
        session="mike", plan_id=alias, title="Two", items=[
            {"id": "y" * 200, "title": "Step"}])
    assert first["plan_id"] != second["plan_id"]
    assert first["items"][0]["item_id"] != second["items"][0]["item_id"]


def test_stale_updates_cannot_rewrite_cancelled_plan(tmp_path):
    _agent(tmp_path)
    old = task_plans.create(
        session="mike", plan_id="old", title="Old", items=[
            {"id": "step", "title": "Step"}])
    task_plans.create(
        session="mike", plan_id="new", title="New", items=[
            {"id": "step", "title": "Step"}])
    with pytest.raises(ValueError, match="no longer active"):
        task_plans.finish(old["plan_id"])
    with pytest.raises(ValueError, match="no longer active"):
        task_plans.update_item(
            task_plans.item_key(old["plan_id"], "step"), "completed")
    assert task_plans.get(old["plan_id"])["status"] == "cancelled"
