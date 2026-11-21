"""A clarp-goal refusal names what to run next, with the goal's current revision in it (R140).

An agent that ran 'clarp-goal finish PLAN' on a durable goal was told only 'revision required
for durable goal', tried three forms and gave up, its goal left active. Each refusal an agent can
meet on the way to finishing now says the command, the revision, and what is missing."""
from __future__ import annotations

import pytest

from lib import agents, task_plans


@pytest.fixture
def goal(tmp_path):
    aid = agents.create_agent(persona="Pip", voice_id="", cwd=str(tmp_path), session="pip")
    agents.bind_backend_session(aid, "pip-native")
    plan = task_plans.create(session="pip", plan_id="game", title="Game",
                             items=[{"id": "build", "title": "Build"}],
                             goal={"outcome": "Ship the game", "criteria": ["Shipped", "Tested"],
                                   "limits": "none", "enroll": False})
    return task_plans.get(plan["plan_id"])


def test_finish_without_a_revision_names_complete_and_the_revision(goal):
    with pytest.raises(ValueError) as refused:
        task_plans.finish(goal["plan_id"])
    assert str(refused.value) == (
        f"revision required for durable goal {goal['plan_id']} (now revision {goal['revision']}): "
        f"run `clarp-goal complete {goal['plan_id']} {goal['revision']}`")


def test_a_step_without_a_revision_names_the_step_command(goal):
    with pytest.raises(ValueError) as refused:
        task_plans.update_item(task_plans.item_key(goal["plan_id"], "build"), "completed")
    assert str(refused.value).endswith(
        f"run `clarp-goal step {goal['plan_id']} {goal['revision']} build completed`")


def test_complete_with_work_unresolved_names_the_steps_and_the_command(goal):
    with pytest.raises(ValueError) as refused:
        task_plans.finish(goal["plan_id"], revision=goal["revision"])
    assert str(refused.value) == (
        f"required work remains unresolved: build; settle each with "
        f"`clarp-goal step {goal['plan_id']} {goal['revision']} ITEM completed` (or removed, with a reason)")


def test_complete_without_evidence_names_the_missing_criteria_and_the_checkpoint(goal):
    plan = task_plans.update_item(task_plans.item_key(goal["plan_id"], "build"), "completed",
                                  revision=goal["revision"])
    plan = task_plans.get(goal["plan_id"])
    with pytest.raises(ValueError) as refused:
        task_plans.finish(plan["plan_id"], revision=plan["revision"])
    text = str(refused.value)
    assert "missing: criterion-1, criterion-2." in text
    assert (f"`clarp-goal checkpoint {plan['plan_id']} {plan['revision']} '{{\"progress\":\"...\","
            f"\"next_work\":\"...\",\"evidence\":{{\"criterion-1\":\"...\", \"criterion-2\":\"...\"}}}}'`") in text
    assert text.endswith(f"then `clarp-goal complete {plan['plan_id']} REVISION` with the revision it answers")


def test_an_unknown_action_names_the_actions_and_complete(goal):
    from lib import task_goal_state
    with pytest.raises(ValueError) as refused:
        task_goal_state.mutate(goal["plan_id"], revision=goal["revision"], action="finish", data={})
    text = str(refused.value)
    assert text.startswith("unknown goal action 'finish': use one of add_step, block, cancel, checkpoint,")
    assert text.endswith("(and `clarp-goal complete PLAN REVISION` to finish a goal)")


def test_the_cli_usage_names_complete():
    import subprocess
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[2] / "scripts" / "agent_tasks.py"
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert done.returncode == 2 and "complete PLAN_ID REVISION (finishes a goal)" in done.stderr
