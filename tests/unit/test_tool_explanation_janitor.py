"""Tool narration is demand work owned by a configured Janitor identity."""
from __future__ import annotations

import threading
import time
import json
from pathlib import Path

import pytest

from lib import agents, backends, db, janitors, tool_explanations


ITEM = {"id": "row", "demand_id": "view", "activity": {"command": "ls"}}


@pytest.fixture(autouse=True)
def no_agent_processes(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *args: [])


def seed(**initial):
    return tool_explanations.janitor_builtins.ensure_builtins(
        cwd="/tmp", initial={"tool-explainer": initial})["tool-explainer"]


def pause(config):
    current = janitors.get(config["session"])
    return janitors.set_enabled(current["session"], current["revision"], False)


def wait_for(check):
    for _ in range(200):
        result = check()
        if result:
            return result
        time.sleep(.01)
    pytest.fail("explanation worker did not finish")


def ready(service, item=ITEM, *, target_agent_id=None):
    def check():
        result = service.request(3, [item], target_agent_id=target_agent_id)["items"][0]
        return result if result["status"] == "ready" else None
    return wait_for(check)


def translate(level, items):
    return {item["id"]: "List the files." for item in items}


def test_missing_janitor_disables_admission_without_creating_an_identity():
    calls = []
    with tool_explanations.ToolExplanations(translate=lambda *_: calls.append(1), debounce=5) as service:
        assert service.request(3, [ITEM])["items"][0]["status"] == "disabled"
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_jobs").fetchone()[0] == 0
        assert db.conn().execute("SELECT count(*) FROM agents").fetchone()[0] == 0
    assert calls == []


def test_missing_janitor_still_records_release_fences():
    with tool_explanations.ToolExplanations(translate=lambda *_: pytest.fail("model must not run"), debounce=5) as service:
        assert service.request(3, [ITEM])["items"][0]["status"] == "disabled"
        assert service.request(3, [], release=["view"])["items"] == []
        assert service.request(3, [ITEM])["items"][0]["status"] == "cancelled"


def test_success_records_bounded_history_without_raw_activity_or_a_chat():
    config = seed(model="configured-model", effort="medium")
    item = {**ITEM, "activity": {"command": "private-script --token=do-not-persist", "summary": "private activity marker"}}
    with tool_explanations.ToolExplanations(translate=translate, debounce=0) as service:
        assert ready(service, item)["text"] == "List the files."
        assert service.request(3, [item])["model"] == "configured-model"
    runs = janitors.list_runs(config["session"])
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "completed"
    assert run["configuration"]["model"] == "configured-model"
    assert run["configuration"]["effort"] == "medium"
    assert run["configuration"]["executor"] == "ephemeral"
    assert run["configuration"]["context"]["item_count"] == 1
    assert run["configuration"]["context"]["detail_level"] == 3
    assert len(run["configuration"]["context"]["request_hash"]) == 64
    assert run["demand_result"]["item_count"] == 1
    history = json.dumps(run)
    assert all(value not in history for value in ["private-script", "private activity marker", "do-not-persist"])
    assert db.conn().execute("SELECT count(*) FROM runtimes").fetchone()[0] == 0
    assert db.conn().execute("SELECT count(*) FROM queued_turns").fetchone()[0] == 0


def test_paused_janitor_hides_ready_cache_without_model_work():
    config = seed()
    calls = []
    def capture(level, items):
        calls.append(1)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=capture, debounce=0) as service:
        ready(service)
        pause(config)
        assert service.request(3, [ITEM])["items"][0]["status"] == "disabled"
        assert len(calls) == 1
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_cache").fetchone()[0] == 1


def test_pause_preserves_release_and_late_request_fences():
    config = seed()
    with tool_explanations.ToolExplanations(translate=lambda *_: pytest.fail("paused work must not run"), debounce=5) as service:
        assert service.request(3, [ITEM])["items"][0]["status"] == "pending"
        pause(config)
        assert service.request(3, [], release=["view"])["items"] == []
        assert service.request(3, [ITEM])["items"][0]["status"] == "cancelled"
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_jobs").fetchone()[0] == 0


