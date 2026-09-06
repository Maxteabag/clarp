from __future__ import annotations

import argparse
import json
import stat
import subprocess

import pytest

from lib import janitor_migration as migration


@pytest.fixture
def pilot(tmp_path):
    root = tmp_path / "pilot"
    root.mkdir()
    files = {
        "session.json": {"session": "sam"},
        "event-watch-state.json": {"cursor": 42, "pending": {}, "deferred": {}, "seen": ["trace"],
                                   "delivery": None, "private_prompt": "do not export"},
        "event-watch-job.json": {"handle": "bg1:2:sam-label-events", "pid": 123, "active": True},
        "owned.json": {"hugo": {"label": "Car mode UX", "source_state_id": 40}},
        "reviews.json": {"hugo": {"state_id": 40, "outcome": "same_task", "unchanged_streak": 1,
                                   "next_eligible_at": 180.5, "private_prompt": "do not export"}},
        "settings.json": {"exclude_sessions": ["theo"]},
    }
    for name, value in files.items():
        (root / name).write_text(json.dumps(value))
    (root / "review-results.jsonl").write_text(json.dumps({"session": "hugo", "state_id": 40,
        "outcome": "same_task", "label": "Car mode UX", "reason": "Still appropriate", "reviewed_at": 60.5}) + "\n")
    return root


class Host:
    def __init__(self, pilot):
        self.pilot = pilot
        self.events = []
        self.service_active = True
        self.main_pid = 123
        self.service_extra = ""
        self.janitor = None
        self.cron = {"schedule_id": "old-cron", "session": "sam", "agent_id": "sam-id",
                     "cron_expression": "*/5 * * * *", "enabled": False}
        self.agents = [{"session": "sam", "agent_id": "sam-id", "busy": False,
                        "backend": "codex", "backend_session_id": "native-history",
                        "model": "gpt-5.3-codex-spark", "effort": "low"},
                       {"session": "hugo", "agent_id": "hugo-id", "status_text": "Car mode UX"},
                       {"session": "theo", "agent_id": "theo-id"}]

    def request(self, method, path, body=None):
        self.events.append((method, path, body))
        if path == "/janitors" and method == "GET":
            return {"janitors": [self.janitor] if self.janitor else [], "templates": []}
        if path == "/agents/snapshot":
            return {"agents": self.agents}
        if path.startswith("/agent-schedules?"):
            return {"schedules": [self.cron]}
        if path == "/agent-schedules/toggle":
            assert body == {"schedule_id": "old-cron", "enabled": False}
            self.cron["enabled"] = False
            return {"ok": True}
        if path == "/janitors" and method == "POST":
            assert not self.service_active
            assert self.cron["enabled"] is False
            assert "model" not in body and "backend" not in body
            self.janitor = {"session": "sam", "agent_id": "sam-id", "enabled": False,
                            "revision": 1, "template_id": "task-labels", **body}
            return {"janitor": self.janitor}
        if path == "/janitors/sam":
            return {"janitor": self.janitor}
        if path.endswith("/migration-state"):
            return {"expected_revision": self.janitor["revision"], "progress": {}, "ownership": [], "receipts": []}
        if path.endswith("/import-pilot"):
            assert not self.service_active
            assert body["expected_agent_id"] == "sam-id"
            assert body["expected_revision"] == 1
            assert body["progress"]["cursor"] == 42
            assert "private_prompt" not in json.dumps(body)
            return {"ok": True, "import_id": body["import_id"]}
        raise AssertionError((method, path, body))

    def systemctl(self, command, **kwargs):
        self.events.append(("systemctl", command, None))
        if "disable" in command:
            assert command == ["systemctl", "--user", "disable", "--now", "sam-events.service"]
            self.service_active = False
            self.main_pid = 0
            return subprocess.CompletedProcess(command, 0, "", "")
        assert command[:4] == ["systemctl", "--user", "show", "sam-events.service"]
        expected = (f"/usr/bin/python3 {self.pilot}/watch_events.py --state-dir {self.pilot} "
                    "--agent-id sam-id --job-id sam-label-events")
        values = {"Id": "sam-events.service", "WorkingDirectory": str(self.pilot),
                  "ExecStart": "{ path=/bin/bash ; argv[]=/bin/bash -c " + expected + self.service_extra + " ; ignore_errors=no ; }",
                  "ActiveState": "active" if self.service_active else "inactive", "MainPID": str(self.main_pid)}
        return subprocess.CompletedProcess(command, 0, "\n".join(f"{k}={v}" for k, v in values.items()), "")


