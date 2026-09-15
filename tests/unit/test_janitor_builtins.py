"""Built-in demand Janitors use ordinary identities and guarded run history."""
from __future__ import annotations

import importlib
import sqlite3

import pytest

from lib import agents, backends, db, janitors


def builtins():
    return importlib.import_module("lib.janitor_builtins")


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *args: [])


def enabled_role(role="message-delegator"):
    return builtins().ensure_builtins(cwd="/tmp", initial={role: {"enabled": True}})[role]


def test_catalog_exposes_distinct_effects_and_reusable_demand_triggers():
    catalog = {v["id"]: v for v in janitors.templates()}
    assert {"task-labels", "message-delegator", "tool-explainer"} <= catalog.keys()
    assert catalog["message-delegator"]["allowed_effects"] == ["message_route"]
    assert catalog["tool-explainer"]["allowed_effects"] == ["tool_explanation"]
    triggers = {v["trigger_id"]: v for v in janitors.trigger_definitions()}
    for tid in ("routing-requested", "tool-explanation-requested"):
        assert triggers[tid]["kind"] == "demand"
        assert triggers[tid]["version"] == 1


@pytest.mark.parametrize("role,patch", [("tool-explainer", {"detail_level": 4}), ("message-delegator", {"timeout_ms": 12000})])
def test_builtin_reset_restores_options_and_keeps_identity_scope(role, patch):
    original = enabled_role(role)
    changed = janitors.configure(original["session"], original["revision"], options=patch)
    reset = janitors.reset_defaults(changed["session"], changed["revision"])
    assert reset["agent_id"] == original["agent_id"]
    assert reset["scope"] == original["scope"]
    assert not reset["enabled"]
    expected = {item["key"]: item["default"] for item in janitors.template(role)["options"]}
    assert all(reset["options"][key] == value for key, value in expected.items())


def test_ensure_installs_real_stable_agents_once_and_preserves_existing_agents():
    sam = agents.create_agent(persona="Sam", voice_id="voice", cwd="/tmp", session="sam", model="chosen")
    before = agents.get_by_agent_id(sam)
    first = builtins().ensure_builtins(cwd="/tmp")
    assert first == builtins().ensure_builtins(cwd="/other")
    assert agents.get_by_agent_id(sam) == before
    assert len(agents.list_agents()) == 1 + len(builtins().ROLES)
    assert not first["message-delegator"]["enabled"]
    assert first["tool-explainer"]["enabled"]
    for role, config in first.items():
        identity = agents.get_by_agent_id(config["agent_id"])
        assert identity["is_janitor"] == 1
        assert identity["model"] == ("" if role == "audio-bookkeeper" else "gpt-5.3-codex-spark")
        assert not identity["heartbeat_enabled"] and not identity["dreaming_enabled"]
        assert config["builtin_role"] == role
        assert config["execution"] == ({"executor": "deterministic", "provider": "local"} if role == "audio-bookkeeper" else {"executor": "ephemeral", "provider": "codex"})
        assert len(config["supported_trigger_ids"]) == 1
        assert not config["capabilities"]["can_release"]
        assert not agents.interaction_capabilities(identity)["can_chat"]
    assert db.conn().execute("SELECT COUNT(*) FROM runtimes").fetchone()[0] == 0
    assert db.conn().execute("SELECT COUNT(*) FROM queued_turns").fetchone()[0] == 0


def test_seed_preserves_legacy_routing_and_never_overwrites_later_configuration():
    seed = {"message-delegator": {"enabled": False, "backend": "codex", "provider": "openai", "model": "gpt-5.4-mini", "effort": "low"}}
    config = builtins().ensure_builtins(initial=seed)["message-delegator"]
    assert config["model"] == "gpt-5.4-mini" and config["execution"]["provider"] == "openai"
    modified = janitors.configure(config["session"], config["revision"], model="user-choice", effort="medium")
    assert not modified["enabled"]
    assert builtins().ensure_builtins(initial={"message-delegator": {"enabled": True}})["message-delegator"] == modified


def test_openai_legacy_minimal_effort_is_preserved_and_dispatchable():
    config = builtins().ensure_builtins(initial={"message-delegator": {
        "enabled": True, "backend": "codex", "provider": "openai", "model": "gpt-5.4-mini", "effort": "minimal"}})["message-delegator"]
    assert builtins().resolve("message-delegator")["effort"] == "minimal"
    updated = janitors.configure(config["session"], config["revision"], model="new-model")
    assert updated["effort"] == "minimal" and updated["execution"]["provider"] == "openai"