def test_paused_worker_still_prunes_expired_private_payloads_without_requests(monkeypatch):
    config = seed()
    clock = [db.now_ms()]
    monkeypatch.setattr(tool_explanations.durable_queue.cache, "now_ms", lambda: clock[0])
    with tool_explanations.ToolExplanations(translate=lambda *_: pytest.fail("paused work must not run"), debounce=5) as service:
        service.request(3, [ITEM])
        service.request(3, [], release=["other-view"])
        tool_explanations.durable_queue.cache.put("old-cache", "Old explanation.")
        pause(config)
        clock[0] += tool_explanations.durable_queue.cache.TTL_MS + 1
        wait_for(lambda: db.conn().execute("SELECT count(*) FROM tool_explanation_jobs").fetchone()[0] == 0)
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_cache").fetchone()[0] == 0
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_demands").fetchone()[0] == 0
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_releases").fetchone()[0] == 0


def test_pause_between_admission_and_model_start_prevents_inference(monkeypatch):
    config = seed()
    original = tool_explanations.janitor_builtins.claim_run
    claimed = threading.Event()
    def pause_after_claim(run_id):
        value = original(run_id)
        pause(config)
        claimed.set()
        return value
    monkeypatch.setattr(tool_explanations.janitor_builtins, "claim_run", pause_after_claim)
    calls = []
    with tool_explanations.ToolExplanations(translate=lambda *_: calls.append(1), debounce=0) as service:
        service.request(3, [ITEM])
        assert claimed.wait(2)
        wait_for(lambda: db.conn().execute("SELECT count(*) FROM tool_explanation_jobs").fetchone()[0] == 0)
    assert calls == []
    assert db.conn().execute("SELECT count(*) FROM tool_explanation_cache").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["pause", "configure", "archive", "model", "attachment"])
def test_changed_configuration_rejects_inflight_output(change):
    config = seed()
    entered, finish = threading.Event(), threading.Event()
    def delayed(level, items):
        entered.set()
        assert finish.wait(3)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=delayed, debounce=0) as service:
        service.request(3, [ITEM])
        assert entered.wait(2)
        if change == "pause":
            pause(config)
        elif change == "configure":
            janitors.configure(config["session"], config["revision"], effort="medium")
        elif change == "archive":
            agents.set_archived(config["agent_id"], True)
        elif change == "model":
            agents.update_agent(config["agent_id"], model="new-model")
        else:
            db.conn().execute("UPDATE janitor_attachments SET enabled=0 WHERE agent_id=?", (config["agent_id"],))
        finish.set()
        wait_for(lambda: db.conn().execute("SELECT count(*) FROM tool_explanation_jobs").fetchone()[0] == 0)
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_cache").fetchone()[0] == 0
    assert janitors.list_runs(config["session"])[0]["demand_result"] is None


@pytest.mark.parametrize("change", ["configure", "model", "replacement"])
def test_reconfiguration_and_replacement_get_fresh_cache_and_frozen_model(change):
    config = seed()
    calls = []
    def capture(level, items):
        calls.append(1)
        return {item["id"]: f"Explanation {len(calls)}." for item in items}
    with tool_explanations.ToolExplanations(translate=capture, debounce=0) as service:
        assert ready(service)["text"] == "Explanation 1."
        if change == "configure":
            config = janitors.configure(config["session"], config["revision"], model="new-model", effort="medium")
            if not config["enabled"]:
                config = janitors.set_enabled(config["session"], config["revision"], True)
        elif change == "model":
            agents.update_agent(config["agent_id"], model="new-model", effort="medium")
        else:
            pause(config)
            agents.create_agent(persona="Alternative", voice_id="", cwd="/tmp", session="custom-explainer", backend="codex", model="new-model", effort="medium")
            config = janitors.create("custom-explainer", template_id="tool-explainer")
            config = janitors.configure(config["session"], config["revision"], options={"detail_level": 3})
            config = janitors.set_enabled(config["session"], config["revision"], True)
        assert ready(service)["text"] == "Explanation 2."
        assert service.request(3, [ITEM])["model"] == "new-model"
    assert len(calls) == 2
    assert db.conn().execute("SELECT count(*) FROM tool_explanation_cache").fetchone()[0] == 2
    assert any(run["configuration"]["model"] == "new-model" and run["configuration"]["effort"] == "medium"
               for run in janitors.list_runs(config["session"]))


