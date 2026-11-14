"""The Goal keeper Janitor keeps goals current beside the working agent.

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
from lib import db, goal_keeper, janitor_autonomy, janitor_builtins, janitors, task_goal_state, task_plans

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
    from lib import goal_ledger
    with goal_ledger.acting_as("user"):  # the apps' route
        with pytest.raises(ValueError, match="reason"):
            _act(plan["plan_id"], "waive", criterion_id=judges["id"])
        _act(plan["plan_id"], "waive", criterion_id=judges["id"],
             reason="Peter accepted 8/10 after ten rounds")
    done = task_plans.finish(plan["plan_id"], revision=task_plans.get(plan["plan_id"])["revision"])
    assert done["status"] == "completed"
    waived = next(c for c in done["goal"]["criteria"] if c["id"] == judges["id"])
    assert waived["waived"]["reason"] == "Peter accepted 8/10 after ten rounds"
    assert waived["waived"]["by"] == "user"
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
        model="gemini-4-hydrogen-low", source_refs=["message:m1"], through_ms=123,
        janitor_agent_id="keeper-1")
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
        "SELECT actor_kind, actor_agent_id, delegation_id FROM goal_events WHERE plan_id=? AND kind='updater'",
        (plan["plan_id"],)).fetchone()
    assert tuple(event) == ("system", "keeper-1", "goal-keeper")
    assert mine["by"] == "goal-keeper" and mine["janitor_agent_id"] == "keeper-1"


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
    steps, progress, _ = goal_keeper.decide(plan, {
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

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0), {"input_tokens": 3000, "output_tokens": 400}, "gemini-4-hydrogen-low"


def _config(*, coalesce=300, activity=True, sweep=True, scope=None):
    attachments = [{"attachment_id": "a-turns", "trigger_id": "agent-work-completed", "trigger_version": 1,
                    "enabled": True, "config": {"coalesce_seconds": coalesce}}]
    if activity:
        attachments.append({"attachment_id": "a-activity", "trigger_id": "goal-activity-observed",
                            "trigger_version": 1, "enabled": True, "config": {}})
    if sweep:
        attachments.append({"attachment_id": "a-sweep", "trigger_id": "schedule", "trigger_version": 1,
                            "enabled": True, "config": {"cron": "17 * * * *", "timezone": "UTC"}})
    return {"agent_id": "keeper-1", "generation": 1, "scope": scope or {}, "options": {"max_goals_per_run": 5},
            "attachments": attachments}


# ---- when it runs -------------------------------------------------------------


def test_triggers_fold_and_nothing_new_costs_no_model_call(owner, helper):
    plan = _goal()
    base = db.now_ms()
    config, progress = _config(), {}
    assert goal_keeper.due(config, progress, base) == [], "no activity, no update"
    _message(helper, "assistant", "Harness built", at=base + 1000, phase="final")
    assert goal_keeper.due(config, progress, base + 2000) == [], "folding: not before the window"
    assert goal_keeper.due(config, progress, base + 1000 + 300_000) == [plan["plan_id"]]
    model = _Model([{"steps": {"harness": {"status": "completed", "evidence": "Harness built"}},
                     "checkpoint": {"progress": "Harness built", "next_work": "Baseline"}}])
    goal_keeper.update(plan["plan_id"], now=base + 301_000, ask=model)
    goal_keeper.settle(progress, plan["plan_id"], "updated", base + 301_000)
    assert len(model.prompts) == 1
    later = base + 3_000_000
    assert goal_keeper.due(config, progress, later) == [], "the cursor moved past what was read"
    assert goal_keeper.update(plan["plan_id"], now=later, ask=model)["outcome"] == "skipped"
    assert len(model.prompts) == 1


def test_only_the_attached_triggers_count(owner, helper):
    plan = _goal()
    base = db.now_ms()
    _message(helper, "assistant", "Harness built", at=base + 1000, phase="final")
    turns_only = _config(activity=False, sweep=False)
    assert goal_keeper.due(turns_only, {}, base + 400_000) == [], "a helper is goal activity"
    _message(owner, "assistant", "Started the climb", at=base + 2000, phase="final")
    assert goal_keeper.due(turns_only, {}, base + 400_000) == [plan["plan_id"]]


def test_a_pause_is_picked_up_within_the_minute(owner):
    plan = _goal()
    now = db.now_ms()
    _act(plan["plan_id"], "pause", reason="Waiting")
    progress = {}
    assert goal_keeper.due(_config(), progress, now + 5000) == []
    assert goal_keeper.due(_config(), progress, now + goal_keeper.FAST_FOLD_SEC * 1000 + 5000) == [plan["plan_id"]]


def test_the_schedule_sweep_catches_a_long_turn_with_no_final_message(owner):
    plan = _goal()
    base = db.now_ms()
    _message(owner, "assistant", "still working", at=base + 1000)
    progress = {}
    assert goal_keeper.due(_config(), progress, base + 2000) == []
    assert goal_keeper.due(_config(), progress, progress["next_sweep_at"] + 1000) == [plan["plan_id"]]


def test_one_retry_then_the_sweep_decides():
    progress = {"next_sweep_at": 9_000_000}
    goal_keeper.settle(progress, "p1", "failed", 1000, "agy exited 1")
    assert progress["goals"]["p1"]["retry_at"] == 1000 + goal_keeper.RETRY_AFTER_MS
    goal_keeper.settle(progress, "p1", "failed", 70_000, "agy exited 1")
    assert progress["goals"]["p1"] == {"retry_at": 9_000_000, "last_error": "agy exited 1"}
    goal_keeper.settle(progress, "p1", "conflict", 80_000, "plan changed")
    assert progress["goals"]["p1"]["retry_at"] == 95_000
    goal_keeper.settle(progress, "p1", "updated", 90_000)
    assert "p1" not in progress["goals"]


def test_the_working_agent_is_never_sent_anything(owner, helper):
    plan = _goal()
    base = db.now_ms()
    _message(owner, "assistant", "Started helper", at=base + 1000, phase="final")
    count = db.conn().execute("SELECT COUNT(*) FROM messages WHERE agent_id=?", (owner,)).fetchone()[0]
    goal_keeper.update(plan["plan_id"], now=base + 2000, ask=_Model([
        {"steps": {}, "checkpoint": {"progress": "Helper started", "next_work": "Wait"}}]))
    assert db.conn().execute("SELECT COUNT(*) FROM messages WHERE agent_id=?",
                             (owner,)).fetchone()[0] == count


# ---- the Janitor ----------------------------------------------------------------


def _keeper():
    return janitor_builtins.ensure_builtins()["goal-keeper"]


def test_the_goal_keeper_is_a_paused_builtin_janitor_with_three_triggers(owner):
    keeper = _keeper()
    assert not keeper["enabled"]
    assert keeper["backend"] == "agy" and keeper["model"] == "gemini-4-hydrogen-low"
    assert keeper["execution"] == {"executor": "ephemeral", "provider": "agy", "isolated": True}
    assert janitors.template("goal-keeper")["allowed_effects"] == ["goal_update"]
    watched = {a["trigger_id"]: a["config"] for a in keeper["attachments"] if a["enabled"]}
    assert watched["agent-work-completed"]["coalesce_seconds"] == 300
    assert "goal-activity-observed" in watched and watched["schedule"]["cron"] == "17 * * * *"
    assert janitor_builtins.eligible("goal-keeper") == [], "paused: nothing runs"
    plan = _goal()
    shown = task_plans.get(plan["plan_id"])["updater"]
    assert shown["enabled"] is False and shown["janitor_agent_id"] == keeper["agent_id"]


def test_isolated_execution_is_an_antigravity_option():
    assert janitors._execution("goal-keeper", {"provider": "agy", "isolated": True}, "agy")["isolated"] is True
    with pytest.raises(janitors.JanitorError, match="Antigravity"):
        janitors._execution("heartbeat-decider", {"provider": "codex", "isolated": True}, "codex")


def test_an_enabled_keeper_runs_one_recorded_batch(owner, helper):
    assert janitors.template("goal-keeper")["options"][0]["default"] == 5, "a queue of five"
    keeper = _keeper()
    keeper = janitors.set_enabled(keeper["session"], keeper["revision"], True)
    plan = _goal()
    _message(owner, "assistant", "Started the helper", at=db.now_ms() - 400_000, phase="final")
    # The goal is older than its trigger: move its cursor back so the turn counts.
    db.conn().execute("UPDATE task_plans SET created_at=created_at-500000 WHERE plan_id=?", (plan["plan_id"],))
    asked = []

    def ask(run, prompt, plan_id):
        asked.append((run["run_id"], plan_id))
        return ({"steps": {"harness": {"status": "in_progress"}},
                 "checkpoint": {"progress": "Helper started", "next_work": "Wait"}}, {}, "gemini-4-hydrogen-low")

    worker = janitor_autonomy.AutonomyJanitors(dispatch=None, notify=None, goal_ask=ask)
    worker.goal_keeper_once(wait=True)
    assert [p for _, p in asked] == [plan["plan_id"]]
    run = janitors.get_run(asked[0][0])
    assert run["status"] == "completed"
    assert task_plans.get(plan["plan_id"])["goal"]["updater"]["progress"] == "Helper started"
    assert _item(plan, "harness")["status"] == "in_progress"
    shown = task_plans.get(plan["plan_id"])["updater"]
    assert shown["enabled"] is True and shown["last_run_at"] and shown["last_error"] is None


# ---- agy isolation -------------------------------------------------------------


def test_agy_runs_sandboxed_in_its_own_stripped_home_and_forgets_the_conversation(tmp_path, monkeypatch):
    from lib.backend import agy
    monkeypatch.setenv("CLARP_CACHE_DIR", str(tmp_path / "cache"))
    seen = {}

    class FakeProc:
        pid = 0
        returncode = 0

        def __init__(self, argv, **kw):
            seen["argv"], seen["cwd"], seen["home"] = argv, kw["cwd"], kw["env"]["HOME"]
            store = tmp_path / "cache" / "keeper-home" / ".gemini" / "antigravity-cli"
            (store / "conversations").mkdir(parents=True, exist_ok=True)
            (store / "brain" / "c-1").mkdir(parents=True, exist_ok=True)
            (store / "conversations" / "c-1.db").write_text("x")

        def communicate(self, timeout=None):
            return json.dumps({"conversation_id": "c-1", "status": "SUCCESS", "response": '{"ok": true}',
                               "usage": {"input_tokens": 9744}}), ""

    monkeypatch.setattr(agy.subprocess, "Popen", FakeProc)
    answer = agy.isolated_run("prompt", model="gemini-4-hydrogen-low", home_name="keeper-home", binary="agy")
    assert seen["argv"][1:9] == ["--sandbox", "--disable-slash-commands", "--print-timeout", "120s",
                                 "--output-format", "json", "--model", "gemini-4-hydrogen-low"]
    assert seen["argv"][-1] == "--print=prompt"
    home = tmp_path / "cache" / "keeper-home"
    assert seen["home"] == str(home) and seen["cwd"] == str(home / "work")
    assert not any((home / "work").iterdir()), "an empty working directory"
    store = home / ".gemini" / "antigravity-cli"
    assert not (store / "conversations" / "c-1.db").exists() and not (store / "brain" / "c-1").exists()
    assert answer["text"] == '{"ok": true}' and answer["usage"]["input_tokens"] == 9744


# ---- replay: the 6 Nov goal, stubbed model -------------------------------------


REPLAY = [
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
        goal_keeper.update(plan["plan_id"], now=at + 1000, ask=model)
    final = task_plans.get(plan["plan_id"])
    assert {i["item_id"].split(":")[-1]: i["status"] for i in final["items"]} == {
        "harness": "completed", "climb": "completed", "pr": "completed"}
    assert final["status"] == "paused" and final["completed_count"] == 3
    assert final["goal"]["updater"]["progress"] == "PR #76 open; ≥9 not reached"
    assert "[tool Bash: npm run build]" in model.prompts[1]
    assert "Done: 10 rounds" in model.prompts[2] and "(from agent helper designer-1)" in model.prompts[2]
    assert "untrusted" in model.prompts[0]


# ---- goals paused before the install (schema step v126) ----------------------


def _paused_before_install(owner, hours_running=1, hours_paused=9):
    """A goal as the code before contract 96 left it: paused, with a step still
    'running' (started_at set), and a pause entry in its history."""
    from lib import db_migrations
    plan = _goal()
    _step(plan, "harness", "in_progress")
    _act(plan["plan_id"], "pause", reason="Waiting for Peter")
    now = db.now_ms()
    started, paused_at = now - (hours_running + hours_paused) * 3_600_000, now - hours_paused * 3_600_000
    item_id = _item(plan, "harness")["item_id"]
    # Undo the new freeze to reproduce the old state, and date the pause.
    db.conn().execute("UPDATE task_items SET started_at=?, active_ms=0 WHERE item_id=?", (started, item_id))
    goal = json.loads(db.conn().execute(
        "SELECT goal_json FROM task_plans WHERE plan_id=?", (plan["plan_id"],)).fetchone()[0])
    for entry in goal["history"]:
        if entry["kind"] == "pause":
            entry["at"] = paused_at
    db.conn().execute("UPDATE task_plans SET goal_json=? WHERE plan_id=?", (json.dumps(goal), plan["plan_id"]))
    db_migrations._migrate_to_v126(db.conn())
    return plan, item_id, hours_running * 3_600_000


def _active_ms(item_id):
    return db.conn().execute("SELECT active_ms FROM task_items WHERE item_id=?", (item_id,)).fetchone()[0]


@pytest.mark.parametrize("then", ["resume", "owner_update", "keeper_write"])
def test_a_goal_paused_before_the_install_never_counts_its_paused_span(owner, then):
    from lib import db_migrations
    plan, item_id, ran = _paused_before_install(owner)
    assert _active_ms(item_id) == ran, "frozen at the last pause, not at the install"
    row = db.conn().execute("SELECT started_at FROM task_items WHERE item_id=?", (item_id,)).fetchone()
    assert row[0] is None
    db_migrations._migrate_to_v126(db.conn())
    assert _active_ms(item_id) == ran, "a second start changes nothing"

    if then == "resume":
        _act(plan["plan_id"], "resume", reason="Peter answered")
        assert _item(plan, "harness")["started_at"] is not None
    elif then == "owner_update":
        _step(plan, "harness", "completed")
    else:
        current = task_plans.get(plan["plan_id"])
        task_plans.updater_apply(plan["plan_id"], seen_revision=current["revision"],
                                 steps={item_id: {"status": "completed", "note": "Evidence: 'harness done'"}},
                                 progress="Harness done", next_work="n", model="m", source_refs=[])
    assert ran <= _active_ms(item_id) < ran + MIN, "the nine paused hours are not active time"



# ---- only the user waives (review #311, finding 2) ----------------------------


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("kind", ["owner", "delegate", "system", "peer", "unattributed"])
def test_an_agent_cannot_waive_its_own_criteria_and_complete_without_evidence(owner, kind, paused):
    from lib import goal_ledger
    plan = _goal()
    judges, pr = task_plans.get(plan["plan_id"])["goal"]["criteria"]
    if paused:
        _act(plan["plan_id"], "pause", reason="The user paused it")
    for action in ("waive", "unwaive"):
        with goal_ledger.acting_as(kind, owner if kind == "owner" else ""):
            with pytest.raises(ValueError, match="only the user"):
                _act(plan["plan_id"], action, criterion_id=judges["id"], reason="Close enough")
    stored = task_plans.get(plan["plan_id"])
    assert all("waived" not in c for c in stored["goal"]["criteria"])
    assert stored["status"] == ("paused" if paused else "active")
    if not paused:
        for stable in ("harness", "climb", "pr"):
            _step(plan, stable, "completed")
        stored = task_plans.get(plan["plan_id"])
        with pytest.raises(ValueError, match="evidence"):
            task_plans.finish(plan["plan_id"], revision=stored["revision"])


def test_the_users_waiver_can_be_restored_by_the_user_only(owner):
    from lib import goal_ledger
    plan = _goal()
    judges = task_plans.get(plan["plan_id"])["goal"]["criteria"][0]
    with goal_ledger.acting_as("user"):
        _act(plan["plan_id"], "waive", criterion_id=judges["id"], reason="Accepted as is")
    with goal_ledger.acting_as("owner", owner), pytest.raises(ValueError, match="only the user"):
        _act(plan["plan_id"], "unwaive", criterion_id=judges["id"])
    with goal_ledger.acting_as("user"):
        _act(plan["plan_id"], "unwaive", criterion_id=judges["id"])
    assert "waived" not in task_plans.get(plan["plan_id"])["goal"]["criteria"][0]


class _Route:
    """The apps' /task-plan/action handler with a request body and a turn identity."""

    def __init__(self, body, caller="", headers=None):
        from lib.routes.goals import GoalRoutes
        self._body, self._caller, self.answer = body, caller, None
        self.headers = headers or {}
        self._handle = GoalRoutes._handle_task_plan_action.__get__(self)

    def _read_json(self):
        return self._body

    def _turn_caller(self):
        return self._caller

    def _json_ok(self, payload):
        self.answer = (200, payload)

    def _json_error(self, code, message):
        self.answer = (code, message)