def test_archived_or_deleted_builtin_is_not_resurrected_by_ensure():
    config = enabled_role()
    agents.set_archived(config["agent_id"], True)
    builtins().ensure_builtins()
    assert agents.get_by_agent_id(config["agent_id"])["archived_at"]
    assert builtins().resolve("message-delegator") is None
    db.conn().execute("UPDATE agents SET deleted_at=1 WHERE agent_id=?", (config["agent_id"],))
    builtins().ensure_builtins()
    assert db.conn().execute("SELECT deleted_at FROM agents WHERE agent_id=?", (config["agent_id"],)).fetchone()[0] == 1


def test_reserved_session_collision_does_not_convert_foreign_agent():
    identity = agents.create_agent(persona="Ordinary", voice_id="", cwd="/tmp", session="clarp-message-delegator")
    config = builtins().ensure_builtins()["message-delegator"]
    assert config["agent_id"] != identity and config["session"] != "clarp-message-delegator"
    assert not agents.get_by_agent_id(identity)["is_janitor"]


def test_jobs_can_overlap_but_two_label_janitors_still_cannot():
    enabled_role()
    for session in ("labels-one", "labels-two"):
        agents.create_agent(persona=session, voice_id="", cwd="/tmp", session=session)
        config = janitors.create(session)
        if session == "labels-one":
            janitors.set_enabled(session, config["revision"], True)
        else:
            with pytest.raises(janitors.JanitorError, match="scope"):
                janitors.set_enabled(session, config["revision"], True)
    assert all(v["enabled"] for v in janitors.list_janitors() if v["template_id"] in {"message-delegator", "tool-explainer"})
    assert all(not v["enabled"] for v in janitors.list_janitors() if v["template_id"] in {"heartbeat-decider", "quota-monitor"})


@pytest.mark.parametrize("template,trigger", [("task-labels", "routing-requested"), ("message-delegator", "agent-work-completed"), ("tool-explainer", "routing-requested")])
def test_configuration_rejects_triggers_for_another_job(template, trigger):
    with pytest.raises(janitors.JanitorError, match="trigger"):
        janitors.validate_configuration(template_id=template, attachments=[{"trigger_id": trigger}])


def test_demand_run_has_frozen_identity_config_idempotency_and_ordinary_history():
    config = enabled_role()
    context = {"input_hash": "abc123", "candidate_count": 2}
    run = builtins().begin_run("message-delegator", "request-1", context=context)
    assert run["agent_id"] == config["agent_id"] and run["session"] == config["session"]
    assert run["configuration"]["executor"] == "ephemeral"
    assert run["configuration"]["model"] == config["model"]
    assert run["configuration"]["effort"] == config["effort"]
    assert run["configuration"]["context"] == context
    assert run["candidates"] == [] and run["status"] == "running"
    assert builtins().begin_run("message-delegator", "request-1", context=context) == run
    with pytest.raises(janitors.JanitorError, match="different"):
        builtins().begin_run("message-delegator", "request-1", context={"input_hash": "different"})
    assert builtins().begin_run("message-delegator", "request-2") is None
    assert janitors.list_runs(config["session"])[0]["run_id"] == run["run_id"]
    assert builtins().complete_run(run["run_id"], result={"summary": "Selected one eligible recipient", "target_count": 1})
    finished = janitors.get_run(run["run_id"])
    assert finished["status"] == "completed" and finished["outcome"] == "completed"
    assert finished["demand_result"]["target_count"] == 1
    assert not db.conn().execute("SELECT 1 FROM janitor_effects").fetchone()
    assert janitors.get(config["session"])["last_run_at"]


@pytest.mark.parametrize("change", ["pause", "configure", "archive", "model", "attachment"])
def test_demand_completion_fails_closed_after_permission_or_identity_changes(change):
    config = enabled_role()
    run = builtins().begin_run("message-delegator", "stale-request")
    if change == "pause":
        janitors.set_enabled(config["session"], config["revision"], False)
    elif change == "configure":
        janitors.configure(config["session"], config["revision"], effort="medium")
    elif change == "archive":
        agents.set_archived(config["agent_id"], True)
    elif change == "model":
        agents.update_agent(config["agent_id"], model="changed-model")
    else:
        db.conn().execute("UPDATE janitor_attachments SET enabled=0 WHERE agent_id=?", (config["agent_id"],))
    assert not builtins().is_current(run["run_id"])
    assert not builtins().complete_run(run["run_id"], result={"summary": "Late output"})
    assert janitors.get_run(run["run_id"])["demand_result"] is None


