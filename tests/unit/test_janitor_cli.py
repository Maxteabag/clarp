from __future__ import annotations

import importlib.util
import io
import json
import uuid
from pathlib import Path
from urllib.error import HTTPError

import pytest

from lib import janitor_cli


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("janitor_admin", ROOT / "bin/clarp-admin.py")
admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admin)


def invoke(argv, replies=()):
    calls = []
    responses = iter(replies)

    def request(method, path, body=None):
        calls.append((method, path, body))
        response = next(responses, {})
        if isinstance(response, Exception):
            raise response
        return response

    args = admin.parser().parse_args(argv)
    janitor_cli.execute(args, request)
    return calls


def test_create_converts_existing_identity_without_enabling_or_changing_model():
    calls = invoke(["janitor", "create", "--agent", "sam-042d", "--paused"])
    assert calls == [("POST", "/janitors", {"session": "sam-042d", "template_id": "task-labels"})]


def test_release_uses_explicit_revision_and_does_not_archive_the_agent():
    calls = invoke(["janitor", "release", "sam-042d", "--expected-revision", "4"])
    assert calls == [("POST", "/janitors/sam-042d/release", {"expected_revision": 4})]


def test_release_can_explicitly_handoff_maintenance_to_another_paused_robot():
    calls = invoke(["janitor", "release", "sam", "--expected-revision", "4",
                    "--successor", "rivet", "--successor-revision", "1"])
    assert calls == [("POST", "/janitors/sam/release", {
        "expected_revision": 4, "successor_session": "rivet", "successor_revision": 1})]


def test_create_new_agent_requires_complete_identity():
    with pytest.raises(SystemExit, match="provide --agent"):
        invoke(["janitor", "create", "--name", "Sam"])


def test_create_dry_run_is_offline_and_always_paused(capsys):
    assert invoke(["janitor", "create", "--agent", "sam", "--dry-run"]) == []
    assert json.loads(capsys.readouterr().out)["body"] == {"session": "sam", "template_id": "task-labels"}
    with pytest.raises(SystemExit, match="unsupported configuration fields: enabled"):
        invoke(["janitor", "create", "--agent", "sam", "--config", '{"enabled":true}'])


def test_new_creation_emits_recoverable_uuid_before_sending(capsys):
    args = admin.parser().parse_args(["janitor", "create", "--name", "Sam", "--backend", "codex", "--cwd", "/tmp"])
    calls = []

    def request(method, path, body=None):
        emitted = json.loads(capsys.readouterr().err)
        assert emitted["request_id"] == body["request_id"]
        assert str(uuid.UUID(body["request_id"])) == body["request_id"]
        calls.append(body)
        return {"janitor": {"session": "sam", "enabled": False}}

    janitor_cli.execute(args, request)
    assert len(calls) == 1


def test_new_creation_retry_and_dry_run_preserve_supplied_request_id(capsys):
    identifier = "69b634f8-835f-4281-8607-eece6a9d7f9e"
    command = ["janitor", "create", "--name", "Sam", "--backend", "codex", "--cwd", "/tmp", "--request-id", identifier]
    assert invoke([*command, "--dry-run"]) == []
    dry = json.loads(capsys.readouterr().out)["body"]
    sent = invoke(command)[0][2]
    assert dry == sent and sent["request_id"] == identifier


def test_new_creation_accepts_request_id_in_structured_config(capsys):
    body = {"name": "Sam", "backend": "codex", "cwd": "/tmp", "request_id": "69b634f8-835f-4281-8607-eece6a9d7f9e"}
    assert invoke(["janitor", "create", "--config", json.dumps(body)])[0][2]["request_id"] == body["request_id"]


def test_creation_rejects_invalid_uuid_and_conversion_does_not_use_new_identity_ledger():
    with pytest.raises(SystemExit, match="must be a UUID"):
        invoke(["janitor", "create", "--name", "Sam", "--backend", "codex", "--cwd", "/tmp", "--request-id", "not-a-uuid"])
    with pytest.raises(SystemExit, match="only for creating a new identity"):
        invoke(["janitor", "create", "--agent", "sam", "--request-id", "69b634f8-835f-4281-8607-eece6a9d7f9e"])