def test_busy_janitor_keeps_durable_demand_for_retry():
    config = seed()
    builtin = tool_explanations.janitor_builtins
    other = builtin.begin_run("tool-explainer", "other-demand")
    calls = []
    def capture(level, items):
        calls.append(1)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=capture, debounce=0) as service:
        service.request(3, [ITEM])
        time.sleep(.3)
        assert calls == []
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_jobs").fetchone()[0] == 1
        assert builtin.complete_run(other["run_id"], result={"summary": "Other invocation finished"})
        assert ready(service)["text"] == "List the files."
    assert len(janitors.list_runs(config["session"])) == 2


def test_crashed_worker_recovers_after_both_queue_and_janitor_leases(monkeypatch):
    config = seed()
    builtin = tool_explanations.janitor_builtins
    queue = tool_explanations.durable_queue
    clock = [db.now_ms()]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    monkeypatch.setattr(queue.cache, "now_ms", lambda: clock[0])
    with tool_explanations.ToolExplanations(translate=translate, debounce=5) as stopped:
        stopped.request(3, [{"id": "row", "activity": ITEM["activity"]}])
    clock[0] += 5001
    assert queue.claim("dead-worker")
    orphan = builtin.begin_run("tool-explainer", "dead-invocation")
    assert builtin.claim_run(orphan["run_id"])
    calls = []
    def capture(level, items):
        calls.append(1)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=capture, debounce=0) as replacement:
        clock[0] += 60001
        # The queue lease has expired but the existing invocation still owns
        # admission; it must not be cancelled merely because a worker restarts.
        replacement.request(3, [ITEM])
        time.sleep(.1)
        assert calls == []
        assert janitors.get_run(orphan["run_id"])["status"] == "running"
        clock[0] += builtin.DEMAND_RUN_TTL_MS
        assert ready(replacement)["text"] == "List the files."
    assert calls == [1]
    assert janitors.get_run(orphan["run_id"])["status"] == "cancelled"
    assert len(janitors.list_runs(config["session"])) == 2


def test_two_workers_share_one_admitted_run_and_answer():
    config = seed()
    calls = []
    def capture(level, items):
        calls.append(1)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=capture, debounce=.1) as first, tool_explanations.ToolExplanations(translate=capture, debounce=.1) as second:
        first.request(3, [ITEM])
        second.request(3, [{**ITEM, "demand_id": "other-view"}])
        assert ready(first)["text"] == ready(second)["text"]
    assert calls == [1]
    assert len(janitors.list_runs(config["session"])) == 1


def test_queued_previous_generation_is_discarded_before_model():
    config = seed()
    with tool_explanations.ToolExplanations(translate=translate, debounce=.3) as service:
        service.request(3, [ITEM])
        old_key = db.conn().execute("SELECT cache_key FROM tool_explanation_jobs").fetchone()[0]
        agents.update_agent(config["agent_id"], model="new-model")
        assert ready(service)["text"] == "List the files."
    assert not db.conn().execute("SELECT 1 FROM tool_explanation_cache WHERE cache_key=?", (old_key,)).fetchone()
    assert len(janitors.list_runs(config["session"])) == 1