def test_the_route_acts_for_the_user_only_without_an_agent_turn(owner):
    plan = _goal()
    judges = task_plans.get(plan["plan_id"])["goal"]["criteria"][0]

    def waive(caller):
        current = task_plans.get(plan["plan_id"])
        route = _Route({"plan_id": plan["plan_id"], "revision": current["revision"], "action": "waive",
                        "data": {"criterion_id": judges["id"], "reason": "Accepted as is"}}, caller)
        route._handle()
        return route.answer

    code, message = waive(owner)          # an agent's turn calling the apps' route
    assert code == 409 and "only the user" in message
    code, payload = waive("")             # the apps, no turn identity
    assert code == 200
    assert next(c for c in payload["plan"]["goal"]["criteria"] if c["id"] == judges["id"])["waived"]["by"] == "user"


# ---- the keeper gives its database connections back (review #311, finding 5) --


def _open_db_handles():
    import os
    target = os.path.realpath(str(db.DB_PATH))
    count = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            if os.path.realpath(os.readlink(f"/proc/self/fd/{fd}")) == target:
                count += 1
        except OSError:
            continue
    return count


def test_update_threads_and_the_batch_thread_give_their_connections_back(owner, helper):
    keeper = _keeper()
    keeper = janitors.set_enabled(keeper["session"], keeper["revision"], True)
    plans = [_goal() for _ in range(3)]

    def ask(run, prompt, plan_id):
        return ({"steps": {}, "checkpoint": {"progress": "No change", "next_work": "Wait"}}, {}, "m")

    worker = janitor_autonomy.AutonomyJanitors(dispatch=None, notify=None, goal_ask=ask)

    def one_pass():
        for plan in plans:
            _message(owner, "assistant", "More work", at=db.now_ms() - 400_000, phase="final")
            db.conn().execute("UPDATE task_plans SET created_at=created_at-500000 WHERE plan_id=?",
                              (plan["plan_id"],))
        worker.goal_keeper_once(wait=True)
        config = janitors.get(keeper["session"])
        goal_keeper.run_batch(config, {"run_id": "direct"}, [p["plan_id"] for p in plans],
                              lambda prompt, plan_id: ask(None, prompt, plan_id), now=db.now_ms())

    one_pass()  # the first pass opens the long-lived connections this thread keeps
    before = _open_db_handles()
    for _ in range(12):
        one_pass()
    assert _open_db_handles() <= before, "every keeper thread closed its own connection"



