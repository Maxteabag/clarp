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
        raise task_plans.PlanChanged("plan changed; reload before editing")

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
                            lambda *a, **k: (_ for _ in ()).throw(task_plans.PlanChanged("plan changed; reload before editing")))
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


# ---- owner conflicts by exception type, not text; capped re-reads -----------


def test_a_provider_error_quoting_plan_changed_is_a_failure_not_a_conflict(owner):
    quoted = RuntimeError("agy exited 1: the agent said 'plan changed; reload before editing'")
    assert goal_keeper.error_code(quoted) == "agy_exit_1"
    assert goal_keeper.error_code(ValueError("plan changed; reload before editing")) == "ValueError"
    assert goal_keeper.error_code(task_plans.PlanChanged("x")) == "owner_changed_plan"
    assert goal_keeper.error_code(task_plans.GoalClosed("x")) == "goal_closed"

    plan = _goal()
    _message(owner, "assistant", "Turn ended", at=db.now_ms() - 400_000, phase="final")
    db.conn().execute("UPDATE task_plans SET created_at=created_at-500000 WHERE plan_id=?", (plan["plan_id"],))
    calls = []

    def ask(prompt):
        calls.append(1)
        raise RuntimeError("agy exited 1: plan changed; reload before editing")

    config = janitors.get(_keeper()["session"])
    result = goal_keeper.run_batch(config, {"run_id": "r1"}, [plan["plan_id"]],
                                   lambda prompt, plan_id: ask(prompt), now=db.now_ms())
    outcome, code = result["outcomes"][plan["plan_id"]]
    assert (outcome, code) == ("failed", "agy_exit_1"), "retried like any other provider error"
    progress, now = {"next_sweep_at": 10_000_000}, 1_000_000
    goal_keeper.settle(progress, plan["plan_id"], outcome, now, code)
    assert progress["goals"][plan["plan_id"]]["retry_at"] == now + goal_keeper.RETRY_AFTER_MS
    goal_keeper.settle(progress, plan["plan_id"], outcome, now + 1, code)
    assert progress["goals"][plan["plan_id"]]["retry_at"] == 10_000_000, "then only the sweep"


def test_a_real_owner_conflict_is_reread_soon_but_only_a_few_times(owner):
    plan = _goal()
    current = task_plans.get(plan["plan_id"])
    with pytest.raises(task_plans.PlanChanged):
        task_plans.updater_apply(plan["plan_id"], seen_revision=current["revision"] - 1, steps={},
                                 progress="p", next_work="n", model="m", source_refs=[])
    progress, now = {"next_sweep_at": 9_000_000}, 1_000_000
    for i in range(goal_keeper.MAX_CONFLICT_RETRIES):
        goal_keeper.settle(progress, plan["plan_id"], "conflict", now + i, "owner_changed_plan")
        assert progress["goals"][plan["plan_id"]]["retry_at"] == now + i + 15_000
    goal_keeper.settle(progress, plan["plan_id"], "conflict", now + 9, "owner_changed_plan")
    assert progress["goals"][plan["plan_id"]]["retry_at"] == 9_000_000, "capped: waits for the sweep"


# ---- one-off catch-up of goals older than the first look-back (audit, 14 Nov) ---

DAY = 24 * 3600 * 1000


def _aged_goal(owner, *, days_old, last_message_days_ago, text="Built the harness and ran round one",
               role="assistant", phase="final"):
    plan = _goal()
    now = db.now_ms()
    created = now - days_old * DAY
    goal = json.loads(db.conn().execute("SELECT goal_json FROM task_plans WHERE plan_id=?",
                                        (plan["plan_id"],)).fetchone()[0])
    # A goal really created days ago: its default first wake is long past too.
    goal.setdefault("continuation", {})["due_at"] = created + 120_000
    db.conn().execute("UPDATE task_plans SET created_at=?, goal_json=? WHERE plan_id=?",
                      (created, json.dumps(goal), plan["plan_id"]))
    _message(owner, role, text, at=now - int(last_message_days_ago * DAY), phase=phase)
    return plan, now


def _receipts(run_id):
    from lib import janitor_store
    return janitor_store.goal_keeper_receipts_for_run(run_id)


def _row(plan_id):
    return db.conn().execute("SELECT * FROM task_plans WHERE plan_id=?", (plan_id,)).fetchone()