def test_configured_model_and_effort_keep_the_ephemeral_tool_free_boundary(monkeypatch):
    seed(model="chosen-model", effort="high")
    run = tool_explanations.janitor_builtins.begin_run("tool-explainer", "inspect-args")
    assert tool_explanations.janitor_builtins.claim_run(run["run_id"])
    monkeypatch.setenv("HOST_PRIVATE_TOKEN", "must-not-reach-provider-process")
    captured = []
    class Captured(Exception):
        pass
    def spawn(args, **kwargs):
        captured.append(args)
        assert args[args.index("--model") + 1] == "chosen-model"
        assert 'model_reasoning_effort="high"' in args
        assert all(flag in args for flag in ["--ephemeral", "--ignore-user-config", "--ignore-rules"])
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert all(setting in args for setting in ['mcp_servers={}', 'web_search="disabled"', 'project_doc_max_bytes=0', 'approval_policy="never"'])
        disabled = {args[i + 1] for i, value in enumerate(args[:-1]) if value == "--disable"}
        assert {"shell_tool", "unified_exec", "apps", "plugins", "hooks", "memories", "multi_agent", "browser_use", "computer_use", "skill_search"} <= disabled
        assert "HOST_PRIVATE_TOKEN" not in kwargs["env"]
        assert kwargs["start_new_session"]
        assert Path(kwargs["cwd"]).name.startswith("clarp-explanations-")
        raise Captured()
    monkeypatch.setattr(tool_explanations.subprocess, "Popen", spawn)
    with tool_explanations.ToolExplanations() as service:
        with pytest.raises(Captured):
            service._run_codex(3, [{"id": "1", "activity": {"command": "ls"}}], run=run)
    assert len(captured) == 1


def test_unsupported_executor_is_refused_before_process_start(monkeypatch):
    seed()
    run = tool_explanations.janitor_builtins.begin_run("tool-explainer", "wrong-provider")
    run["configuration"]["provider"] = "openai"
    monkeypatch.setattr(tool_explanations.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("must not start"))
    with tool_explanations.ToolExplanations() as service:
        with pytest.raises(ValueError, match="unsupported"):
            service._run_codex(3, [{"id": "1", "activity": {}}], run=run)


def test_failed_model_records_safe_health_and_keeps_failure_cooldown():
    config = seed()
    def broken(*_):
        raise RuntimeError("private raw command and secret")
    with tool_explanations.ToolExplanations(translate=broken, debounce=0) as service:
        service.request(3, [ITEM])
        wait_for(lambda: janitors.list_runs(config["session"]) and janitors.list_runs(config["session"])[0]["status"] == "failed")
        assert service.request(3, [ITEM])["items"][0]["reason"] == "translator_failed"
    current = janitors.get(config["session"])
    assert current["last_error"] == "translator_failed"
    assert current["health"] == "needs_attention"
    assert "private raw command" not in json.dumps(janitors.list_runs(config["session"]))


def test_eight_distinct_activities_share_one_audience_batch():
    config = seed()
    items = [{"id": str(i), "activity": {"command": f"ls directory-{i}"}} for i in range(8)]
    calls = []
    def capture(level, batch):
        calls.append(batch)
        return translate(level, batch)
    with tool_explanations.ToolExplanations(translate=capture, debounce=.1) as service:
        service.request(3, items)
        wait_for(lambda: all(row["status"] == "ready" for row in service.request(3, items)["items"]))
    assert len(calls) == 1 and len(calls[0]) == 8
    runs = janitors.list_runs(config["session"])
    assert len(runs) == 1 and runs[0]["configuration"]["context"]["item_count"] == 8


def scoped_owner(session, target_ids):
    agents.create_agent(persona=session, voice_id="", cwd="/tmp", session=session,
                        backend="codex", model=f"{session}-model", effort="medium")
    config = janitors.create(session, template_id="tool-explainer", scope={"agent_ids": target_ids})
    config = janitors.configure(session, config["revision"], options={"detail_level": 3})
    return janitors.set_enabled(session, config["revision"], True)