# ---- review #311 leftovers --------------------------------------------------


def test_the_route_refuses_a_turn_token_that_does_not_verify(owner):
    plan = _goal()
    judges = task_plans.get(plan["plan_id"])["goal"]["criteria"][0]
    current = task_plans.get(plan["plan_id"])
    route = _Route({"plan_id": plan["plan_id"], "revision": current["revision"], "action": "waive",
                    "data": {"criterion_id": judges["id"], "reason": "Accepted"}},
                   caller="", headers={"X-Clarp-Turn": "not-a-turn-we-know"})
    route._handle()
    assert route.answer == (403, "verified turn identity required")
    assert "waived" not in task_plans.get(plan["plan_id"])["goal"]["criteria"][0]


def test_the_batch_thread_closes_its_own_connection(owner, helper, monkeypatch):
    import threading
    keeper = _keeper()
    janitors.set_enabled(keeper["session"], keeper["revision"], True)
    plan = _goal()
    _message(owner, "assistant", "Started", at=db.now_ms() - 400_000, phase="final")
    db.conn().execute("UPDATE task_plans SET created_at=created_at-500000 WHERE plan_id=?", (plan["plan_id"],))
    closed_by = []
    real_close = db.close_local

    def recording_close():
        closed_by.append(threading.current_thread().name)
        real_close()

    monkeypatch.setattr(db, "close_local", recording_close)
    worker = janitor_autonomy.AutonomyJanitors(
        dispatch=None, notify=None,
        goal_ask=lambda run, prompt, plan_id: ({"steps": {}, "checkpoint": {"progress": "p", "next_work": "n"}}, {}, "m"))
    worker.goal_keeper_once(wait=True)
    assert "goal-keeper-run" in closed_by, "the batch thread gives its own connection back"
    assert any(name.startswith("goal-keeper_") for name in closed_by), "and so does each update thread"