def test_paused_missing_and_disabled_demand_never_admit():
    assert builtins().begin_run("message-delegator", "missing") is None
    config = builtins().ensure_builtins()["message-delegator"]
    assert builtins().begin_run("message-delegator", "paused") is None
    config = janitors.set_enabled(config["session"], config["revision"], True)
    janitors.configure(config["session"], config["revision"], attachments=[{**config["attachments"][0], "enabled": False}])
    assert builtins().begin_run("message-delegator", "disabled") is None
    assert not janitors.list_runs(config["session"])


def test_demand_job_cannot_enter_label_queue_or_write_labels():
    config = enabled_role()
    with pytest.raises(janitors.JanitorError, match="task.label"):
        janitors.create_run(config["attachments"][0]["attachment_id"], config["generation"], [{}])
    run = builtins().begin_run("message-delegator", "restricted")
    assert not janitors.validate_dispatch(run["session"], run["run_id"], run["trace_id"])
    agents.create_agent(persona="Task", voice_id="", cwd="/tmp", session="task")
    with pytest.raises(janitors.JanitorError, match="task.label"):
        janitors.review(run["run_id"], "task", 1, "changed", label="Stolen label")


def test_context_and_result_are_bounded_metadata_not_transcripts():
    enabled_role()
    with pytest.raises(janitors.JanitorError, match="metadata"):
        builtins().begin_run("message-delegator", "raw", context={"prompt": "User transcript"})
    run = builtins().begin_run("message-delegator", "bounded")
    with pytest.raises(janitors.JanitorError, match="metadata"):
        builtins().complete_run(run["run_id"], result={"summary": "x" * 501})


def test_v74_upgrade_is_additive_preserves_configuration_and_definitions():
    sam = agents.create_agent(persona="Sam", voice_id="", cwd="/tmp", session="sam")
    configured = janitors.create("sam")
    c = db.conn()
    c.execute("DROP TABLE janitor_demand_claims")
    c.execute("DROP TABLE janitor_demand_results")
    c.execute("DROP TABLE janitor_builtins")
    c.execute("DROP TRIGGER janitor_demand_trigger_no_update")
    c.execute("DROP TRIGGER janitor_demand_trigger_no_delete")
    c.execute("DELETE FROM janitor_trigger_definitions WHERE kind='demand'")
    c.execute("ALTER TABLE janitor_configs DROP COLUMN execution_json")
    c.execute("PRAGMA user_version=74")
    db._migrate(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] >= 75
    assert janitors.get("sam") == configured
    assert agents.get_by_agent_id(sam)["is_janitor"]
    assert len(agents.list_agents()) == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        c.execute("UPDATE janitor_trigger_definitions SET name='Changed' WHERE trigger_id='routing-requested'")
    assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_custom_janitor_can_own_reusable_trigger_when_builtin_is_paused():
    builtins().ensure_builtins()
    aid = agents.create_agent(persona="Router", voice_id="", cwd="/tmp", session="custom-router", backend="codex", model="custom-model")
    config = janitors.create("custom-router", template_id="message-delegator")
    config = janitors.set_enabled("custom-router", config["revision"], True)
    selected = builtins().resolve("message-delegator")
    assert selected["agent_id"] == aid
    run = builtins().begin_run("message-delegator", "custom-request")
    assert run["agent_id"] == aid and run["configuration"]["model"] == "custom-model"
    assert builtins().get_builtin("message-delegator")["agent_id"] != aid


def test_scoped_demand_requires_a_matching_target_and_is_never_global_by_accident():
    config = enabled_role()
    target = agents.create_agent(persona="Target", voice_id="", cwd="/tmp", session="target")
    config = janitors.configure(config["session"], config["revision"], scope={"agent_ids": [target]})
    janitors.set_enabled(config["session"], config["revision"], True)
    assert builtins().resolve("message-delegator") is None
    assert builtins().resolve("message-delegator", target_agent_id=target)["agent_id"] == config["agent_id"]