def test_scoped_owners_admit_correct_targets_and_freeze_separate_runs():
    pause(seed())
    targets = [agents.create_agent(persona=name, voice_id="", cwd="/tmp", session=name)
               for name in ["target-a", "target-b", "outside"]]
    configs = [scoped_owner(f"scoped-{index}", [target]) for index, target in enumerate(targets[:2])]
    calls = []
    def capture(level, items):
        calls.append(items)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=capture, debounce=.1) as service:
        for target, config in zip(targets, configs):
            response = service.request(3, [ITEM], target_agent_id=target)
            assert response["items"][0]["status"] == "pending"
            assert response["model"] == config["model"]
        assert service.request(3, [ITEM], target_agent_id=targets[2])["items"][0]["status"] == "disabled"
        assert service.request(3, [ITEM])["items"][0]["status"] == "disabled"
        for target in targets[:2]:
            assert ready(service, target_agent_id=target)["text"] == "List the files."
    assert len(calls) == 2 and all(len(batch) == 1 for batch in calls)
    assert db.conn().execute("SELECT count(*) FROM tool_explanation_cache").fetchone()[0] == 2
    for target, config in zip(targets, configs):
        runs = janitors.list_runs(config["session"])
        assert len(runs) == 1 and runs[0]["status"] == "completed"
        assert runs[0]["configuration"]["model"] == config["model"]
        assert runs[0]["configuration"]["target_agent_id"] == target


def test_same_scoped_owner_keeps_distinct_targets_out_of_shared_batches_and_cache():
    pause(seed())
    targets = [agents.create_agent(persona=name, voice_id="", cwd="/tmp", session=name)
               for name in ["target-a", "target-b"]]
    config = scoped_owner("scoped", targets)
    calls = []
    def capture(level, items):
        running = next(run for run in janitors.list_runs(config["session"]) if run["status"] == "running")
        target = running["configuration"]["target_agent_id"]
        calls.append((target, len(items)))
        return {item["id"]: f"Explanation for {target}." for item in items}
    with tool_explanations.ToolExplanations(translate=capture, debounce=.1) as first, tool_explanations.ToolExplanations(translate=capture, debounce=.1) as second:
        first.request(3, [ITEM], target_agent_id=targets[0])
        second.request(3, [ITEM], target_agent_id=targets[1])
        for service, target in zip([first, second], targets):
            assert ready(service, target_agent_id=target)["text"] == f"Explanation for {target}."
    assert sorted(calls) == sorted((target, 1) for target in targets)
    assert db.conn().execute("SELECT count(*) FROM tool_explanation_cache").fetchone()[0] == 2
    assert len(janitors.list_runs(config["session"])) == 2


def invalidate_target(target, change):
    if change == "archive":
        agents.set_archived(target, True)
    elif change == "delete":
        agents.soft_delete(target)
    else:
        janitors.create(agents.get_by_agent_id(target)["session"])


@pytest.mark.parametrize("change", ["archive", "delete", "convert"])
@pytest.mark.parametrize("phase", ["queued", "running", "cached"])
def test_scoped_target_lifecycle_fences_inference_and_cache(change, phase):
    pause(seed())
    target = agents.create_agent(persona="Target", voice_id="", cwd="/tmp", session="target")
    config = scoped_owner("scoped", [target])
    entered, finish = threading.Event(), threading.Event()
    calls = []
    def capture(level, items):
        calls.append(1)
        entered.set()
        if phase == "running":
            assert finish.wait(3)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=capture, debounce=.15 if phase == "queued" else 0) as service:
        assert service.request(3, [ITEM], target_agent_id=target)["items"][0]["status"] == "pending"
        if phase == "running":
            assert entered.wait(2)
        elif phase == "cached":
            assert ready(service, target_agent_id=target)["text"] == "List the files."
        invalidate_target(target, change)
        finish.set()
        wait_for(lambda: db.conn().execute("SELECT count(*) FROM tool_explanation_jobs").fetchone()[0] == 0)
        response = service.request(3, [ITEM], target_agent_id=target)["items"][0]
        assert response["status"] == "disabled" and "text" not in response
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_cache").fetchone()[0] == int(phase == "cached")
    runs = janitors.list_runs(config["session"])
    if phase == "queued":
        assert calls == [] and runs == []
    elif phase == "running":
        assert calls == [1] and runs[0]["status"] == "cancelled"
        assert runs[0]["demand_result"] is None
    else:
        assert calls == [1] and runs[0]["status"] == "completed"