def test_schema_126_leaves_active_goals_and_their_running_clocks_alone(owner):
    from lib import db_migrations
    plan = _goal()
    _step(plan, "harness", "in_progress")
    item_id = _item(plan, "harness")["item_id"]
    started = db.now_ms() - 3_600_000
    db.conn().execute("UPDATE task_items SET started_at=?, active_ms=5 WHERE item_id=?", (started, item_id))
    db_migrations._migrate_to_v126(db.conn())
    row = db.conn().execute("SELECT started_at, active_ms FROM task_items WHERE item_id=?", (item_id,)).fetchone()
    assert (row[0], row[1]) == (started, 5), "a running step of an active goal keeps its clock"
    assert task_plans.get(plan["plan_id"])["status"] == "active"


# ---- a receipt for every action (Peter, 14 Nov) -------------------------------

from lib import janitor_store  # noqa: E402


def _activity(owner, plan, text="More work", ago_ms=400_000):
    _message(owner, "assistant", text, at=db.now_ms() - ago_ms, phase="final")
    db.conn().execute("UPDATE task_plans SET created_at=created_at-500000 WHERE plan_id=?", (plan["plan_id"],))


def _batch(plan, answer, *, usage=None, model="gemini-4-hydrogen-low", run_id="run-1"):
    config = {"agent_id": "keeper-1", "model": model, "options": {"max_goals_per_run": 2}}

    def ask(prompt, plan_id):
        if isinstance(answer, Exception):
            raise answer
        return answer, (usage if usage is not None else {"input_tokens": 1234, "output_tokens": 56}), model

    goal_keeper.run_batch(config, {"run_id": run_id}, [plan["plan_id"]], ask, now=db.now_ms())
    return janitor_store.goal_keeper_receipts_for_run(run_id)