def _full(progress, ready):
    return frozenset(p for p in ready if (progress.get("goals") or {}).get(p, {}).get("catch_up"))


def _turn_ended(agent_id, *, at, kind="interrupted", reason=None):
    db.conn().execute("INSERT INTO state_log(agent_id, runtime_id, ts, kind, detail) VALUES (?,?,?,?,?)",
                      (agent_id, None, at, kind, json.dumps({"reason": reason} if reason else {})))


def test_a_goal_older_than_the_look_back_is_caught_up_once_and_then_left_to_triggers(owner):
    plan, now = _aged_goal(owner, days_old=5, last_message_days_ago=2)
    progress = {}
    config = _config()
    ready = goal_keeper.due(config, progress, now)
    assert ready == [plan["plan_id"]], "queued for its one catch-up"
    assert plan["plan_id"] in progress["caught_up"]
    prompts = []

    def ask(prompt, plan_id):
        prompts.append(prompt)
        return ({"steps": {"harness": {"status": "completed", "note": "Evidence: 'Built the harness'"}},
                 "checkpoint": {"progress": "Harness built, round one run", "next_work": "Round two"}}, {}, "m")

    result = goal_keeper.run_batch(config, {"run_id": "catch-1"}, ready, ask, now=now, full=_full(progress, ready))
    assert result["outcomes"][plan["plan_id"]][0] == "updated"
    assert "Built the harness" in prompts[0], "the 2-day-old activity was read, past the 24 h cap"
    receipt = _receipts("catch-1")[0]
    assert receipt["outcome"] == "updated" and receipt["progress"] == "Harness built, round one run"
    goal_keeper.settle(progress, plan["plan_id"], "updated", now)
    assert goal_keeper.due(config, progress, now + 1000) == [], "idempotent: not queued again"


def test_the_catch_up_is_bounded_and_paced_by_the_queue(owner):
    plans = [_aged_goal(owner, days_old=6, last_message_days_ago=2)[0] for _ in range(5)]
    progress, now = {}, db.now_ms()
    config = _config()
    ready = goal_keeper.due(config, progress, now)
    assert sorted(ready) == sorted(p["plan_id"] for p in plans)
    calls = []

    def ask(prompt, plan_id):
        calls.append(plan_id)
        return ({"steps": {}, "checkpoint": {"progress": "Caught up", "next_work": "n"}}, {}, "m")

    config["options"]["max_goals_per_run"] = 2
    done, runs = set(), 0
    while len(done) < 5 and runs < 5:
        at = now + runs * 20_000
        batch = goal_keeper.due(config, progress, at)[:2]
        result = goal_keeper.run_batch(config, {"run_id": f"pace-{runs}"}, batch, ask, now=at,
                                       full=_full(progress, batch))
        for plan_id, (outcome, _) in result["outcomes"].items():
            goal_keeper.settle(progress, plan_id, outcome, at)
            done.add(plan_id)
        assert len(result["outcomes"]) <= 2, "max_goals_per_run per run"
        runs += 1
    assert done == {p["plan_id"] for p in plans} and runs == 3
    assert sorted(calls) == sorted(done), "each goal read exactly once"
    assert sum(len(_receipts(f"pace-{i}")) for i in range(runs)) == 5, "a receipt for each"
    assert goal_keeper.due(config, progress, now + 200_000) == []


def test_a_failing_catch_up_gets_one_retry_then_only_the_sweep(owner):
    plan, now = _aged_goal(owner, days_old=10, last_message_days_ago=4)
    progress, config, calls = {}, _config(), []

    def failing(prompt, plan_id):
        calls.append(prompt)
        raise RuntimeError("agy exited 1")

    at = now
    for _ in range(8):   # eight passes over about seven hours: sweeps included
        ready = goal_keeper.due(config, progress, at)
        if ready:
            result = goal_keeper.run_batch(config, {"run_id": f"fail-{at}"}, ready, failing, now=at,
                                           full=_full(progress, ready))
            for plan_id, (outcome, code) in result["outcomes"].items():
                goal_keeper.settle(progress, plan_id, outcome, at, code)
        at = max(at + goal_keeper.RETRY_AFTER_MS + 1000, (progress.get("next_sweep_at") or at) + 1000)
    assert len(calls) == 2, "the catch-up attempt and its one retry, then no full-history call"
    assert all("Built the harness" in c for c in calls)


