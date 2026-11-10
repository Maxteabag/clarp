"""AUT-N1b-reader: goal history details may be stored as references to the
append-only goal ledger, which mirrors every entry under "history:<index>"
({"ledger": key, "shape": key order}). This build only READS them: get()
expands every reference back, byte-identical; nothing writes them yet (the
writer ships one release later). The state.sqlite rule: compatible one
release in both directions, so a Host rolled back to this build reads what
the writer stored. The golden was captured on 21ebde7c, before any change
(UPDATE_GOAL_PAYLOAD_GOLDEN=1 rewrites it; review the diff)."""
from __future__ import annotations

import json
import os
import pathlib

from lib import agents, db, goal_ledger as ledger, task_goal_state as goals, task_plans

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "goal_payload" / "replanned_goal.json"
STEPS = 300


def _replanned_goal(tmp_path, monkeypatch):
    clock = [1_800_000_000_000]

    def tick():
        clock[0] += 1
        return clock[0]

    monkeypatch.setattr(db, "now_ms", tick)
    agent_id = agents.create_agent(persona="Pip", voice_id="v", cwd=str(tmp_path), session="pip")
    agents.start_runtime(agent_id, "pip")
    with ledger.acting_as("owner"):
        plan = task_plans.create(
            session="pip", title="Ship the importer",
            items=[{"id": f"s{n}", "title": f"Step {n}", "detail": "x" * 200} for n in range(STEPS)],
            goal={"criteria": ["the importer ships"], "limits": "No deploys", "enroll": False})
        for round_ in range(2):
            plan = goals.mutate(plan["plan_id"], revision=plan["revision"], action="replan", data={
                "reason": f"round {round_}",
                "steps": [{"id": f"r{round_}-{n}", "title": f"Round {round_} step {n}",
                           "detail": "y" * 200} for n in range(STEPS)]})
    return plan["plan_id"]


def golden_payload(plan_id):
    """json.dumps(task_plans.get()), with what differs run to run normalized:
    generated identities, and the order of 'counts' (built from a set, so it
    varies with the hash seed in production too)."""
    plan = task_plans.get(plan_id)
    plan["counts"] = dict(sorted(plan["counts"].items()))
    return (json.dumps(plan, ensure_ascii=False)
            .replace(plan_id, "<plan>").replace(plan["agent_id"], "<agent>"))


def assert_golden(payload):
    if os.environ.get("UPDATE_GOAL_PAYLOAD_GOLDEN") == "1":
        GOLDEN.write_text(payload, encoding="utf-8")
    assert payload == GOLDEN.read_text(encoding="utf-8")


def _writer_shape(value):
    """The writer's key-order record: {"k": keys, "c": {key: shape}} where a
    dict's keys are not sorted, {"a": shape} for a list of like items, else
    {"l": [...]}; None when sorted all the way down."""
    if isinstance(value, dict):
        keys = list(value)
        kids = {key: shape for key in keys if (shape := _writer_shape(value[key])) is not None}
        if keys == sorted(keys) and not kids:
            return None
        return {"k": keys, **({"c": kids} if kids else {})}
    if isinstance(value, list):
        shapes = [_writer_shape(item) for item in value]
        if all(shape is None for shape in shapes):
            return None
        return {"a": shapes[0]} if all(shape == shapes[0] for shape in shapes) else {"l": shapes}
    return None


def _store_as_the_writer_would(plan_id):
    """Rewrite the stored goal in the writer's format: every large detail as a
    reference to its ledger row."""
    con = db.conn()
    goal = json.loads(con.execute("SELECT goal_json FROM task_plans WHERE plan_id=?",
                                  (plan_id,)).fetchone()[0])
    offset = int(goal.get("history_offset") or 0)
    for position, entry in enumerate(goal["history"]):
        if "ledger" in entry["detail"] or len(json.dumps(entry["detail"], ensure_ascii=False)) <= 1024:
            continue    # small, or already a reference (a build with the writer)
        key = f"history:{offset + position}"
        stub = {"ledger": key}
        if (shape := _writer_shape(entry["detail"])) is not None:
            stub["shape"] = shape
        entry["detail"] = stub
    con.execute("UPDATE task_plans SET goal_json=? WHERE plan_id=?",
                (json.dumps(goal, ensure_ascii=False), plan_id))
    return sum("ledger" in entry["detail"] for entry in goal["history"])


def test_a_goal_in_the_old_format_reads_as_before(tmp_path, monkeypatch):
    plan_id = _replanned_goal(tmp_path, monkeypatch)
    assert_golden(golden_payload(plan_id))


def test_a_goal_the_writer_stored_with_references_reads_back_identical(tmp_path, monkeypatch):
    """The rollback case: a document written by the writer build, read by this one."""
    plan_id = _replanned_goal(tmp_path, monkeypatch)
    assert _store_as_the_writer_would(plan_id) == 4     # 2 retired + 2 proposed step lists
    assert_golden(golden_payload(plan_id))


def test_expanded_history_keeps_the_artifact_summary_encoding_bound(tmp_path, monkeypatch):
    """Reviewer B: the artifact summary bounds the history it serializes by
    the stored goal text. A document with references is small while its
    history is not; the bound must cover what get() expands, so a summary
    with too little budget leaves the digest pending without encoding it."""
    from lib import artifacts
    plan_id = _replanned_goal(tmp_path, monkeypatch)
    inline, inline_text, inline_bound = task_plans.get_with_goal_text_and_bound(plan_id)
    inline_digest = artifacts._history_marker(
        artifacts._PlanWithGoalText(inline, inline_text, inline_bound), inline["goal"]["history"],
        inline_text, artifacts.summary_budget(max_bytes=10_000_000))["digest"]
    assert inline_digest
    _store_as_the_writer_would(plan_id)
    artifacts._HISTORY_DIGEST.pop(plan_id)     # a cold cache: the bound decides
    plan, text, bound = task_plans.get_with_goal_text_and_bound(plan_id)
    assert len(text.encode()) < 4096 and bound > 500_000
    wrapped = artifacts._PlanWithGoalText(plan, text, bound)
    history = plan["goal"]["history"]
    encoded = []
    real_dumps = artifacts.json.dumps
    monkeypatch.setattr(artifacts.json, "dumps", lambda value, *a, **k: (
        encoded.append(1) if value is history else None, real_dumps(value, *a, **k))[1])
    small = artifacts._history_marker(wrapped, history, text, artifacts.summary_budget(max_bytes=4096))
    assert small["digest_pending"] is True and encoded == []
    monkeypatch.setattr(artifacts.json, "dumps", real_dumps)
    large = artifacts._history_marker(wrapped, history, text, artifacts.summary_budget(max_bytes=10_000_000))
    assert large["digest"] == inline_digest