def args(pilot, **overrides):
    return argparse.Namespace(session="sam", agent_id="sam-id", pilot_dir=str(pilot),
                              service="sam-events.service", cron_id="old-cron",
                              **{"apply": False, "backup": None, **overrides})


def test_default_preview_makes_no_mutation_or_filesystem_write(pilot):
    host = Host(pilot)
    before = {p.name: p.read_bytes() for p in pilot.iterdir()}
    preview = migration.migrate(args(pilot), host.request, runner=host.systemctl)
    assert preview["dry_run"] is True
    assert preview["cursor"] == 42
    assert preview["omitted_ownership"] == []
    assert all(event[0] == "GET" or (event[0] == "systemctl" and "show" in event[1]) for event in host.events)
    assert before == {p.name: p.read_bytes() for p in pilot.iterdir()}


def test_apply_stops_exact_listener_before_conversion_import_and_preserves_identity(pilot, tmp_path):
    host = Host(pilot)
    host.cron["enabled"] = True
    backup = tmp_path / "private-backup.json"
    result = migration.migrate(args(pilot, apply=True, backup=str(backup)), host.request, runner=host.systemctl)
    assert result["janitor"]["enabled"] is False
    assert host.janitor["scope"]["exclude_agent_ids"] == ["theo-id"]
    assert host.agents[0]["backend_session_id"] == "native-history"
    assert host.agents[0]["model"] == "gpt-5.3-codex-spark"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    saved = json.loads(backup.read_text())
    assert saved["phase"] == "stopped-before-import"
    assert saved["pilot"]["progress"]["reviews"]["hugo"]["next_eligible_at"] == 180.5
    actions = [(event[0], event[1]) for event in host.events]
    stopped = next(i for i, e in enumerate(actions) if e[0] == "systemctl" and "disable" in e[1])
    imported = next(i for i, e in enumerate(actions) if e == ("POST", "/janitors/sam/import-pilot"))
    assert stopped < imported
    assert not any(path.endswith("/enabled") for method, path, _ in host.events if method == "POST")


@pytest.mark.parametrize("bad", ["identity", "busy", "queued", "cron-owner", "cron-expression", "service-extra", "pid"])
def test_mismatched_or_active_ownership_blocks_before_stopping(pilot, tmp_path, bad):
    host = Host(pilot)
    if bad == "identity": host.agents[0]["agent_id"] = "someone-else"
    if bad == "busy": host.agents[0]["busy"] = True
    if bad == "queued": host.agents[0]["queued_turn_count"] = 1
    if bad == "cron-owner": host.cron["session"] = "other"
    if bad == "cron-expression": host.cron["cron_expression"] = "@daily"
    if bad == "service-extra": host.service_extra = " ; /bin/other"
    if bad == "pid": host.main_pid = 444
    with pytest.raises(ValueError):
        migration.migrate(args(pilot, apply=True, backup=str(tmp_path / "backup")), host.request, runner=host.systemctl)
    assert host.service_active is True
    assert not any(e[0] == "POST" or (e[0] == "systemctl" and "disable" in e[1]) for e in host.events)


def test_manual_label_override_is_reported_and_never_reclaimed(pilot, tmp_path):
    host = Host(pilot)
    host.agents[1]["status_text"] = "My label"
    preview = migration.migrate(args(pilot), host.request, runner=host.systemctl)
    assert preview["omitted_ownership"] == [{"target_session": "hugo", "reason": "label no longer matches"}]
    migration.migrate(args(pilot, apply=True, backup=str(tmp_path / "backup")), host.request, runner=host.systemctl)
    imported = next(e[2] for e in host.events if e[:2] == ("POST", "/janitors/sam/import-pilot"))
    assert imported["ownership"] == []
    assert host.agents[1]["status_text"] == "My label"
    assert json.loads((tmp_path / "backup").read_text())["pilot"]["ownership"][0]["label"] == "Car mode UX"


