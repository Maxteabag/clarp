"""The goal updater keeps goals current beside the working agent.

2026-11-13: a goal sat a week as "0 of 4 steps done", step 1 "Active", while a
helper had finished all four steps on the first day. The owner had delegated
the work and only checkpointed; pausing reset the running step to pending but
kept its active time; and correcting it needed resume, edit, pause.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from lib import agents as agents_db
from lib import db, goal_updater, task_goal_state, task_plans

MIN = 60_000


@pytest.fixture
def owner(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Jonatan", voice_id="V", cwd=str(tmp_path), session="jonatan", backend="claude")
    agents_db.start_runtime(agent_id, "jonatan")
    agents_db.bind_backend_session(agent_id, "native-updater-test")
    return agent_id


@pytest.fixture
def helper(owner, tmp_path):
    agent_id = agents_db.create_agent(
        persona="Designer", voice_id="V", cwd=str(tmp_path), session="designer-1", backend="codex")
    db.conn().execute("UPDATE agents SET parent_agent_id=?, role='helper' WHERE agent_id=?",
                      (owner, agent_id))
    return agent_id


def _goal(**extra):
    return task_plans.create(
        session="jonatan", title="Booking UI hill-climb",
        items=[{"id": "harness", "title": "Screenshot harness"},
               {"id": "climb", "title": "Iterate to 9/10"},
               {"id": "pr", "title": "Open the PR"}],
        goal={"criteria": ["Both judges give 9/10", "PR opened"], "limits": "No merge",
              "enroll": True, **extra})


def _act(plan_id, action, **data):
    current = task_plans.get(plan_id)
    return task_goal_state.mutate(plan_id, revision=current["revision"], action=action, data=data)


def _item(plan, stable):
    return next(i for i in task_plans.get(plan["plan_id"])["items"]
                if i["item_id"].endswith(":" + stable))


def _step(plan, stable, status, detail=None):
    current = task_plans.get(plan["plan_id"])
    return task_plans.update_item(_item(plan, stable)["item_id"], status, detail,
                                  revision=current["revision"])


_SEQ = [0]


def _message(agent_id, role, text, *, at, phase=None, sender=None, tools=()):
    _SEQ[0] += 1
    stamp = datetime.fromtimestamp(at / 1000, timezone.utc).isoformat()
    db.conn().execute(
        "INSERT INTO messages(message_id,agent_id,backend_session_id,seq,role,timestamp,text,"
        "tools_json,updated_at,origin,sender_agent_id,phase) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"m{_SEQ[0]}", agent_id, "s", _SEQ[0], role, stamp, text,
         json.dumps([{"name": n, "summary": s} for n, s in tools]), at,
         "agent" if sender else "user", sender, phase))


# ---- pause keeps the record, stops the clock --------------------------------


def test_pause_keeps_a_running_step_in_progress_and_stops_its_clock(owner):
    plan = _goal()
    _step(plan, "harness", "in_progress")
    _act(plan["plan_id"], "pause", reason="Waiting for Peter")
    item = _item(plan, "harness")
    assert item["status"] == "in_progress" and item["paused"] is True
    assert item["started_at"] is None
    frozen = item["elapsed_ms"]
    assert task_plans.get(plan["plan_id"])["items"][0]["elapsed_ms"] == frozen
    _act(plan["plan_id"], "resume", reason="Peter answered")
    item = _item(plan, "harness")
    assert item["paused"] is False and item["started_at"] is not None


def test_owner_can_record_steps_and_progress_on_a_paused_goal(owner):
    plan = _goal()
    _act(plan["plan_id"], "pause", reason="Waiting for Peter")
    _step(plan, "harness", "completed")
    assert _item(plan, "harness")["status"] == "completed"
    after = _act(plan["plan_id"], "checkpoint", progress="All rounds done, PR #76 open",
                 next_work="Peter decides on PR #76")
    assert after["status"] == "paused"
    assert after["goal"]["checkpoint"]["progress"] == "All rounds done, PR #76 open"
    assert after["goal"]["continuation"]["state"] == "paused"


def test_a_paused_checkpoint_with_a_continuation_saves_nothing(owner):
    plan = _goal()
    _act(plan["plan_id"], "pause", reason="Waiting")
    before = task_plans.get(plan["plan_id"])["revision"]
    with pytest.raises(ValueError, match="goal is paused"):
        _act(plan["plan_id"], "checkpoint", progress="x", next_work="y",
             continuation={"kind": "timer", "due_at": db.now_ms() + MIN})
    assert task_plans.get(plan["plan_id"])["revision"] == before


# ---- waiver ------------------------------------------------------------------


def test_a_waived_criterion_lets_the_goal_complete_and_stays_visible(owner):
    plan = _goal()
    goal = task_plans.get(plan["plan_id"])["goal"]
    judges, pr = goal["criteria"]
    for stable in ("harness", "climb", "pr"):
        _step(plan, stable, "completed")
    _act(plan["plan_id"], "checkpoint", progress="Done", next_work="none",
         evidence={pr["id"]: "PR #76 opened"})
    with pytest.raises(ValueError, match="waiver"):
        task_plans.finish(plan["plan_id"], revision=task_plans.get(plan["plan_id"])["revision"])
    with pytest.raises(ValueError, match="reason"):
        _act(plan["plan_id"], "waive", criterion_id=judges["id"])
    _act(plan["plan_id"], "waive", criterion_id=judges["id"],
         reason="Peter accepted 8/10 after ten rounds")
    done = task_plans.finish(plan["plan_id"], revision=task_plans.get(plan["plan_id"])["revision"])
    assert done["status"] == "completed"
    waived = next(c for c in done["goal"]["criteria"] if c["id"] == judges["id"])
    assert waived["waived"]["reason"] == "Peter accepted 8/10 after ten rounds"
    assert waived["evidence"] == ""


# ---- the updater's write path -------------------------------------------------


def test_updater_writes_steps_and_its_own_checkpoint_only(owner):
    plan = _goal()
    _act(plan["plan_id"], "checkpoint", progress="Helper started", next_work="Wait for it")
    before = task_plans.get(plan["plan_id"])
    harness = _item(plan, "harness")["item_id"]
    task_plans.updater_apply(
        plan["plan_id"], seen_revision=before["revision"],
        steps={harness: {"status": "completed", "note": "Evidence: helper said harness done"}},
        progress="Harness done, round 2 running", next_work="Round 3",
        model="gemini-4-hydrogen-low", source_refs=["message:m1"], through_ms=123)
    after = task_plans.get(plan["plan_id"])
    assert _item(plan, "harness")["status"] == "completed"
    assert after["revision"] == before["revision"], "the owner's next command is not refused"
    assert after["status"] == before["status"] == "active"
    assert after["goal"]["checkpoint"] == before["goal"]["checkpoint"]
    assert after["goal"]["continuation"] == before["goal"]["continuation"]
    assert all(not c.get("evidence") for c in after["goal"]["criteria"])
    mine = after["goal"]["updater"]
    assert (mine["progress"], mine["model"], mine["through_ms"]) == (
        "Harness done, round 2 running", "gemini-4-hydrogen-low", 123)
    event = db.conn().execute(
        "SELECT actor_kind, delegation_id FROM goal_events WHERE plan_id=? AND kind='updater'",
        (plan["plan_id"],)).fetchone()
    assert tuple(event) == ("system", "goal-updater")


def test_updater_never_reopens_closed_steps_or_writes_over_a_newer_plan(owner):
    plan = _goal()
    _step(plan, "harness", "completed")
    current = task_plans.get(plan["plan_id"])
    harness = _item(plan, "harness")["item_id"]
    task_plans.updater_apply(plan["plan_id"], seen_revision=current["revision"],
                             steps={harness: {"status": "in_progress"}}, progress="p",
                             next_work="n", model="m", source_refs=[])
    assert _item(plan, "harness")["status"] == "completed"
    with pytest.raises(ValueError, match="plan changed"):
        task_plans.updater_apply(plan["plan_id"], seen_revision=current["revision"] - 1,
                                 steps={}, progress="p", next_work="n", model="m", source_refs=[])


def test_updater_records_on_a_paused_goal_without_starting_a_clock(owner):
    plan = _goal()
    _act(plan["plan_id"], "pause", reason="Waiting")
    current = task_plans.get(plan["plan_id"])
    climb = _item(plan, "climb")["item_id"]
    task_plans.updater_apply(plan["plan_id"], seen_revision=current["revision"],
                             steps={climb: {"status": "in_progress"}}, progress="p",
                             next_work="n", model="m", source_refs=[])
    item = _item(plan, "climb")
    assert item["status"] == "in_progress" and item["paused"] and item["started_at"] is None
    assert task_plans.get(plan["plan_id"])["status"] == "paused"


def test_a_completed_answer_without_evidence_stays_in_progress(owner):
    plan = task_plans.get(_goal()["plan_id"])
    steps, progress, _ = goal_updater.decide(plan, {
        "steps": {"harness": {"status": "completed", "evidence": "Helper: harness built, 3 apps"},
                  "climb": {"status": "completed"},
                  "nope": {"status": "completed", "evidence": "x"}},
        "checkpoint": {"progress": "Harness built", "next_work": "Climb"}})
    by_stable = {k.split(":")[-1]: v for k, v in steps.items()}
    assert by_stable["harness"]["status"] == "completed"
    assert by_stable["climb"]["status"] == "in_progress"
    assert "no concrete evidence" in by_stable["climb"]["note"]
    assert "nope" not in by_stable and progress == "Harness built"


# ---- when it runs -------------------------------------------------------------


class _Model:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt, model_id):
        self.prompts.append(prompt)
        return self.answers.pop(0), {"input_tokens": 3000, "output_tokens": 400}


@pytest.fixture(autouse=True)
def _fresh_state():
    goal_updater._STATE.clear()
    yield
    goal_updater._STATE.clear()


def test_triggers_fold_and_nothing_new_costs_no_model_call(owner, helper):
    plan = _goal()
    base = db.now_ms()
    assert goal_updater.due(base) == [], "no activity, no update"
    _message(helper, "assistant", "Harness built", at=base + 1000, phase="final")
    assert goal_updater.due(base + 2000) == [], "folding: not before the window"
    assert goal_updater.due(base + 1000 + goal_updater.fold_ms()) == [plan["plan_id"]]
    model = _Model([{"steps": {"harness": {"status": "completed", "evidence": "Harness built"}},
                     "checkpoint": {"progress": "Harness built", "next_work": "Baseline"}}])
    goal_updater.update(plan["plan_id"], now=base + 1000 + goal_updater.fold_ms(), call=model)
    assert len(model.prompts) == 1
    goal_updater._STATE.clear()
    later = base + 10 * goal_updater.fold_ms()
    assert goal_updater.due(later) == [], "the cursor moved past what was read"
    assert goal_updater.update(plan["plan_id"], now=later, call=model) == {"skipped": "nothing new"}
    assert len(model.prompts) == 1


def test_a_pause_is_picked_up_within_the_minute(owner):
    plan = _goal()
    now = db.now_ms()
    _act(plan["plan_id"], "pause", reason="Waiting")
    assert goal_updater.due(now + 5000) == []
    assert goal_updater.due(now + goal_updater.FAST_FOLD_SEC * 1000 + 5000) == [plan["plan_id"]]


def test_the_sweep_catches_a_long_turn_with_no_final_message(owner):
    plan = _goal()
    base = db.now_ms()
    _message(owner, "assistant", "still working", at=base + 1000)
    assert goal_updater.due(base + 2 * MIN) == []
    assert goal_updater.due(base + goal_updater.sweep_ms() + 2000) == [plan["plan_id"]]


def test_the_kill_switch_stops_every_run(owner, monkeypatch):
    _goal()
    monkeypatch.setenv("CLARP_GOAL_UPDATER", "0")
    assert goal_updater.GoalUpdater(call=_Model([])).tick() == []


def test_the_working_agent_is_never_sent_anything(owner, helper):
    plan = _goal()
    base = db.now_ms()
    _message(owner, "assistant", "Started helper", at=base + 1000, phase="final")
    count = db.conn().execute("SELECT COUNT(*) FROM messages WHERE agent_id=?", (owner,)).fetchone()[0]
    goal_updater.update(plan["plan_id"], now=base + 2000, call=_Model([
        {"steps": {}, "checkpoint": {"progress": "Helper started", "next_work": "Wait"}}]))
    assert db.conn().execute("SELECT COUNT(*) FROM messages WHERE agent_id=?",
                             (owner,)).fetchone()[0] == count


# ---- replay: the 6 Nov goal, stubbed model -------------------------------------


REPLAY = [
    # (minutes after start, who, role, text, phase, answer)
    (2, "owner", "assistant", "Started helper designer-1 with the brief", "final",
     {"steps": {"harness": {"status": "in_progress"}},
      "checkpoint": {"progress": "Helper designer-1 building the harness", "next_work": "Baseline"}}),
    (40, "helper", "assistant", "Harness and reference apps ready; baseline Codex 4/10", "final",
     {"steps": {"harness": {"status": "completed", "evidence": "Harness and reference apps ready"},
                "climb": {"status": "in_progress"}},
      "checkpoint": {"progress": "Baseline 4/10, climbing", "next_work": "Rounds 2-10"}}),
    (78, "report", "user", "Done: 10 rounds, Codex 8/8, Opus 8/7. Branch pushed.", None,
     {"steps": {"climb": {"status": "completed", "evidence": "10 rounds done, Codex 8/8"},
                "pr": {"status": "in_progress"}},
      "checkpoint": {"progress": "All 10 rounds done (8/8, 8/7)", "next_work": "Open the PR"}}),
    (80, "owner", "assistant", "PR #76 opened; pausing for Peter's decision", "final",
     {"steps": {"pr": {"status": "completed", "evidence": "PR #76 opened"}},
      "checkpoint": {"progress": "PR #76 open; ≥9 not reached", "next_work": "Peter decides"}}),
]


def test_replay_of_a_delegated_goal_ends_current(owner, helper):
    plan = _goal()
    base = db.now_ms()
    model = _Model([row[5] for row in REPLAY])
    for minutes, who, role, text, phase, _answer in REPLAY:
        at = base + minutes * MIN
        if who == "report":
            _message(owner, "user", text, at=at, sender=helper)
        else:
            _message(owner if who == "owner" else helper, role, text, at=at, phase=phase,
                     tools=[("Bash", "npm run build")] if who == "helper" else ())
        if minutes == 80:
            _act(plan["plan_id"], "pause", reason="Waiting for Peter's decision on PR #76")
        goal_updater.update(plan["plan_id"], now=at + 1000, call=model)
    final = task_plans.get(plan["plan_id"])
    assert {i["item_id"].split(":")[-1]: i["status"] for i in final["items"]} == {
        "harness": "completed", "climb": "completed", "pr": "completed"}
    assert final["status"] == "paused" and final["completed_count"] == 3
    assert final["goal"]["updater"]["progress"] == "PR #76 open; ≥9 not reached"
    assert final["goal"]["checkpoint"] is None or "progress" not in (final["goal"]["checkpoint"] or {}) \
        or final["goal"]["checkpoint"]["progress"] != final["goal"]["updater"]["progress"]
    assert "[tool Bash: npm run build]" in model.prompts[1]
    assert "Done: 10 rounds" in model.prompts[2] and "(from agent helper designer-1)" in model.prompts[2]
    assert "untrusted" in model.prompts[0]