def _short_id(plan, stable):
    return goal_keeper._short(plan["plan_id"], _item(plan, stable)["item_id"])


def test_an_update_leaves_a_receipt_with_steps_sources_model_tokens_and_time(owner, helper):
    plan = _goal()
    _activity(owner, plan, "Harness built: screenshots in /var/tmp/shots")
    [receipt] = _batch(plan, {"steps": {_short_id(plan, "harness"): {
        "status": "completed", "evidence": "Harness built: screenshots in /var/tmp/shots"}},
        "checkpoint": {"progress": "Harness done", "next_work": "Baseline round"}})
    assert receipt["outcome"] == "updated" and receipt["plan_id"] == plan["plan_id"]
    assert receipt["title"] == "Booking UI hill-climb"
    assert receipt["steps"] == [{"item_id": _item(plan, "harness")["item_id"], "from": "pending",
                                 "to": "completed", "note": receipt["steps"][0]["note"]}]
    assert receipt["steps"][0]["note"].startswith("Evidence:")
    assert receipt["progress"] == "Harness done" and receipt["next_work"] == "Baseline round"
    assert receipt["model"] == "gemini-4-hydrogen-low"
    assert (receipt["input_tokens"], receipt["output_tokens"]) == (1234, 56)
    assert isinstance(receipt["duration_ms"], int) and receipt["error"] == ""
    assert any(ref.startswith("message:") for ref in receipt["source_refs"]), "it names what it read"
    # The ledger's own receipt of the write now carries those sources too.
    refs = json.loads(db.conn().execute(
        "SELECT source_refs FROM goal_events WHERE plan_id=? AND kind='updater'",
        (plan["plan_id"],)).fetchone()[0])
    assert refs and refs == receipt["source_refs"][:20]
    assert task_plans.get(plan["plan_id"])["updater"]["last_receipt"]["outcome"] == "updated"