def test_inflight_delivery_prevents_migration(pilot):
    state = json.loads((pilot / "event-watch-state.json").read_text())
    state["delivery"] = {"accepted": False, "id": "ambiguous"}
    (pilot / "event-watch-state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="ambiguous delivery"):
        migration.read_pilot(pilot, "sam")


def test_existing_backup_is_never_overwritten_or_stopped(pilot, tmp_path):
    host = Host(pilot)
    backup = tmp_path / "backup"
    backup.write_text("existing")
    with pytest.raises(FileExistsError):
        migration.migrate(args(pilot, apply=True, backup=str(backup)), host.request, runner=host.systemctl)
    assert host.service_active
    assert backup.read_text() == "existing"


def test_enabled_replacement_is_rejected_before_touching_service(pilot):
    host = Host(pilot)
    host.janitor = {"session": "sam", "agent_id": "sam-id", "enabled": True}
    with pytest.raises(ValueError, match="paused"):
        migration.migrate(args(pilot), host.request, runner=host.systemctl)
    assert host.service_active


def test_import_hash_is_stable_and_drops_noncontract_fields(pilot):
    first = migration.read_pilot(pilot, "sam")
    second = migration.read_pilot(pilot, "sam")
    assert first["import_id"] == second["import_id"]
    assert "private_prompt" not in json.dumps(first)
    assert first["receipts"][0]["outcome"] == "same_task"
    assert first["receipts"][0]["after"] == "Car mode UX"


def test_oversized_ownership_is_not_silently_truncated(pilot):
    (pilot / "owned.json").write_text(json.dumps({str(i): {"label": "A label"} for i in range(1001)}))
    with pytest.raises(ValueError, match="1000"):
        migration.read_pilot(pilot, "sam")


def test_scope_omissions_keep_history_and_original_source(pilot):
    host = Host(pilot)
    source = migration.read_pilot(pilot, "sam")
    source["progress"]["pending"] = {"hugo": {"state_id": 41}}
    payload, omissions = migration.prepare_import(source, {"agents": host.agents},
        {"agent_ids": [], "exclude_agent_ids": ["hugo-id"]})
    assert payload["ownership"] == []
    assert payload["progress"]["pending"] == {}
    assert payload["receipts"] == source["receipts"]
    assert source["ownership"] and source["progress"]["pending"]
    assert omissions == {"omitted_ownership": [{"target_session": "hugo", "reason": "outside current scope"}],
                         "omitted_pending": ["hugo"]}


def test_pilot_payload_imports_through_real_store_and_idempotent_retry(pilot):
    from lib import agents, janitors
    sam = agents.create_agent(persona="Sam", voice_id="", cwd="/tmp", session="sam", backend="codex")
    hugo = agents.create_agent(persona="Hugo", voice_id="", cwd="/tmp", session="hugo", backend="codex")
    agents.set_custom_status(hugo, "Car mode UX")
    config = janitors.create("sam", attachments=[{"trigger_id": "agent-work-completed",
        "trigger_version": 1, "enabled": True, "config": {"coalesce_seconds": 8, "max_targets": 3}}])
    payload = migration.read_pilot(pilot, "sam")
    imported = janitors.import_pilot("sam", config["revision"], sam, **payload)
    assert imported["enabled"] is False
    assert agents.get_by_agent_id(hugo)["custom_status"] == "Car mode UX"
    assert imported["generation"] > config["generation"]
    assert janitors.import_pilot("sam", config["revision"], sam, **payload)["revision"] == imported["revision"]
    exported = janitors.export_migration("sam")
    assert exported["progress"]["cursor"] == 42
    assert exported["progress"]["reviews"]["hugo"]["next_eligible_at"] == 180.5
    assert exported["receipts"][0]["after"] == "Car mode UX"


def test_ambiguous_import_recovery_reuses_frozen_payload_despite_new_labels(pilot, tmp_path):
    host = Host(pilot)
    backup = tmp_path / "backup.json"
    migration.migrate(args(pilot, apply=True, backup=str(backup)), host.request, runner=host.systemctl)
    first = next(e[2] for e in host.events if e[:2] == ("POST", "/janitors/sam/import-pilot"))
    host.agents[1]["status_text"] = "A new label"
    host.events.clear()
    result = migration.migrate(args(pilot, apply=True, resume_backup=str(backup)), host.request, runner=host.systemctl)
    second = next(e[2] for e in host.events if e[:2] == ("POST", "/janitors/sam/import-pilot"))
    assert first == second
    assert result["import_id"] == first["import_id"]
    assert not any(e[0] == "systemctl" and "disable" in e[1] for e in host.events)


def test_recovery_refuses_if_old_listener_was_restarted(pilot, tmp_path):
    host = Host(pilot)
    backup = tmp_path / "backup.json"
    migration.migrate(args(pilot, apply=True, backup=str(backup)), host.request, runner=host.systemctl)
    host.service_active = True
    host.main_pid = 123
    host.events.clear()
    with pytest.raises(ValueError, match="old service and cron off"):
        migration.migrate(args(pilot, apply=True, resume_backup=str(backup)), host.request, runner=host.systemctl)
    assert not any(e[0] == "POST" for e in host.events)