def test_builtin_cannot_change_job_remove_or_release():
    config = enabled_role()
    for action in (lambda: janitors.remove(config["session"], config["revision"]),
                   lambda: janitors.release(config["session"], config["revision"]),
                   lambda: janitors.configure(config["session"], config["revision"], template_id="task-labels")):
        with pytest.raises(janitors.JanitorError, match="built.in"):
            action()
    assert janitors.get(config["session"]) == config


def test_release_preserves_identity_history_and_blocks_old_maintenance_admission():
    aid = agents.create_agent(persona="Sam", voice_id="v", cwd="/tmp", session="sam", model="chosen")
    agents.record_state(aid, "done")
    config = janitors.create("sam")
    history = list(db.conn().execute("SELECT * FROM state_log"))
    released = janitors.release("sam", config["revision"])
    assert released["agent_id"] == aid and not released["is_janitor"]
    identity = agents.get_by_agent_id(aid)
    assert identity["persona"] == "Sam" and identity["model"] == "chosen"
    assert identity["archived_at"] is None and not identity["is_janitor"]
    assert list(db.conn().execute("SELECT * FROM state_log")) == history
    assert agents.interaction_capabilities(identity)["can_chat"]
    assert janitors.list_janitors() == []
    with pytest.raises(janitors.JanitorError, match="released"):
        janitors.set_enabled("sam", released["revision"], True)
    recreated = janitors.create("sam")
    assert recreated["agent_id"] == aid and recreated["is_janitor"] and not recreated["enabled"]


def test_release_requires_paused_idle_agent_and_checks_revision():
    aid = agents.create_agent(persona="Sam", voice_id="", cwd="/tmp", session="sam")
    config = janitors.create("sam")
    config = janitors.set_enabled("sam", config["revision"], True)
    with pytest.raises(janitors.JanitorError, match="Pause"):
        janitors.release("sam", config["revision"])
    config = janitors.set_enabled("sam", config["revision"], False)
    agents.record_state(aid, "thinking")
    with pytest.raises(janitors.JanitorError, match="finish"):
        janitors.release("sam", config["revision"])


def test_provider_and_backend_change_atomically_with_generation_fence():
    config = enabled_role()
    run = builtins().begin_run("message-delegator", "before-provider-change")
    new = janitors.configure(config["session"], config["revision"], backend="claude", model="sonnet",
                            effort="", execution={"executor": "ephemeral", "provider": "claude"})
    assert new["backend"] == "claude" and new["model"] == "sonnet"
    assert new["execution"]["provider"] == "claude" and not new["enabled"]
    assert not builtins().is_current(run["run_id"])


def test_claim_is_once_only_and_request_history_remains_readable_while_paused():
    config = enabled_role()
    assert builtins().get_request_run("message-delegator", "claim-once") is None
    run = builtins().begin_run("message-delegator", "claim-once")
    assert builtins().claim_run(run["run_id"])
    assert not builtins().claim_run(run["run_id"])
    builtins().complete_run(run["run_id"], result={"summary": "Handled"})
    janitors.set_enabled(config["session"], config["revision"], False)
    assert builtins().get_request_run("message-delegator", "claim-once")["status"] == "completed"
    assert not builtins().claim_run(run["run_id"])


def test_external_result_transaction_rolls_back_with_callers_effect():
    enabled_role()
    run = builtins().begin_run("message-delegator", "atomic-result")
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    assert builtins().complete_run(run["run_id"], result={"summary": "Handled"}, connection=c)
    c.execute("ROLLBACK")
    assert builtins().is_current(run["run_id"])
    assert janitors.get_run(run["run_id"])["demand_result"] is None


def test_label_finisher_cannot_bypass_demand_result_fence():
    enabled_role()
    run = builtins().begin_run("message-delegator", "no-label-finisher")
    with pytest.raises(janitors.JanitorError, match="Demand"):
        janitors.finish_run(run["run_id"])


def test_builtin_seed_is_atomic_if_any_identity_is_already_owned():
    import uuid
    aid = str(uuid.uuid5(uuid.NAMESPACE_URL, "clarp:janitor:builtin:tool-explainer"))
    c = db.conn()
    c.execute("INSERT INTO agents(agent_id,persona,voice_id,cwd,session,created_at) VALUES (?,'Foreign','','/tmp','foreign',1)", (aid,))
    with pytest.raises(janitors.JanitorError, match="owned"):
        builtins().ensure_builtins()
    assert len(agents.list_agents()) == 1
    assert not c.execute("SELECT 1 FROM janitor_builtins").fetchone()