def test_a_paused_goal_is_not_caught_up(owner):
    plan, now = _aged_goal(owner, days_old=10, last_message_days_ago=2)
    _act(plan["plan_id"], "pause", reason="Waiting for Peter")
    progress = {}
    assert goal_keeper.due(_config(), progress, db.now_ms()) == []
    assert plan["plan_id"] not in progress.get("caught_up", {})


# ---- stalled goals: a rule, no model call (audit, 14 Nov; review of #330) --------


def _boom(prompt, plan_id):
    raise AssertionError("a stalled goal with nothing new needs no model call")


def _seeded(owner, *, days_old=10, last_message_days_ago=4):
    """A goal the keeper read while it was still active (so its cursor is past the old activity)."""
    plan, now = _aged_goal(owner, days_old=days_old, last_message_days_ago=last_message_days_ago)
    goal_keeper.run_batch(_config(), {"run_id": "seed"}, [plan["plan_id"]],
                          lambda p, pid: ({"steps": {}, "checkpoint": {"progress": "Round one done", "next_work": "n"}}, {}, "m"),
                          now=now - 3 * DAY, full=frozenset({plan["plan_id"]}))
    assert task_plans.get(plan["plan_id"])["goal"]["updater"]["progress"] == "Round one done"
    return plan, now


def test_a_goal_idle_three_days_is_marked_once_without_a_model_call(owner):
    plan, now = _seeded(owner)
    before = {i["item_id"]: i["status"] for i in task_plans.get(plan["plan_id"])["items"]}
    found = goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now)
    assert found and found[2] == "idle"
    goal_keeper.run_batch(_config(), {"run_id": "stall-1"}, [plan["plan_id"]], _boom, now=now + 1000)
    mine = task_plans.get(plan["plan_id"])["goal"]["updater"]
    assert mine["progress"].startswith("Stalled since ") and "no activity" in mine["progress"]
    assert "days" not in mine["progress"], "no day counter in the line"
    assert {i["item_id"]: i["status"] for i in task_plans.get(plan["plan_id"])["items"]} == before, "no step invented"
    receipt = _receipts("stall-1")[0]
    assert (receipt["outcome"], receipt["reason"], receipt["model"]) == ("stalled", "idle", "rule")
    assert task_plans.get(plan["plan_id"])["status"] == "active", "never paused, cancelled or completed"


def test_the_idle_line_is_not_rewritten_the_next_day(owner):
    plan, now = _seeded(owner)
    goal_keeper.run_batch(_config(), {"run_id": "day-1"}, [plan["plan_id"]], _boom, now=now)
    first = task_plans.get(plan["plan_id"])["goal"]["updater"]
    goal_keeper.run_batch(_config(), {"run_id": "day-2"}, [plan["plan_id"]], _boom, now=now + DAY)
    assert _receipts("day-2")[0]["outcome"] == "skipped"
    assert task_plans.get(plan["plan_id"])["goal"]["updater"] == first, "same line, no new write"
    progress = {"caught_up": {plan["plan_id"]: now}}
    config = _config()
    goal_keeper.due(config, progress, now + DAY)
    assert goal_keeper.due(config, progress, progress["next_sweep_at"] + 1000) == [], "the sweep has nothing to mark"


def test_a_turn_that_mentions_a_rate_limit_but_ended_normally_is_not_marked(owner):
    plan, now = _aged_goal(owner, days_old=2, last_message_days_ago=0.1,
                           text="Done. The API error and the rate limit from yesterday are fixed.")
    _turn_ended(owner, at=now - 2 * 3600_000, kind="idle")
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now) is None


def test_a_turn_the_host_classified_usage_limit_is_marked_after_an_hour(owner):
    plan, now = _aged_goal(owner, days_old=2, last_message_days_ago=0.1, text="Working on round two")
    _turn_ended(owner, at=now - 2 * 3600_000, reason="usage_limit")
    found = goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now)
    assert found and found[2] == "usage_limit" and "usage limit" in found[1]
    _turn_ended(owner, at=now - 10 * MIN, reason="usage_limit")
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now) is None, "an hour's grace first"
    _turn_ended(owner, at=now - 2 * 3600_000 + 1, reason="server_restart")
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now) is None, "a restart is not a stall"