def test_paused_scoped_owner_does_not_block_other_targets_or_release_fences():
    pause(seed())
    targets = [agents.create_agent(persona=name, voice_id="", cwd="/tmp", session=name)
               for name in ["target-a", "target-b"]]
    configs = [scoped_owner(f"scoped-{index}", [target]) for index, target in enumerate(targets)]
    calls = []
    def capture(level, items):
        calls.append(items)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=capture, debounce=.15) as service:
        service.request(3, [ITEM], target_agent_id=targets[0])
        service.request(3, [{**ITEM, "demand_id": "other-view"}], target_agent_id=targets[1])
        pause(configs[0])
        assert ready(service, {**ITEM, "demand_id": "other-view"}, target_agent_id=targets[1])["text"] == "List the files."
        assert service.request(3, [ITEM], target_agent_id=targets[0])["items"][0]["status"] == "disabled"
        service.request(3, [], release=["view"], target_agent_id=targets[0])
        assert service.request(3, [ITEM], target_agent_id=targets[0])["items"][0]["status"] == "cancelled"
    assert len(calls) == 1
    assert janitors.list_runs(configs[0]["session"]) == []
    assert len(janitors.list_runs(configs[1]["session"])) == 1


def test_custom_janitor_default_detail_disables_legacy_client_requests():
    pause(seed())
    agents.create_agent(persona="Custom", voice_id="", cwd="/tmp", session="custom", backend="codex")
    config = janitors.create("custom", template_id="tool-explainer")
    janitors.set_enabled("custom", config["revision"], True)
    with tool_explanations.ToolExplanations(translate=lambda *_: pytest.fail("default Developer must not run"), debounce=5) as service:
        response = service.request(3, [ITEM])
        assert response["detail_level"] == 0
        assert response["items"][0]["status"] == "disabled"
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_jobs").fetchone()[0] == 0
    assert janitors.list_runs("custom") == []


def configure_detail(config, level):
    current = janitors.get(config["session"])
    current = janitors.configure(current["session"], current["revision"], options={"detail_level": level})
    return janitors.set_enabled(current["session"], current["revision"], True)


def test_explicit_owner_detail_overrides_every_legacy_client_and_shares_audience_cache():
    config = configure_detail(seed(), 4)
    calls = []
    def capture(level, items):
        calls.append(level)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=capture, debounce=.1) as service:
        for requested in range(5):
            result = service.request(requested, [ITEM])
            assert result["detail_level"] == 4
            assert result["items"][0]["status"] == "pending"
        assert ready(service)["text"] == "List the files."
        for requested in range(5):
            assert service.request(requested, [ITEM])["items"][0]["status"] == "ready"
    assert calls == [4]
    run = janitors.list_runs(config["session"])[0]
    assert run["configuration"]["options"] == {"detail_level": 4}
    assert run["configuration"]["context"]["detail_level"] == 4
    assert db.conn().execute("SELECT count(*) FROM tool_explanation_cache").fetchone()[0] == 1


def test_explicit_developer_detail_cannot_be_overridden_by_legacy_clients(monkeypatch):
    config = configure_detail(seed(), 0)
    monkeypatch.setattr(tool_explanations, "script_evidence", lambda *_: pytest.fail("disabled detail must not read scripts"))
    with tool_explanations.ToolExplanations(translate=lambda *_: pytest.fail("Developer detail must not run"), debounce=0) as service:
        for requested in range(5):
            response = service.request(requested, [ITEM], cwd="/tmp")
            assert response["detail_level"] == 0
            assert response["items"][0]["status"] == "disabled"
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_jobs").fetchone()[0] == 0
    assert janitors.list_runs(config["session"]) == []


def test_unconfigured_builtin_alone_preserves_legacy_detail_until_adoption():
    config = seed()
    assert config["options"] == {"detail_level": 0}
    assert "detail_level" not in config["configured_option_keys"]
    calls = []
    def capture(level, items):
        calls.append(level)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=capture, debounce=0) as service:
        assert service.request(0, [ITEM])["items"][0]["status"] == "disabled"
        assert ready(service)["text"] == "List the files."
        assert calls == [3]
        config = configure_detail(config, 0)
        assert "detail_level" in config["configured_option_keys"]
        assert service.request(3, [ITEM])["items"][0]["status"] == "disabled"
        assert calls == [3]


