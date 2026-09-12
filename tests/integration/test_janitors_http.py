"""Real authenticated HTTP routes with isolated state and no model/service work."""
import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from lib import agents, db, janitors, task_plans
from lib.janitor_runner import SQLiteSource


_spec = importlib.util.spec_from_file_location(
    "janitor_http_server", Path(__file__).resolve().parents[2] / "server/server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)
TOKEN = "isolated-janitor-http-test"


def test_builtin_scope_is_complete_on_wire_without_changing_stored_scope(host):
    from lib import janitor_builtins
    row = janitor_builtins.ensure_builtins(cwd=str(host.path))["tool-explainer"]
    before = db.conn().execute("SELECT scope_json,revision,generation FROM janitor_configs WHERE agent_id=?", (row["agent_id"],)).fetchone()
    assert before["scope_json"] == "{}"
    _, catalog = request(host, "/janitors")
    expected = {"agent_ids": [], "exclude_agent_ids": []}
    assert next(j for j in catalog["janitors"] if j["agent_id"] == row["agent_id"])["scope"] == expected
    _, response = request(host, f'/janitors/{row["session"]}')
    assert response["janitor"]["scope"] == expected
    after = db.conn().execute("SELECT scope_json,revision,generation FROM janitor_configs WHERE agent_id=?", (row["agent_id"],)).fetchone()
    assert tuple(after) == tuple(before)
    _, response = request(host, f'/janitors/{row["session"]}/reset-defaults', {"expected_revision": row["revision"]})
    assert response["janitor"]["scope"] == expected


def test_scope_wire_defaults_preserve_existing_exclusions(host):
    from lib import janitor_builtins
    row = janitor_builtins.ensure_builtins(cwd=str(host.path))["tool-explainer"]
    db.conn().execute("UPDATE janitor_configs SET scope_json=? WHERE agent_id=?", (json.dumps({"exclude_agent_ids": [host.theo]}), row["agent_id"]))
    _, response = request(host, f'/janitors/{row["session"]}')
    assert response["janitor"]["scope"] == {"agent_ids": [], "exclude_agent_ids": [host.theo]}


def test_release_returns_sam_to_chat_without_archiving_or_losing_identity(host):
    before = agents.get_by_agent_id(host.sam)
    row = create(host)
    status, result = request(host, "/janitors/sam/release", {
        "expected_revision": row["revision"]})
    assert status == 200, result
    after = agents.get_by_agent_id(host.sam)
    assert after["is_janitor"] == 0 and after["archived_at"] is None
    for field in ("agent_id", "session", "persona", "voice_id", "model", "avatar_path"):
        assert after[field] == before[field]
    assert agents.interaction_capabilities(after)["can_chat"] is True
    assert agents.get_focus() == host.theo
    assert request(host, "/janitors")[1]["janitors"] == []


def test_release_requires_auth_and_current_revision(host):
    row = create(host)
    body = {"expected_revision": row["revision"]}
    assert request(host, "/janitors/sam/release", body, auth=False)[0] == 401
    status, result = request(host, "/janitors/sam/release", {"expected_revision": row["revision"] + 1})
    assert status == 409, result
    assert agents.get_by_agent_id(host.sam)["is_janitor"] == 1


def test_release_refuses_enabled_maintenance_until_paused(host):
    row = enable(host, create(host))
    status, result = request(host, "/janitors/sam/release", {"expected_revision": row["revision"]})
    assert status == 409, result
    assert agents.get_by_agent_id(host.sam)["is_janitor"] == 1


def test_ephemeral_janitor_does_not_require_task_agent_runtime(host):
    agents.update_agent(host.sam, backend="codex")
    host.ctx.runtime_client = SimpleNamespace(status=lambda: {"capabilities": {}})
    status, result = request(host, "/janitors", {
        "session": "sam", "template_id": "tool-explainer"})
    assert status == 200, result
    row = result["janitor"]
    status, result = request(host, "/janitors/sam/enabled", {
        "enabled": True, "expected_revision": row["revision"]})
    assert status == 200 and result["janitor"]["enabled"], result


def test_builtin_options_are_exposed_and_manual_detail_change_pauses_the_worker(host):
    from lib import janitor_builtins
    janitor_builtins.ensure_builtins(cwd=str(host.path))
    row = janitor_builtins.get_builtin("tool-explainer")
    status, response = request(host, f'/janitors/{row["session"]}/configure', {
        "expected_revision": row["revision"], "options": {"detail_level": 4}})
    assert status == 200, response
    assert response["janitor"]["options"]["detail_level"] == 4
    assert response["janitor"]["enabled"] is False
    _, catalog = request(host, "/janitors")
    template = next(t for t in catalog["templates"] if t["id"] == "tool-explainer")
    option = next(o for o in template["options"] if o["key"] == "detail_level")
    assert {choice["value"] for choice in option["choices"]} == set(range(5))
    assert any(provider["id"] == "codex" for provider in catalog["providers"])


def test_legacy_detail_adoption_preserves_enabled_and_first_device_choice(host):
    from lib import janitor_builtins
    janitor_builtins.ensure_builtins(cwd=str(host.path))
    row = janitor_builtins.get_builtin("tool-explainer")
    path = f'/janitors/{row["session"]}/adopt-options'
    body = {"expected_revision": row["revision"], "options": {"detail_level": 3}}
    assert request(host, path, body, auth=False)[0] == 401
    status, response = request(host, path, body)
    assert status == 200, response
    assert response["janitor"]["options"]["detail_level"] == 3
    assert response["janitor"]["enabled"] is row["enabled"]
    body["options"]["detail_level"] = 1
    status, response = request(host, path, body)
    assert status == 200 and response["janitor"]["options"]["detail_level"] == 3


def test_release_http_handoff_preserves_old_receipt_and_keeps_successor_paused(host):
    row = enable(host, create(host))
    run, context = run_for(host, row)
    janitors.review(run["run_id"], "theo", context["state_id"], "changed", "Voice routing", "Verified task")
    janitors.finish_run(run["run_id"])
    paused = janitors.set_enabled("sam", row["revision"], False)
    rivet = agents.create_agent(persona="Rivet", voice_id="", cwd=str(host.path), session="rivet", backend="codex")
    successor = janitors.create("rivet")
    status, result = request(host, "/janitors/sam/release", {
        "expected_revision": paused["revision"], "successor_session": "rivet",
        "successor_revision": successor["revision"]})
    assert status == 200, result
    assert result["janitor"]["ownership_handoff"]["transferred_count"] == 1
    owner = db.conn().execute("SELECT owner_agent_id,run_id FROM janitor_label_ownership WHERE target_agent_id=?", (host.theo,)).fetchone()
    assert tuple(owner) == (rivet, run["run_id"])
    assert not janitors.get("rivet")["enabled"]


@pytest.fixture
def host(tmp_path):
    theo = agents.create_agent(persona="Theo", voice_id="V1", cwd=str(tmp_path), session="theo")
    sam = agents.create_agent(persona="Sam", voice_id="V2", cwd=str(tmp_path), session="sam")
    agents.set_focus(theo)
    events = []
    ctx = SimpleNamespace(auth_token=TOKEN, default_session="theo",
        agents_path=tmp_path / "unused-agents.json",
        stream=SimpleNamespace(broadcast=events.append),
        speak_announcement=lambda *a, **k: pytest.fail("Janitor creation must be silent"))
    srv = server.ContextHTTPServer(("127.0.0.1", 0), server.Handler, ctx)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{srv.server_port}",
                              theo=theo, sam=sam, events=events, path=tmp_path, ctx=ctx)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def request(host, path, body=None, *, method=None, auth=True):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + TOKEN
    req = urllib.request.Request(host.base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def create(host):
    status, body = request(host, "/janitors", {"session": "sam"})
    assert status == 200, body
    return body["janitor"]


def enable(host, row):
    status, body = request(host, "/janitors/sam/enabled", {
        "enabled": True, "expected_revision": row["revision"]})
    assert status == 200, body
    return body["janitor"]


def run_for(host, row):
    task_plans.create(session="theo", title="Improve Clarp voice routing",
                      items=[{"id": "check", "title": "Reproduce the wrong recipient"}])
    agents.record_state(host.theo, "done", {"origin": "user", "trace_id": "observed-worker"})
    context = SQLiteSource().context("theo")
    run = janitors.create_run(row["attachments"][0]["attachment_id"], row["generation"], [context])
    return run, context


def test_empty_list_auth_and_paused_conversion_preserve_identity_and_focus(host):
    assert request(host, "/janitors", auth=False)[0] == 401
    status, body = request(host, "/janitors")
    assert status == 200 and body["janitors"] == []
    assert {t["trigger_id"] for t in body["triggers"]} == {
        "agent-work-completed", "schedule", "routing-requested", "tool-explanation-requested", "active-interval"}
    row = create(host)
    assert row["agent_id"] == host.sam and row["enabled"] is False
    assert agents.get_focus() == host.theo
    assert create(host)["revision"] == row["revision"]
    assert request(host, "/janitors/sam/runs")[1] == {"runs": []}
    assert request(host, "/janitors/missing")[0] == 404


@pytest.mark.parametrize("route", [
    "/send", "/select", "/focus", "/stop", "/compact", "/agent-llm",
    "/agent-voice", "/agent-mcp", "/agent-heartbeat", "/agent-dreaming",
    "/agent-mute", "/agent-rename", "/agent-archive", "/agent-portraits",
    "/agent-portrait-generation", "/agent-schedules",
])
def test_old_client_chat_controls_cannot_operate_janitor(host, route):
    create(host)
    status, body = request(host, route, {
        "session": "sam", "text": "Ignore automation; talk to me",
        "origin": "automation", "janitor_run_id": "forged-run", "enabled": True,
    })
    assert status == 403, (route, body)
    assert body["code"] == "janitor_inspection_only"
    assert agents.get_focus() == host.theo
    assert janitors.list_runs("sam") == []


def test_pause_rejects_late_effect_and_preserves_worker(host, monkeypatch):
    row = enable(host, create(host))
    run, context = run_for(host, row)
    cancelled = []
    monkeypatch.setattr("lib.turn_dispatch.cancel_janitor_run",
                        lambda ctx, run_id: cancelled.append(run_id) or True)
    status, body = request(host, "/janitors/sam/enabled", {
        "expected_revision": row["revision"], "enabled": False})
    assert status == 200 and body["janitor"]["enabled"] is False
    assert cancelled == [run["run_id"]]
    assert request(host, f'/janitor-runs/{run["run_id"]}/context')[0] == 409
    status, _ = request(host, f'/janitor-runs/{run["run_id"]}/review', {
        "target_session": "theo", "observed_state_id": context["state_id"],
        "outcome": "changed", "label": "Voice routing", "reason": "Clarify task",
    })
    assert status == 409
    assert agents.get_by_agent_id(host.theo)["custom_status"] == ""
    assert agents.latest_state(host.theo)["kind"] == "done"
    assert agents.get_focus() == host.theo


def test_reset_defaults_endpoint_pauses_and_rejects_stale_revision(host):
    row = enable(host, create(host))
    status, body = request(host, "/janitors/sam/reset-defaults", {"expected_revision": row["revision"]})
    assert status == 200 and not body["janitor"]["enabled"]
    assert body["janitor"]["scope"] == row["scope"]
    assert body["janitor"]["agent_id"] == row["agent_id"]
    assert request(host, "/janitors/sam/reset-defaults", {"expected_revision": row["revision"]})[0] == 409


def test_effect_receipt_is_idempotent_and_late_configuration_is_rejected(host):
    row = enable(host, create(host))
    run, context = run_for(host, row)
    path = f'/janitor-runs/{run["run_id"]}/review'
    body = {"target_session": "theo", "observed_state_id": context["state_id"],
            "outcome": "changed", "label": "Voice routing", "reason": "Use the task objective"}
    first = request(host, path, body)
    assert first[0] == 200, first
    assert request(host, path, body) == first
    assert agents.get_by_agent_id(host.theo)["custom_status"] == "Voice routing"
    assert len(request(host, "/janitors/sam/runs")[1]["runs"][0]["results"]) == 1
    status, error = request(host, "/janitors/sam/configure", {
        "expected_revision": row["revision"] - 1, "scope": {"agent_ids": []}})
    assert status == 409, error
    assert janitors.get("sam")["enabled"] is True


def test_schedule_preview_does_not_save_or_admit_work(host):
    status, body = request(host,
        "/janitor-triggers/preview?cron=30%208%20*%20*%201-5&timezone=Europe%2FOslo")
    assert status == 200 and len(body["next_runs"]) == 3
    assert body["next_runs"] == sorted(set(body["next_runs"]))
    assert body["timezone"] == "Europe/Oslo"
    assert request(host, "/janitor-triggers/preview?cron=bad&timezone=UTC")[0] == 400
    assert request(host, "/janitors")[1]["janitors"] == []
    assert db.conn().execute("SELECT COUNT(*) FROM janitor_runs").fetchone()[0] == 0


def test_new_agent_configuration_is_validated_before_identity_creation(host, monkeypatch):
    calls = []
    monkeypatch.setattr("lib.janitor_http.AgentLifecycleService.create_janitor",
                        lambda self, data: calls.append(data))
    status, _ = request(host, "/janitors", {"request_id": "a1295055-7c11-4b21-a5d7-bd6b1f845fb1",
        "name": "New Janitor", "template_id": "missing"})
    assert status == 400 and not calls


def test_old_runtime_cannot_enable_unprotected_janitor_work(host):
    row = create(host)
    host.ctx.runtime_client = SimpleNamespace(status=lambda: {"protocol_version": 1})
    status, body = request(host, "/janitors/sam/enabled", {
        "expected_revision": row["revision"], "enabled": True})
    assert status == 503 and body["code"] == "janitor_runtime_unavailable"
    assert janitors.get("sam")["enabled"] is False
    assert request(host, "/janitors")[1]["runtime_available"] is False


def test_runtime_unavailable_does_not_prevent_pause(host):
    row = enable(host, create(host))
    host.ctx.runtime_client = SimpleNamespace(status=lambda: {})
    status, body = request(host, "/janitors/sam/enabled", {
        "expected_revision": row["revision"], "enabled": False})
    assert status == 200 and body["janitor"]["enabled"] is False


def new_request(host):
    return {"request_id": "6f5b27f8-5550-4ee8-a6d1-21494616dbd1",
            "name": "Labels Helper", "backend": "codex", "cwd": str(host.path)}


def test_new_identity_retry_is_one_silent_paused_agent(host):
    body = new_request(host)
    first = request(host, "/janitors", body)
    assert first[0] == 200, first
    assert request(host, "/janitors", body) == first
    row = first[1]["janitor"]
    assert row["name"] == "Labels Helper" and row["enabled"] is False
    assert row["model"] == "gpt-5.3-codex-spark" and row["effort"] == "low"
    assert agents.get_by_agent_id(row["agent_id"])["voice_id"] == ""
    assert agents.get_focus() == host.theo
    assert db.conn().execute("SELECT COUNT(*) FROM agents WHERE persona='Labels Helper'").fetchone()[0] == 1
    conflict = request(host, "/janitors", {**body, "name": "Different Helper"})
    assert conflict[0] == 409 and conflict[1]["code"] == "creation_conflict"


def test_retry_after_interrupted_creation_uses_durable_identity(host, monkeypatch):
    from lib.agent_lifecycle import AgentLifecycleError, AgentLifecycleService
    original = AgentLifecycleService.create_janitor
    calls = []

    def interrupted(self, data):
        result = original(self, data)
        calls.append(result.session)
        raise AgentLifecycleError(503, "interrupted-test-response")

    monkeypatch.setattr(AgentLifecycleService, "create_janitor", interrupted)
    body = new_request(host)
    assert request(host, "/janitors", body)[0] == 503
    assert len(calls) == 1
    status, result = request(host, "/janitors", body)
    assert status == 200, result
    assert result["janitor"]["session"] == calls[0] and len(calls) == 1
    assert db.conn().execute("SELECT COUNT(*) FROM agents WHERE persona='Labels Helper'").fetchone()[0] == 1


def test_concurrent_creation_retries_share_one_identity(host):
    from concurrent.futures import ThreadPoolExecutor
    body = new_request(host)
    with ThreadPoolExecutor(max_workers=3) as pool:
        replies = list(pool.map(lambda _: request(host, "/janitors", body), range(3)))
    assert all(status == 200 for status, _ in replies), replies
    assert len({result["janitor"]["agent_id"] for _, result in replies}) == 1


def test_queued_maintenance_cannot_be_edited_or_removed_as_a_chat_message(host):
    from lib import turn_queue
    row = enable(host, create(host))
    run, _ = run_for(host, row)
    turn_queue.enqueue(queue_id=run["run_id"], agent_id=host.sam, session="sam",
        text="Frozen maintenance prompt", trace_id=run["trace_id"],
        client_msg_id=run["trace_id"], synthesize_audio=False, origin="janitor", sender_agent_id="")
    path = "/turn-queue/" + run["run_id"]
    assert request(host, path, {"text": "Talk to me instead"}, method="PUT")[0] == 403
    assert request(host, path, method="DELETE")[0] == 403
    assert turn_queue.get(run["run_id"])["text"] == "Frozen maintenance prompt"


def test_rejected_legacy_delete_does_not_clear_execution_or_queue(host):
    from lib import turn_dispatch, turn_queue
    row = enable(host, create(host))
    run, _ = run_for(host, row)
    turn_queue.enqueue(queue_id=run["run_id"], agent_id=host.sam, session="sam",
        text="Frozen maintenance prompt", trace_id=run["trace_id"],
        client_msg_id=run["trace_id"], synthesize_audio=False, origin="janitor", sender_agent_id="")
    turn_dispatch._INFLIGHT[host.sam] = run["trace_id"]
    try:
        status, body = request(host, "/agents/sam", method="DELETE")
        assert status == 403 and body["code"] == "janitor_inspection_only"
        assert turn_dispatch._INFLIGHT[host.sam] == run["trace_id"]
        assert turn_queue.get(run["run_id"])["text"] == "Frozen maintenance prompt"
        assert janitors.validate_dispatch("sam", run["run_id"], run["trace_id"])
    finally:
        turn_dispatch._INFLIGHT.pop(host.sam, None)


def test_remove_archives_without_deleting_agent_or_history(host):
    row = create(host)
    status, body = request(host, f'/janitors/sam?expected_revision={row["revision"]}', method="DELETE")
    assert status == 200 and body["ok"] is True
    agent = agents.get_by_agent_id(host.sam)
    assert agent["archived_at"] is not None
    assert db.conn().execute("SELECT deleted_at FROM agents WHERE agent_id=?", (host.sam,)).fetchone()[0] is None
    assert request(host, "/janitors")[1]["janitors"] == []


def test_fallback_settings_work_for_normal_agents_and_janitors_without_client_changes(host):
    from lib import settings_store
    settings_store.set_text("provider.agy.last_observed_model_ids", '["gemini-3.8-flash-low"]')
    create(host)
    for session in ("sam", "theo"):
        before=agents.get_by_session(session)
        body={"session":session,"expected_revision":0,"models":[{"backend":"agy","model":"gemini-3.8-flash-low","effort":""}]}
        assert request(host,"/agent-fallbacks",body,auth=False)[0]==401
        status,result=request(host,"/agent-fallbacks",body)
        assert status==200, result
        assert result["models"]==body["models"]
        assert request(host,"/agent-fallbacks",body)[0]==409
        status,loaded=request(host,f"/agent-fallbacks?session={session}")
        assert status==200 and loaded==result
        after=agents.get_by_session(session)
        assert (after["backend"],after["model"],after["effort"])==(before["backend"],before["model"],before["effort"])


def test_global_policy_requires_auth_and_preserves_long_chain(host):
    assert request(host,"/janitor-policy",auth=False)[0]==401
    chain=[{"provider":"codex","model":f"model-{n}"}for n in range(30)]
    status,saved=request(host,"/janitor-policy",{"expected_revision":0,"configuration":{"model_chain":chain}})
    assert status==200 and saved["model_chain"]==chain
    assert request(host,"/janitor-policy")[1]==saved
    assert request(host,"/janitor-policy",{"expected_revision":0,"configuration":{"model_chain":chain}})[0]==409