def test_only_the_owners_own_last_turn_can_mark(owner, helper):
    plan, now = _aged_goal(owner, days_old=2, last_message_days_ago=0.1, text="Waiting for the designer")
    _turn_ended(helper, at=now - 2 * 3600_000, reason="usage_limit")
    _turn_ended(owner, at=now - 3 * 3600_000, kind="idle")
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now) is None, "a helper's limit stop is not the owner's"
    _message(owner, "user", "[from designer] I hit my usage limit", at=now - 2 * 3600_000)
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now) is None, "a quoted limit is not a turn end"


def test_a_goal_waiting_on_a_timer_dependency_or_running_job_is_not_stalled(owner):
    plan, now = _aged_goal(owner, days_old=10, last_message_days_ago=5)
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now), "idle without anything to wait on"
    _act(plan["plan_id"], "checkpoint", progress="Waiting for the reviewer", next_work="Merge",
         continuation={"kind": "timer", "due_at": now + 10 * DAY})
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now) is None, "a timer ten days out"
    goal = json.loads(_row(plan["plan_id"])["goal_json"])
    goal["continuation"] = {"state": "waiting", "reason": "Waiting for build", "dependency_key": "k"}
    db.conn().execute("UPDATE task_plans SET goal_json=? WHERE plan_id=?", (json.dumps(goal), plan["plan_id"]))
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now) is None, "waiting on a dependency"
    goal["continuation"] = {"state": "ready", "reason": ""}
    db.conn().execute("UPDATE task_plans SET goal_json=? WHERE plan_id=?", (json.dumps(goal), plan["plan_id"]))
    cols = [r[1] for r in db.conn().execute("PRAGMA table_info(background_jobs)")]
    values = {"job_id": "job-1", "agent_id": owner, "session": "jonatan", "kind": "build", "title": "Long build", "status": "running", "updated_at": now - DAY,
              "started_at": now - 5 * DAY}
    db.conn().execute(f"INSERT INTO background_jobs({','.join(k for k in values if k in cols)}) VALUES "
                      f"({','.join('?' for k in values if k in cols)})", [v for k, v in values.items() if k in cols])
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now) is None, "a job still running"


def test_an_active_goal_with_recent_work_is_not_stalled(owner):
    plan, now = _aged_goal(owner, days_old=5, last_message_days_ago=0.5, text="Round three judged")
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now) is None


def test_a_paused_goal_is_never_marked_stalled(owner):
    plan, now = _aged_goal(owner, days_old=10, last_message_days_ago=5)
    _act(plan["plan_id"], "pause", reason="Waiting for Peter")
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), db.now_ms()) is None


def test_the_hourly_sweep_queues_a_stalled_goal_not_yet_marked(owner):
    plan, now = _seeded(owner)
    progress = {"caught_up": {plan["plan_id"]: now}}
    config = _config()
    assert goal_keeper.due(config, progress, now) == []
    assert goal_keeper.due(config, progress, progress["next_sweep_at"] + 1000) == [plan["plan_id"]]


def test_a_stalled_goal_with_new_text_keeps_the_model_update_and_says_it_stalled(owner):
    plan, now = _aged_goal(owner, days_old=10, last_message_days_ago=4)
    goal_keeper.run_batch(_config(), {"run_id": "both"}, [plan["plan_id"]],
                          lambda p, pid: ({"steps": {}, "checkpoint": {"progress": "Round one done", "next_work": "n"}}, {}, "m"),
                          now=now, full=frozenset({plan["plan_id"]}))
    progress = task_plans.get(plan["plan_id"])["goal"]["updater"]["progress"]
    assert progress.startswith("Stalled since ") and progress.endswith("Round one done")


# ---- what the 15-second check reads ---------------------------------------------