def test_nothing_new_to_say_leaves_an_unchanged_receipt(owner, helper):
    plan = _goal()
    answer = {"steps": {}, "checkpoint": {"progress": "Still waiting on Peter", "next_work": "Wait"}}
    _activity(owner, plan, "Asked Peter")
    _batch(plan, answer, run_id="run-1")
    _activity(owner, plan, "Still no answer", ago_ms=-1000)  # after the first update's cursor
    [receipt] = _batch(plan, answer, run_id="run-2")
    assert receipt["outcome"] == "unchanged" and receipt["steps"] == []


def test_a_completed_claim_without_evidence_is_held_and_says_so(owner, helper):
    plan = _goal()
    _step(plan, "harness", "in_progress")
    _activity(owner, plan, "I think the harness is done")
    [receipt] = _batch(plan, {"steps": {_short_id(plan, "harness"): {"status": "completed"}},
                              "checkpoint": {"progress": "Harness probably done", "next_work": "Check"}})
    assert receipt["outcome"] == "held_by_evidence_rule"
    assert receipt["steps"] == [{"item_id": _item(plan, "harness")["item_id"], "from": None, "to": "held",
                                 "note": "Completed claimed without evidence; left in progress"}]
    assert _item(plan, "harness")["status"] == "in_progress"


def test_a_write_refused_because_the_owner_changed_the_plan_leaves_a_receipt(owner, helper, monkeypatch):
    plan = _goal()
    _activity(owner, plan)

    def refused(*args, **kwargs):
        raise ValueError("plan changed; reload before editing")

    monkeypatch.setattr(task_plans, "updater_apply", refused)
    [receipt] = _batch(plan, {"steps": {}, "checkpoint": {"progress": "p", "next_work": "n"}})
    assert (receipt["outcome"], receipt["error"]) == ("refused_owner_changed", "owner_changed_plan")
    assert receipt["source_refs"], "what it had read is still on the receipt"