def test_structured_configuration_preserves_literal_shell_metacharacters(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model": "literal-$(not-a-command)", "scope": {"agent_ids": ["worker"]}}))
    calls = invoke(["janitor", "configure", "sam", "--expected-revision", "3", "--config", "@" + str(path)])
    assert calls == [("POST", "/janitors/sam/configure", {
        "expected_revision": 3, "model": "literal-$(not-a-command)", "scope": {"agent_ids": ["worker"]}})]


def test_attach_retains_other_attachments_and_pins_observed_revision():
    current = {"revision": 4, "attachments": [{"attachment_id": "old", "trigger_id": "schedule",
               "trigger_version": 1, "enabled": True, "config": {"cron": "@daily", "timezone": "UTC"},
               "next_run_at": 123}]}
    calls = invoke(["janitor", "attach", "sam", "--trigger", "agent-work-completed@1"], [{"janitor": current}])
    body = calls[-1][2]
    assert body["expected_revision"] == 4
    assert body["attachments"][0]["attachment_id"] == "old"
    assert "next_run_at" not in body["attachments"][0]
    assert body["attachments"][1] == {"trigger_id": "agent-work-completed", "trigger_version": 1,
                                      "enabled": True, "config": {}}


@pytest.mark.parametrize("trigger", ["schedule", "schedule@0", "schedule@x", "@1"])
def test_attach_rejects_unversioned_or_invalid_trigger(trigger):
    with pytest.raises(SystemExit, match="versioned identifier"):
        invoke(["janitor", "attach", "sam", "--trigger", trigger])


def test_stale_enable_is_not_retried_or_rebased():
    args = admin.parser().parse_args(["janitor", "enable", "sam"])
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return {"janitor": {"revision": 3}}
        raise HTTPError(path, 409, "Conflict", {}, io.BytesIO(b'{"error":"revision changed"}'))

    with pytest.raises(SystemExit, match="nothing was retried"):
        janitor_cli.execute(args, request)
    assert len(calls) == 2
    assert calls[-1][2] == {"expected_revision": 3, "enabled": True}


def test_old_host_is_not_reported_as_empty_success():
    with pytest.raises(SystemExit, match="may not support Janitors"):
        invoke(["janitor", "list"], [HTTPError("/janitors", 404, "Missing", {}, io.BytesIO(b'{}'))])


def test_run_review_uses_observation_and_run_membership_contract():
    calls = invoke(["janitor", "review", "run/one", "--session", "hugo", "--state-id", "99",
                    "--outcome", "changed", "--label", "Car mode UX", "--reason", "Current request"])
    assert calls == [("POST", "/janitor-runs/run%2Fone/review", {
        "target_session": "hugo", "observed_state_id": 99, "outcome": "changed",
        "label": "Car mode UX", "reason": "Current request"})]


@pytest.mark.parametrize("label", ["Too many words here", "An extremelylonglabel", "One"])
def test_review_rejects_labels_outside_caption_contract(label):
    with pytest.raises(SystemExit, match="2–3 words"):
        invoke(["janitor", "review", "run", "--session", "hugo", "--state-id", "99",
                "--outcome", "changed", "--label", label, "--reason", "test"])


@pytest.mark.parametrize("argv,path", [
    (["janitor", "inspect", "sam"], "/janitors/sam"),
    (["janitor", "runs", "sam", "--limit", "12"], "/janitors/sam/runs?limit=12"),
    (["janitor", "run-context", "run"], "/janitor-runs/run/context"),
    (["janitor", "export", "sam"], "/janitors/sam/migration-state"),
    (["trigger", "list"], "/janitor-triggers"),
])
def test_read_commands_are_read_only(argv, path):
    assert invoke(argv) == [("GET", path, None)]


def test_remove_uses_guarded_archive_route():
    assert invoke(["janitor", "remove", "sam", "--expected-revision", "5"]) == [
        ("DELETE", "/janitors/sam?expected_revision=5", None)]


def test_caption_rule_removed_from_shared_persona_but_background_lifecycle_retained():
    from lib.codex_runner import persona_identity_instruction
    text = persona_identity_instruction("Theo", session="theo")
    assert "Use the installed `clarp-background-jobs` skill" in text
    assert "work that continues after your final response" in text
    assert "Detached statuses" not in text
    assert "Never set a visible status" not in text
    skill = (ROOT / "skills/clarp-background-jobs/SKILL.md").read_text()
    assert "short header status" not in skill
    for guard in ("generation-specific handle", "job-heartbeat", "job-finish", "job-fail", "Cancellation is sticky"):
        assert guard in skill


def test_managed_janitor_skill_is_distributed_with_its_helper():
    manifest = json.loads((ROOT / "skills/manifest.json").read_text())
    assert any(s["id"] == "clarp-janitors" and s["pack"] == "core" for s in manifest["skills"])
    skill = ROOT / "skills/clarp-janitors"
    assert (skill / "scripts/janitor_run.py").is_file()
    assert (skill / "references/pilot-migration.md").is_file()


def test_leader_instructions_preserve_progress_and_permission_duties_without_caption_formatting():
    from lib.leader_memory import LEADER_STANDING_ORDERS_V2
    assert "Visible self-statuses" not in LEADER_STANDING_ORDERS_V2
    assert "Evidence first" in LEADER_STANDING_ORDERS_V2
    assert "Explicit ownership" in LEADER_STANDING_ORDERS_V2
    assert "Merge, release, publish, or deploy authority" in LEADER_STANDING_ORDERS_V2