def test_the_check_for_new_messages_reads_only_the_new_ones(owner, helper):
    """The keeper asks every 15 seconds, for every open goal, which messages of
    its agents changed since its last look. No index starts with (agent,
    updated_at), so the plain condition walked every message those agents ever
    had: about a gigabyte of pages a pass on a Host with two million messages.
    The rows are found through an index that holds ``updated_at`` and only
    those are read."""
    plan_rows = []
    for sql in (f"SELECT agent_id, role, phase, sender_agent_id, updated_at FROM messages "
                f"WHERE {goal_keeper._rows_since('?,?')} ORDER BY updated_at",
                f"SELECT message_id, agent_id, timestamp, role, text, tools_json, origin, sender_agent_id "
                f"FROM messages WHERE {goal_keeper._rows_since('?,?', until=True)} ORDER BY timestamp, seq"):
        steps = [row[3] for row in db.conn().execute(
            "EXPLAIN QUERY PLAN " + sql, (owner, helper, 0) + ((1,) if "<= ?" in sql else ()))]
        plan_rows.append(steps)
        searches = [step for step in steps if "messages" in step and ("SEARCH" in step or "SCAN" in step)]
        assert any("COVERING INDEX" in step for step in searches), steps
        assert all("COVERING INDEX" in step or "INTEGER PRIMARY KEY" in step for step in searches), steps


def test_the_rewritten_condition_finds_the_rows_the_plain_one_found(owner, helper):
    base = db.now_ms() - 10_000_000
    for i in range(60):
        _message(owner if i % 3 else helper, "assistant" if i % 2 else "user", f"m{i}",
                 at=base + i * 1000, phase="final" if i % 4 == 1 else None,
                 sender=helper if i % 5 == 0 else None)
    key = lambda rows: sorted(repr(tuple(row)) for row in rows)
    for since in (0, base + 20_500, base + 59_000, base + 10_000_000):
        plain = db.conn().execute(
            "SELECT agent_id, role, phase, sender_agent_id, updated_at FROM messages "
            "WHERE agent_id IN (?,?) AND updated_at > ?", (owner, helper, since)).fetchall()
        found = db.conn().execute(
            f"SELECT agent_id, role, phase, sender_agent_id, updated_at FROM messages "
            f"WHERE {goal_keeper._rows_since('?,?')}", (owner, helper, since)).fetchall()
        assert key(found) == key(plain)
        until = base + 40_000
        plain = db.conn().execute(
            "SELECT message_id FROM messages WHERE agent_id IN (?,?) AND updated_at > ? "
            "AND updated_at <= ?", (owner, helper, since, until)).fetchall()
        found = db.conn().execute(
            f"SELECT message_id FROM messages WHERE {goal_keeper._rows_since('?,?', until=True)}",
            (owner, helper, since, until)).fetchall()
        assert key(found) == key(plain)
    assert len(db.conn().execute("SELECT 1 FROM messages WHERE agent_id IN (?,?)", (owner, helper)).fetchall()) == 60

# ---- review leftovers (#330, #311, #328) -------------------------------------


def test_a_turn_the_host_classified_as_a_lost_connection_is_a_provider_error(owner):
    plan, now = _aged_goal(owner, days_old=2, last_message_days_ago=0.1, text="Working on round two")
    _turn_ended(owner, at=now - 2 * 3600_000, reason="connection")
    found = goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now)
    assert found and found[2] == "provider_error"


def _set_continuation(plan_id, continuation):
    goal = json.loads(_row(plan_id)["goal_json"])
    goal["continuation"] = continuation
    db.conn().execute("UPDATE task_plans SET goal_json=? WHERE plan_id=?", (json.dumps(goal), plan_id))


def test_a_dependency_past_its_deadline_is_treated_like_an_overdue_timer(owner):
    # Goal recovery normally moves such a goal on at its deadline ('External dependency
    # deadline passed', tests/integration/test_task_goal_recovery.py); one still waiting
    # past it is one recovery does not visit, and after the idle days it is idle.
    plan, now = _aged_goal(owner, days_old=10, last_message_days_ago=5)
    waiting = {"state": "waiting", "reason": "Waiting for build", "dependency_key": "k"}
    _set_continuation(plan["plan_id"], {**waiting, "due_at": now + DAY})
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now) is None, "deadline still ahead"
    _set_continuation(plan["plan_id"], {**waiting, "due_at": now - 4 * DAY})
    found = goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now)
    assert found and found[2] == "idle", "deadline passed, idle for days"
    _set_continuation(plan["plan_id"], waiting)
    assert goal_keeper.stall(db.conn(), _row(plan["plan_id"]), now) is None, "no deadline: still a wait"
    plan2, now2 = _aged_goal(owner, days_old=10, last_message_days_ago=1)
    _set_continuation(plan2["plan_id"], {**waiting, "due_at": now2 - 4 * DAY})
    assert goal_keeper.stall(db.conn(), _row(plan2["plan_id"]), now2) is None, "recent work: not idle"