def test_claim_expiry_cancels_late_output_and_admits_a_new_request(monkeypatch):
    enabled_role()
    clock = [1_000_000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    old = builtins().begin_run("message-delegator", "orphan")
    assert builtins().claim_run(old["run_id"])
    clock[0] += builtins().DEMAND_RUN_TTL_MS - 1
    assert builtins().begin_run("message-delegator", "fresh") is None
    clock[0] += 1
    assert not builtins().is_current(old["run_id"])
    fresh = builtins().begin_run("message-delegator", "fresh")
    assert fresh and builtins().claim_run(fresh["run_id"])
    assert janitors.get_run(old["run_id"])["status"] == "cancelled"
    assert not builtins().complete_run(old["run_id"], result={"summary": "Late output"})


def test_recovery_cancels_only_expired_ephemeral_and_never_replays_old_claim(monkeypatch):
    enabled_role()
    clock = [1_000_000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    old = builtins().begin_run("message-delegator", "interrupted")
    assert builtins().claim_run(old["run_id"])
    label_agent = agents.create_agent(persona="Labels", voice_id="", cwd="/tmp", session="labels")
    label_config = janitors.create("labels")
    c = db.conn()
    c.execute("""INSERT INTO janitor_runs(run_id,agent_id,session,attachment_id,generation,trace_id,
        candidates_json,configuration_json,created_at) VALUES ('label-run',?,'labels',?,1,'label-trace','[]','{"template_id":"task-labels"}',1)""",
        (label_agent, label_config["attachments"][0]["attachment_id"]))
    assert builtins().recover_expired_runs() == 0
    clock[0] += builtins().DEMAND_RUN_TTL_MS
    assert builtins().recover_expired_runs() == 1
    assert builtins().recover_expired_runs() == 0
    assert janitors.get_run("label-run")["status"] == "queued"
    assert janitors.get_run(old["run_id"])["status"] == "cancelled"
    assert builtins().begin_run("message-delegator", "interrupted")["status"] == "cancelled"
    assert not builtins().claim_run(old["run_id"])
    assert builtins().begin_run("message-delegator", "after-restart")


def test_provider_must_match_identity_backend_and_invalid_edit_is_atomic():
    config = enabled_role()
    with pytest.raises(janitors.JanitorError, match="backend"):
        janitors.configure(config["session"], config["revision"], execution={"executor": "ephemeral", "provider": "claude"})
    assert janitors.get(config["session"]) == config


def test_release_waits_for_paused_ephemeral_process_to_finish():
    builtins().ensure_builtins()
    agents.create_agent(persona="Custom", voice_id="", cwd="/tmp", session="custom", backend="codex", model="chosen")
    config = janitors.create("custom", template_id="message-delegator")
    config = janitors.set_enabled("custom", config["revision"], True)
    run = builtins().begin_run("message-delegator", "running-process")
    assert builtins().claim_run(run["run_id"])
    paused = janitors.set_enabled("custom", config["revision"], False)
    assert paused["cancellation_pending"]
    with pytest.raises(janitors.JanitorError, match="finish"):
        janitors.release("custom", paused["revision"])
    assert not builtins().complete_run(run["run_id"], outcome="cancelled")
    assert not janitors.get("custom")["cancellation_pending"]
    assert not janitors.release("custom", paused["revision"])["is_janitor"]


def test_demand_lookup_and_admission_never_query_runtime_processes(monkeypatch):
    builtins().ensure_builtins()
    def forbidden(*args):
        raise AssertionError("Demand metadata must not call the runtime service")
    monkeypatch.setattr(backends, "active_handles", forbidden)
    assert builtins().get_builtin("message-delegator")
    assert builtins().resolve("tool-explainer")
    assert builtins().begin_run("tool-explainer", "no-runtime-rpc")


def test_reenable_waits_for_cancelled_provider_invocation_before_new_claim():
    config = enabled_role()
    old = builtins().begin_run("message-delegator", "old-invocation")
    assert builtins().claim_run(old["run_id"])
    paused = janitors.set_enabled(config["session"], config["revision"], False)
    janitors.set_enabled(config["session"], paused["revision"], True)
    assert builtins().begin_run("message-delegator", "new-invocation") is None
    assert not builtins().complete_run(old["run_id"], outcome="cancelled")
    assert builtins().begin_run("message-delegator", "new-invocation")