@pytest.mark.parametrize("answer", [RuntimeError("agy exited 1"), {"steps": {}}])
def test_a_model_failure_or_a_bad_answer_leaves_a_failed_receipt(owner, helper, answer):
    plan = _goal()
    _activity(owner, plan)
    [receipt] = _batch(plan, answer)
    assert receipt["outcome"] == "failed" and receipt["error"]
    assert receipt["title"] == "Booking UI hill-climb" and receipt["model"] == "gemini-4-hydrogen-low"


def test_a_goal_with_nothing_new_is_skipped_with_a_receipt(owner):
    plan = _goal()
    [receipt] = _batch(plan, {"steps": {}, "checkpoint": {"progress": "p", "next_work": "n"}})
    assert (receipt["outcome"], receipt["reason"]) == ("skipped", "nothing new")
    assert receipt["input_tokens"] is None, "no model call, no tokens"


def test_receipts_persist_per_goal_and_show_in_the_keepers_run_history(owner, helper):
    keeper = _keeper()
    janitors.set_enabled(keeper["session"], keeper["revision"], True)
    plan = _goal()
    _activity(owner, plan, "Started")

    def ask(run, prompt, plan_id):
        return ({"steps": {}, "checkpoint": {"progress": "Helper started", "next_work": "Wait"}},
                {"input_tokens": 10, "output_tokens": 2}, "gemini-4-hydrogen-low")

    janitor_autonomy.AutonomyJanitors(dispatch=None, notify=None, goal_ask=ask).goal_keeper_once(wait=True)
    [run] = janitors.list_runs(keeper["session"], limit=1)
    assert [r["plan_id"] for r in run["receipts"]] == [plan["plan_id"]]
    assert run["receipts"][0]["run_id"] == run["run_id"]
    stored = janitor_store.goal_keeper_receipts_for_plan(plan["plan_id"])
    assert [r["receipt_id"] for r in stored] == [run["receipts"][0]["receipt_id"]]
    assert "prompt" not in stored[0] and "Started" not in json.dumps(stored[0]), "ids, not transcript text"


def test_a_goal_keeps_its_newest_receipts_only(owner, monkeypatch):
    monkeypatch.setattr(janitor_store, "GOAL_KEEPER_RECEIPTS_PER_GOAL", 3)
    plan = _goal()
    for n in range(5):
        _batch(plan, {"steps": {}, "checkpoint": {"progress": "p", "next_work": "n"}}, run_id=f"run-{n}")
    kept = janitor_store.goal_keeper_receipts_for_plan(plan["plan_id"], limit=10)
    assert [r["run_id"] for r in kept] == ["run-4", "run-3", "run-2"]


# ---- review of PR #326 (issue #311): receipts can't be lost or leak text ------


def test_a_receipt_that_cannot_be_stored_rolls_the_goal_write_back(owner, helper, monkeypatch):
    import sqlite3
    plan = _goal()
    _activity(owner, plan, "Harness built")
    real_insert, calls = janitor_store.insert_goal_keeper_receipt, []

    def flaky(c, receipt):
        calls.append(receipt["outcome"])
        if len(calls) == 1:  # the write's own receipt, inside updater_apply's transaction
            raise sqlite3.OperationalError("database is locked")
        return real_insert(c, receipt)

    monkeypatch.setattr(janitor_store, "insert_goal_keeper_receipt", flaky)
    [receipt] = _batch(plan, {"steps": {_short_id(plan, "harness"): {
        "status": "completed", "evidence": "Harness built"}},
        "checkpoint": {"progress": "Harness done", "next_work": "Baseline"}})
    assert _item(plan, "harness")["status"] == "pending", "no committed keeper write without its receipt"
    assert "updater" not in task_plans.get(plan["plan_id"])["goal"]
    assert db.conn().execute("SELECT count(*) FROM goal_events WHERE plan_id=? AND kind='updater'",
                             (plan["plan_id"],)).fetchone()[0] == 0
    assert (receipt["outcome"], receipt["error"]) == ("failed", "database_locked")
    assert (receipt["progress"], receipt["next_work"]) == ("", ""), "nothing was written"


