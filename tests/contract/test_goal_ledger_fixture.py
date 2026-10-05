"""The frozen goal-ledger fixture (contract/fixtures/goal-ledger.json) is what
the Host actually emits.

One representative scenario runs through the real store: an owner goal with a
checkpoint, a method change, subgoals (one retired), a user pause and resume,
a bookkeeping delegate whose listener wakes it, its observations, claims,
discrepancy and unknown, activity it has not booked yet, a system wake claim
and a dependency wait. Ids and the clock are pinned, so the output must equal
the fixture exactly. Regenerate with CLARP_UPDATE_FIXTURES=1 after an
intended, additive change, and tell the native clients.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = REPO / "contract/fixtures/goal-ledger.json"
SCHEMA = REPO / "contract/schemas/goal-ledger.json"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from schema_check import validate  # noqa: E402

from lib import (agents, bookkeeping_listener as listener, db,  # noqa: E402
                 goal_ledger as ledger, message_store, task_goal_recovery as recovery,
                 task_goal_state as goals, task_plans)

START = 1_791_160_000_000


def _scenario(monkeypatch, tmp_path) -> dict:
    clock = iter(range(START, START + 10**9, 1000))
    monkeypatch.setattr(db, "now_ms", lambda: next(clock))

    def agent(session, persona, parent=None):
        agent_id = agents.create_agent(persona=persona, voice_id="v", cwd=str(tmp_path), session=session)
        if parent:
            agents.set_lineage(agent_id, parent_agent_id=parent, role="helper")
        return agents.get_by_agent_id(agent_id)

    pip = agent("pip", "Pip")
    agents.start_runtime(pip["agent_id"], "pip")
    agents.bind_backend_session(pip["agent_id"], "native-pip")
    accountant = agent("pip-accountant", "Pip Accountant", parent=pip["agent_id"])

    def say(text, client, role="user"):
        if role == "user":
            return message_store.record_user_message(
                agent_id=pip["agent_id"], backend_session_id="native-pip",
                client_msg_id=client, text=text)
        return agents.upsert_live_assistant_message(
            agent_id=pip["agent_id"], backend_session_id="native-pip", trace_id=client, text=text)

    def act(action, data, actor="owner"):
        nonlocal plan
        with (ledger.acting_as("owner", pip["agent_id"], verified=True) if actor == "owner"
              else ledger.acting_as(actor)):
            plan = goals.mutate(plan["plan_id"], revision=plan["revision"], action=action, data=data)

    with ledger.acting_as("owner", pip["agent_id"], verified=True):
        plan = task_plans.create(
            session="pip", title="Number Ninja logs events", plan_id="number-ninja",
            items=[{"id": "v5", "title": "Publish v5 with draft-key events"},
                   {"id": "report", "title": "Report to Peter"}],
            goal={"outcome": "Peter's Number Ninja form (v5) logs events live to the Host",
                  "criteria": ["form-events for v5 returns real events from Peter's play",
                               "Peter has the corrected status without asking"],
                  "limits": "No tests, probes or redeploys", "enroll": True})
    say("Make the game log every answer", "peter-asks")
    act("checkpoint", {"progress": "v5 published with draft-key capture",
                       "next_work": "Wait for Peter to play on build 2741",
                       "evidence": {"criterion-2": "Told Peter build 2741 is required"}})
    act("replan", {"reason": "v6 logs with clarpForm.log directly; v5 draft keys duplicate it",
                   "steps": [{"id": "v6", "title": "Publish v6 with clarpForm.log"},
                             {"id": "report", "title": "Report to Peter"}]})
    ledger.owner_subgoal(plan["plan_id"], "add", {
        "subgoal_id": "phone", "title": "Events arrive from Peter's phone",
        "intent": "The original acceptance is real play on his phone",
        "criteria": ["Build 2741 or later is installed", "form-events returns his rows"]})
    ledger.owner_subgoal(plan["plan_id"], "add", {
        "subgoal_id": "v5-draft", "title": "v5 draft-key capture", "intent": "First logging path"})
    ledger.owner_subgoal(plan["plan_id"], "update", {
        "subgoal_id": "v5-draft", "expected_revision": 1, "fields": {"status": "retired"},
        "reason": "Replaced by v6 clarpForm.log"})
    ledger.owner_subgoal(plan["plan_id"], "update", {
        "subgoal_id": "phone", "expected_revision": 1,
        "fields": {"current_action": "Waiting for Peter to install 2741 and play v6",
                   "next_dependency": "Peter updates Clarp on his phone"},
        "reason": "Install not verified"})
    act("pause", {"reason": "User paused this goal"}, actor="user")
    act("resume", {"reason": "User explicitly resumed this goal"}, actor="user")

    delegation = ledger.enable(pip, accountant, "Pilot bookkeeping for Pip", baseline_messages=200)
    d = delegation["delegation_id"]
    token = ledger.read_token(d)
    wakes = []
    pending = listener.Pending()
    for _ in range(12):
        if listener.tick(d, pending, lambda s, t, w: wakes.append(w)) == "dispatched":
            break
    seen = ledger.observe(d, token, caller=accountant["agent_id"])
    asked = next(m for m in seen["messages"] if m["message_id"] == "u-peter-asks")
    ledger.record(d, token, plan["plan_id"], caller=accountant["agent_id"], wake_id=wakes[0],
                  message_through=seen["message_through"],
                  goal_event_through=seen["goal_event_through"], entries=[
        {"type": "discrepancy", "subject": "criterion:criterion-1",
         "text": "Criterion names v5 while the plan now publishes v6",
         "source_refs": ["goal_event:1", "goal_event:4"]},
        {"type": "observation", "subject": "step:v6", "text": "v6 replaces v5 publishing",
         "source_refs": ["goal_event:4"]},
        {"type": "claim", "subject": "criterion:criterion-2",
         "text": "Pip says Peter was told build 2741 is required",
         "source_refs": ["goal_event:2"]},
        {"type": "unknown", "subject": "subgoal:phone",
         "text": "Whether 2741 is installed on Peter's phone", "observed_at": START + 60_000},
        {"type": "subgoal_update", "subgoal_id": "phone", "expected_revision": 2,
         "fields": {"status": "unknown", "last_observed_at": START + 60_000,
                    "evidence": [{"text": "Last phone report was build 2738", "basis": "observed",
                                  "source_refs": [f"message:{asked['message_id']}@{asked['revision']}"]}]},
         "reason": "No install receipt seen"},
        {"type": "subgoal_propose", "subgoal_id": "report",
         "title": "Peter hears the corrected status", "intent": "Criterion 2", "owner": "pip"},
    ])
    say("Status?", "peter-status")   # not booked yet: lag and freshness show it
    listener.tick(d, listener.Pending(), lambda s, t, w: wakes.append(w))
    recovery._claim(plan["plan_id"], db.now_ms() + 20_000)   # the Host claims a wake
    plan = task_plans.get(plan["plan_id"])
    act("checkpoint", {"progress": "Asked Avana which build Peter runs",
                       "next_work": "Read her answer, then report",
                       "continuation": {"kind": "dependency", "key": "peer:r0123456789ab",
                                        "reason": "Waiting for Avana to answer",
                                        "due_at": START + 3_600_000}})
    return {
        "plan": task_plans.get(plan["plan_id"]),
        "ledger": ledger.events(plan["plan_id"]),
        "delegations": {"delegations": ledger.delegations_for(pip["agent_id"])},
        "_ids": {pip["agent_id"]: "agent-pip", accountant["agent_id"]: "agent-pip-accountant",
                 d: "dg0000000000000001"},
    }


def _normalize(data: dict) -> dict:
    ids = data.pop("_ids")
    text = json.dumps(data, sort_keys=True)
    plan_id = data["plan"]["plan_id"]
    text = text.replace(plan_id, "agent-pip:number-ninja:0000000a")
    for old, new in ids.items():
        text = text.replace(old, new)
    text = re.sub(r'"(bg1:\d+:)bookkeeping-[^"]+"', r'"\1bookkeeping-dg0000000000000001"', text)
    text = re.sub(r'task-goal-[0-9a-f]{32}', 'task-goal-00000000000000000000000000000000', text)
    text = re.sub(r'"\d{4}-\d\d-\d\dT[\d:.]+Z"', '"2026-10-05T00:00:00.000Z"', text)
    return json.loads(text)


def test_the_goal_ledger_fixture_is_what_the_host_emits(monkeypatch, tmp_path):
    produced = _normalize(_scenario(monkeypatch, tmp_path))
    schema = json.loads(SCHEMA.read_text())
    for key, definition in (("plan", "plan"), ("ledger", "ledger_page"), ("delegations", "delegations")):
        validate(produced[key], schema["$defs"][definition], schema, key)
    document = {"description": json.loads(FIXTURE.read_text())["description"]
                if FIXTURE.exists() else "", **produced}
    if os.environ.get("CLARP_UPDATE_FIXTURES"):
        FIXTURE.write_text(json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    assert json.loads(FIXTURE.read_text()) == document