def test_only_goals_flagged_for_catch_up_are_read_in_full(owner):
    flagged, now = _aged_goal(owner, days_old=5, last_message_days_ago=2, text="OLD-FLAGGED harness built")
    other, _ = _aged_goal(owner, days_old=5, last_message_days_ago=2, text="OLD-OTHER harness built")
    _message(owner, "assistant", "NEW turn ended", at=now - 10 * MIN, phase="final")
    prompts = {}

    def ask(prompt, plan_id):
        prompts[plan_id] = prompt
        return ({"steps": {}, "checkpoint": {"progress": "p", "next_work": "n"}}, {}, "m")

    goal_keeper.run_batch(_config(), {"run_id": "full-1"}, [flagged["plan_id"], other["plan_id"]], ask,
                          now=now, full=frozenset({flagged["plan_id"]}))
    assert "OLD-FLAGGED" in prompts[flagged["plan_id"]], "the flagged goal is read from its start"
    assert "OLD-OTHER" not in prompts[other["plan_id"]] and "OLD-FLAGGED" not in prompts[other["plan_id"]], \
        "the other only its recent delta"
    assert "NEW turn ended" in prompts[other["plan_id"]]


def test_the_store_refuses_the_keepers_write_to_a_closed_goal_with_goal_closed(owner):
    plan = _goal()
    current = task_plans.get(plan["plan_id"])
    _act(plan["plan_id"], "cancel", reason="Done elsewhere")
    with pytest.raises(task_plans.GoalClosed):
        task_plans.updater_apply(plan["plan_id"], seen_revision=current["revision"], steps={},
                                 progress="p", next_work="n", model="m", source_refs=[])


def test_a_real_agy_timeout_reads_timeout(monkeypatch):
    from lib import backends, model_fallbacks

    class Agy:
        supports_routing = True
        required_binary = "sh"

        @staticmethod
        def isolated_oneshot(prompt, *, model, home_name):
            raise TimeoutError("agy did not answer within 150 s")

    monkeypatch.setattr(backends, "get", lambda provider: Agy)
    monkeypatch.setattr(janitor_builtins, "is_current", lambda run_id: True)
    monkeypatch.setattr(model_fallbacks, "execute", lambda agent_id, request_id, primary, invoke, **kw: invoke(primary))
    run = {"run_id": "t-1", "agent_id": "keeper", "configuration": {
        "provider": "agy", "model": "gemini-4-hydrogen-low", "effort": "", "isolated": True,
        "template_id": "goal-keeper"}}
    with pytest.raises(model_fallbacks.ProviderFailure) as caught:
        janitor_autonomy.model_json(run, "prompt")
    assert caught.value.category == "timeout"
    assert goal_keeper.error_code(caught.value) == "timeout", "was ProviderFailure"
    assert goal_keeper.error_code(model_fallbacks.ProviderFailure("something else")) == "ProviderFailure"


def test_delta_reads_messages_in_timestamp_then_seq_order(owner):
    """Rows inserted out of order come back by (timestamp, seq): the ORDER BY is
    what keeps the model's view chronological (review of PR 333)."""
    base = db.now_ms() - 600_000
    rows = [  # (message_id, minute offset, seq), inserted in this deliberately wrong order
        ("m-late", 5, 1), ("m-early", 1, 2), ("m-same-b", 3, 9), ("m-same-a", 3, 4)]
    for message_id, minute, seq in rows:
        at = base + minute * 60_000
        stamp = datetime.fromtimestamp(at / 1000, timezone.utc).isoformat()
        db.conn().execute(
            "INSERT INTO messages(message_id,agent_id,backend_session_id,seq,role,timestamp,text,"
            "tools_json,updated_at,origin,sender_agent_id,phase) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, owner, "s-order", seq, "assistant", stamp, "text " + message_id, "[]", at,
             "user", None, "final"))
    _text, refs = goal_keeper.delta(db.conn(), [owner], {owner: "Jonatan"}, base - 1, db.now_ms())
    assert refs == ["message:m-early", "message:m-same-a", "message:m-same-b", "message:m-late"]