def test_legacy_request_schema_remains_validated_after_owner_detail_is_configured():
    configure_detail(seed(), 3)
    with tool_explanations.ToolExplanations(translate=translate) as service:
        for invalid in [None, True, False, -1, 5, "3", 3.0]:
            with pytest.raises(ValueError, match="detail_level"):
                service.request(invalid, [ITEM])


def test_owner_detail_change_invalidates_cache_and_changes_prompt_audience():
    config = configure_detail(seed(), 3)
    calls = []
    def capture(level, items):
        calls.append(level)
        return {item["id"]: f"Audience {level}." for item in items}
    with tool_explanations.ToolExplanations(translate=capture, debounce=0) as service:
        assert ready(service)["text"] == "Audience 3."
        old_keys = {row[0] for row in db.conn().execute("SELECT cache_key FROM tool_explanation_cache")}
        config = configure_detail(config, 4)
        assert ready(service)["text"] == "Audience 4."
        keys = {row[0] for row in db.conn().execute("SELECT cache_key FROM tool_explanation_cache")}
        assert len(keys - old_keys) == 1
    assert calls == [3, 4]


def test_owner_detail_change_rejects_inflight_output():
    config = configure_detail(seed(), 3)
    entered, finish = threading.Event(), threading.Event()
    def capture(level, items):
        entered.set()
        assert finish.wait(3)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=capture, debounce=0) as service:
        service.request(3, [ITEM])
        assert entered.wait(2)
        configure_detail(config, 0)
        finish.set()
        wait_for(lambda: db.conn().execute("SELECT count(*) FROM tool_explanation_jobs").fetchone()[0] == 0)
        assert service.request(3, [ITEM])["items"][0]["status"] == "disabled"
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_cache").fetchone()[0] == 0
    assert janitors.list_runs(config["session"])[0]["demand_result"] is None


def test_configured_detail_remains_disabled_while_paused_and_releases_demand():
    config = configure_detail(seed(), 4)
    with tool_explanations.ToolExplanations(translate=lambda *_: pytest.fail("paused must not run"), debounce=5) as service:
        assert service.request(0, [ITEM])["items"][0]["status"] == "pending"
        pause(config)
        result = service.request(3, [ITEM])
        assert result["detail_level"] == 4 and result["items"][0]["status"] == "disabled"
        service.request(3, [], release=["view"])
        assert service.request(3, [ITEM])["items"][0]["status"] == "cancelled"


@pytest.mark.parametrize("phase", ["queued", "running", "cached"])
def test_adopting_explicit_default_revokes_legacy_detail_without_pausing(phase):
    config = seed()
    assert config["options"] == {"detail_level": 0}
    assert config["configured_option_keys"] == []
    entered, finish = threading.Event(), threading.Event()
    calls = []
    def capture(level, items):
        calls.append(level)
        entered.set()
        if phase == "running":
            assert finish.wait(3)
        return translate(level, items)
    with tool_explanations.ToolExplanations(translate=capture, debounce=.15 if phase == "queued" else 0) as service:
        assert service.request(3, [ITEM])["items"][0]["status"] == "pending"
        if phase == "running":
            assert entered.wait(2)
        elif phase == "cached":
            assert ready(service)["text"] == "List the files."
        adopted = janitors.adopt_options(config["session"], config["revision"], {"detail_level": 0})
        assert adopted["enabled"] and adopted["options"] == config["options"]
        assert adopted["generation"] == config["generation"] + 1
        assert adopted["configured_option_keys"] == ["detail_level"]
        finish.set()
        wait_for(lambda: db.conn().execute("SELECT count(*) FROM tool_explanation_jobs").fetchone()[0] == 0)
        result = service.request(3, [ITEM])
        assert result["detail_level"] == 0 and result["items"][0]["status"] == "disabled"
        assert db.conn().execute("SELECT count(*) FROM tool_explanation_cache").fetchone()[0] == int(phase == "cached")
    assert calls == ([] if phase == "queued" else [3])
    runs = janitors.list_runs(config["session"])
    if phase == "queued":
        assert runs == []
    elif phase == "running":
        assert runs[0]["status"] == "cancelled" and runs[0]["demand_result"] is None