def test_a_lost_receipt_leaves_a_logged_line_after_bounded_retries(owner, monkeypatch):
    plan = _goal()
    logged, attempts = [], []
    monkeypatch.setattr(goal_keeper, "RECEIPT_RETRY_DELAYS", (0.0, 0.0))

    def always_fail(c, receipt):
        attempts.append(1)
        raise RuntimeError("disk full")

    monkeypatch.setattr(janitor_store, "insert_goal_keeper_receipt", always_fail)
    monkeypatch.setattr(goal_keeper, "log_exception", lambda event, exc, detail="": logged.append((event, detail)))
    assert _batch(plan, {"steps": {}, "checkpoint": {"progress": "p", "next_work": "n"}}, run_id="run-x") == []
    assert len(attempts) == 3, "one try and two bounded retries"
    assert ("goalKeeperReceiptLost", f"run=run-x plan={plan['plan_id']} outcome=skipped") in logged


@pytest.mark.parametrize("case", ["refused", "cancelled", "closed"])
def test_a_receipt_for_a_goal_not_written_carries_no_checkpoint_text(owner, helper, monkeypatch, case):
    plan = _goal()
    _activity(owner, plan)
    if case == "refused":
        monkeypatch.setattr(task_plans, "updater_apply",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("plan changed; reload before editing")))
    if case == "closed":
        _act(plan["plan_id"], "cancel", reason="Not needed")
    config = {"agent_id": "keeper-1", "model": "m", "options": {"max_goals_per_run": 1}}
    answer = {"steps": {}, "checkpoint": {"progress": "PROPOSED TEXT", "next_work": "PROPOSED NEXT"}}
    goal_keeper.run_batch(config, {"run_id": "run-1"}, [plan["plan_id"]],
                          lambda prompt, plan_id: (answer, {}, "m"), now=db.now_ms(),
                          current=(lambda: False) if case == "cancelled" else (lambda: True))
    [receipt] = janitor_store.goal_keeper_receipts_for_run("run-1")
    assert receipt["outcome"] == {"refused": "refused_owner_changed", "cancelled": "cancelled",
                                  "closed": "skipped"}[case]
    assert (receipt["progress"], receipt["next_work"]) == ("", "")
    assert "PROPOSED" not in json.dumps(receipt)


@pytest.mark.parametrize("failure,code", [
    (RuntimeError("agy exited 1: Quota exceeded for project 1234, see https://… long provider text"), "agy_exit_1"),
    (TimeoutError("agy did not answer within 150 s"), "timeout"),
    ({"steps": {}}, "parse_error"),
    (KeyError("weird"), "KeyError"),
])
def test_receipt_and_plan_errors_are_short_codes_never_provider_text(owner, helper, failure, code):
    plan = _goal()
    _activity(owner, plan)
    [receipt] = _batch(plan, failure)
    assert receipt["error"] == code
    shown = task_plans.get(plan["plan_id"])["updater"]
    assert shown["last_receipt"]["error"] == code
    assert "provider text" not in json.dumps(shown) and "Quota" not in json.dumps(shown)


def test_receipt_fields_are_cut_to_their_lengths(owner, helper):
    plan = task_plans.create(
        session="jonatan", title="T" * 400, items=[{"id": "harness", "title": "Harness"}],
        goal={"criteria": ["Done"], "limits": "None", "enroll": True})
    _activity(owner, plan)
    [receipt] = _batch(plan, {"steps": {_short_id(plan, "harness"): {
        "status": "in_progress", "note": "n" * 2000}},
        "checkpoint": {"progress": "p" * 3000, "next_work": "w" * 3000}})
    assert receipt["outcome"] == "updated"
    assert len(receipt["title"]) == 200
    assert (len(receipt["progress"]), len(receipt["next_work"])) == (500, 300)
    assert len(receipt["steps"][0]["note"]) == 300


def test_the_per_goal_cap_deletes_the_oldest_receipts_in_the_store(owner):
    plan = _goal()
    c = db.conn()
    for n in range(janitor_store.GOAL_KEEPER_RECEIPTS_PER_GOAL + 5):
        janitor_store.insert_goal_keeper_receipt(c, {"receipt_id": f"r{n:04d}", "run_id": f"run-{n}",
                                                     "plan_id": plan["plan_id"], "outcome": "skipped",
                                                     "created_at": 1_000 + n})
    kept = c.execute("SELECT count(*), min(created_at) FROM goal_keeper_receipts WHERE plan_id=?",
                     (plan["plan_id"],)).fetchone()
    assert tuple(kept) == (janitor_store.GOAL_KEEPER_RECEIPTS_PER_GOAL, 1_005)
