"""Maintenance ownership and generation checks using real isolated SQLite."""
from __future__ import annotations

import hashlib
import json

import pytest

from lib import agents, backends, db, janitors, scheduler, turn_queue
from lib.janitor_context import build_context_from_connection


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *args: [])


def agent(session="sam", *, plan=False):
    aid = agents.create_agent(persona=session.title(), voice_id="v", cwd="/tmp/clarp-ios", session=session,
                              backend="codex", model="gpt-5.3-codex-spark", effort="low")
    agents.record_state(aid, "done", {"origin": "user", "trace_id": session + "-initial"})
    if plan:
        db.conn().execute("INSERT INTO task_plans(plan_id,agent_id,session,title,status,created_at,updated_at) VALUES (?,?,?,'Improve Clarp car mode','active',1,1)", ("plan-" + session, aid, session))
    return aid


def setup():
    sam, hugo = agent(), agent("hugo", plan=True)
    cfg = janitors.create("sam", attachments=[{"trigger_id": "agent-work-completed", "config": {"coalesce_seconds": 0}}])
    cfg = janitors.set_enabled("sam", cfg["revision"], True)
    return sam, hugo, cfg


def admit(cfg, session="hugo", **kwargs):
    context = build_context_from_connection(db.conn(), session)
    return janitors.create_run(cfg["attachments"][0]["attachment_id"], cfg["generation"], [context], **kwargs)


def review(run, label="Car mode UX", outcome="changed"):
    return janitors.review(run["run_id"], "hugo", run["candidates"][0]["state_id"], outcome,
                           label=label, reason="Names the actual iPhone feature")


def test_conversion_keeps_identity_runtime_history_and_model():
    sam = agent()
    rid = agents.start_runtime(sam, "sam")
    agents.bind_backend_session(sam, "same-native-conversation")
    before = db.conn().execute("SELECT COUNT(*) FROM state_log").fetchone()[0]
    created = janitors.create("sam")
    assert created["agent_id"] == sam and not created["enabled"]
    assert created["model"] == "gpt-5.3-codex-spark"
    assert created["effort"] == "low"
    assert agents.live_backend_session(sam) == "same-native-conversation"
    assert db.conn().execute("SELECT COUNT(*) FROM state_log").fetchone()[0] == before
    assert agents.get_by_session("sam")["is_janitor"] == 1
    assert janitors.create("sam", scope={"agent_ids": ["unknown"]}) == created
    assert agents.session_dict()["sam"]["interaction_capabilities"] == {
        "can_chat": False, "can_voice_target": False, "can_restart": False, "can_inspect": True}


def test_active_interval_config_and_reset_preserve_scope_and_pause():
    sam, worker = agent(), agent("worker")
    cfg = janitors.create("sam", scope={"agent_ids": [worker]}, attachments=[{
        "trigger_id": "active-interval", "config": {"interval_seconds": 120, "idle_timeout_seconds": 60, "run_on_resume": False}}])
    cfg = janitors.set_enabled("sam", cfg["revision"], True)
    reset = janitors.reset_defaults("sam", cfg["revision"])
    assert reset["agent_id"] == sam and reset["scope"] == cfg["scope"]
    assert not reset["enabled"] and reset["generation"] > cfg["generation"]
    assert reset["attachments"][0]["config"]["interval_seconds"] == 900
    assert reset["attachments"][0]["config"]["run_on_resume"] is True
    with pytest.raises(janitors.JanitorError, match="changed"):
        janitors.reset_defaults("sam", cfg["revision"])


@pytest.mark.parametrize("config", [{"interval_seconds": 0}, {"idle_timeout_seconds": True}, {"run_on_resume": "true"}])
def test_active_interval_rejects_invalid_parameters(config):
    agent()
    with pytest.raises(janitors.JanitorError):
        janitors.create("sam", attachments=[{"trigger_id": "active-interval", "config": config}])


def test_existing_ordinary_agents_and_cron_are_unchanged():
    worker = agent("josh")
    schedule = scheduler.create_schedule("josh", "Normal job", "@daily", "Work")
    assert not agents.get_by_agent_id(worker)["is_janitor"]
    assert all(agents.interaction_capabilities(agents.get_by_agent_id(worker)).values())
    assert scheduler.get_schedule(schedule["schedule_id"]) == schedule
    assert janitors.list_janitors() == []


@pytest.mark.parametrize("busy_kind", ["thinking", "tool", "compacting"])
def test_conversion_rejects_busy_workers_without_mutating(busy_kind):
    sam = agent()
    agents.record_state(sam, busy_kind)
    with pytest.raises(janitors.JanitorError, match="current work"):
        janitors.create("sam")
    assert not agents.get_by_agent_id(sam)["is_janitor"]


def test_conversion_rejects_queued_ordinary_work():
    sam = agent()
    turn_queue.enqueue(queue_id="q", agent_id=sam, session="sam", text="Real task", trace_id="q",
                       client_msg_id="q", synthesize_audio=True, origin="user", sender_agent_id="")
    with pytest.raises(janitors.JanitorError, match="current work"):
        janitors.create("sam")
    assert turn_queue.status("q") == "queued"


def test_invalid_trigger_preflight_and_create_leave_no_partial_state():
    sam = agent()
    bad = [{"trigger_id": "schedule", "config": {"cron": "0 8:30 * * 1-5", "timezone": "Europe/Oslo"}}]
    with pytest.raises(janitors.JanitorError):
        janitors.validate_configuration(attachments=bad)
    with pytest.raises(janitors.JanitorError):
        janitors.create("sam", attachments=bad)
    assert not agents.get_by_agent_id(sam)["is_janitor"]
    assert not db.conn().execute("SELECT 1 FROM janitor_configs").fetchone()


def test_configuration_revision_validation_and_fence_are_atomic():
    sam, hugo, cfg = setup()
    run = admit(cfg)
    with pytest.raises(janitors.JanitorError, match="reasoning effort"):
        janitors.configure("sam", cfg["revision"], effort="nonexistent")
    assert janitors.get_run(run["run_id"])["status"] == "queued"
    assert janitors.get("sam")["revision"] == cfg["revision"]
    new = janitors.configure("sam", cfg["revision"], effort="medium")
    assert not new["enabled"] and new["generation"] > cfg["generation"]
    assert janitors.get_run(run["run_id"])["outcome"] == "cancelled"
    with pytest.raises(janitors.JanitorError) as error:
        janitors.set_enabled("sam", cfg["revision"], True)
    assert error.value.code == "revision_conflict"


def test_scope_ownership_prevents_overlapping_current_and_future_targets():
    sam, hugo, cfg = setup()
    iris = agent("iris")
    josh = agent("josh")
    second = janitors.create("iris", scope={"agent_ids": [josh]})
    with pytest.raises(janitors.JanitorError) as error:
        janitors.set_enabled("iris", second["revision"], True)
    assert error.value.code == "scope_conflict"
    cfg = janitors.configure("sam", cfg["revision"], scope={"agent_ids": [hugo]})
    cfg = janitors.set_enabled("sam", cfg["revision"], True)
    assert janitors.set_enabled("iris", second["revision"], True)["enabled"]


def test_out_of_scope_candidate_cannot_create_run():
    sam, hugo, cfg = setup()
    josh = agent("josh", plan=True)
    cfg = janitors.configure("sam", cfg["revision"], scope={"agent_ids": [hugo]})
    cfg = janitors.set_enabled("sam", cfg["revision"], True)
    with pytest.raises(janitors.JanitorError, match="outside"):
        admit(cfg, "josh")
    assert janitors.list_runs("sam") == []


def test_frozen_admission_is_idempotent_and_single_active():
    sam, hugo, cfg = setup()
    run = admit(cfg, run_id="stable", progress={"cursor": 17})
    assert admit(cfg, run_id="stable", progress={"cursor": 99}) == run
    assert janitors.get_progress(cfg["attachments"][0]["attachment_id"])["cursor"] == 17
    with pytest.raises(janitors.JanitorError, match="active run"):
        admit(cfg)
    assert janitors.validate_dispatch("sam", "stable", "stable")
    assert not janitors.validate_dispatch("hugo", "stable", "stable")
    assert not janitors.validate_dispatch("sam", "stable", "wrong")


def test_effect_receipt_is_atomic_idempotent_and_never_changes_lifecycle():
    sam, hugo, cfg = setup()
    run = admit(cfg)
    before_state = agents.latest_state(hugo)
    receipt = review(run)
    assert receipt["before"] == "" and receipt["after"] == "Car mode UX"
    assert receipt["outcome"] == "changed"
    assert agents.latest_state(hugo) == before_state
    assert janitors.owned_label(sam, "hugo") == "Car mode UX"
    assert review(run) == receipt
    finished = janitors.finish_run(run["run_id"])
    assert finished["outcome"] == "changed"
    assert review(run) == receipt  # Lost HTTP response after turn completion.
    assert len(finished["results"]) == 1


def test_failed_receipt_insert_rolls_back_label_and_ownership():
    sam, hugo, cfg = setup()
    run = admit(cfg)
    db.conn().execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON janitor_effects BEGIN SELECT RAISE(ABORT,'receipt unavailable'); END")
    with pytest.raises(Exception, match="receipt unavailable"):
        review(run)
    assert agents.get_by_agent_id(hugo)["custom_status"] == ""
    assert janitors.owned_label(sam, "hugo") is None


def test_pause_rejects_stale_effects_and_progress_even_after_reenable():
    sam, hugo, cfg = setup()
    run = admit(cfg)
    turn_queue.enqueue(queue_id=run["run_id"], agent_id=sam, session="sam", text="Maintenance", trace_id=run["trace_id"],
                       client_msg_id=run["run_id"], synthesize_audio=False, origin="automation", sender_agent_id="")
    paused = janitors.set_enabled("sam", cfg["revision"], False)
    assert turn_queue.status(run["run_id"]) == "cancelled"
    assert not janitors.validate_dispatch("sam", run["run_id"], run["trace_id"])
    janitors.set_enabled("sam", paused["revision"], True)
    with pytest.raises(janitors.JanitorError):
        review(run)
    with pytest.raises(janitors.JanitorError, match="superseded"):
        janitors.save_progress(cfg["attachments"][0]["attachment_id"], cfg["generation"], {"cursor": 100})
    assert agents.get_by_agent_id(hugo)["custom_status"] == ""


def test_manual_identical_text_still_revokes_janitor_ownership():
    sam, hugo, cfg = setup()
    run = admit(cfg)
    review(run)
    janitors.finish_run(run["run_id"])
    agents.set_custom_status(hugo, "Car mode UX")
    assert janitors.owned_label(sam, "hugo") is None
    next_run = admit(cfg)
    with pytest.raises(janitors.JanitorError, match="another writer"):
        review(next_run, "Voice mode UX")
    assert agents.get_by_agent_id(hugo)["custom_status"] == "Car mode UX"


def test_new_task_evidence_without_state_change_rejects_effect():
    sam, hugo, cfg = setup()
    run = admit(cfg)
    db.conn().execute("UPDATE task_plans SET title='Build a weather widget',updated_at=2 WHERE agent_id=?", (hugo,))
    with pytest.raises(janitors.JanitorError, match="work changed"):
        review(run)
    assert janitors.get_run(run["run_id"])["results"] == []


def test_new_queued_work_rejects_effect_in_same_transaction():
    sam, hugo, cfg = setup()
    run = admit(cfg)
    turn_queue.enqueue(queue_id="next", agent_id=hugo, session="hugo", text="Different task", trace_id="next",
                       client_msg_id="next", synthesize_audio=True, origin="user", sender_agent_id="")
    with pytest.raises(janitors.JanitorError, match="work changed"):
        review(run)


def test_same_task_receipt_and_expiry_are_truthful(monkeypatch):
    sam, hugo, cfg = setup()
    run = admit(cfg)
    review(run)
    janitors.finish_run(run["run_id"])
    next_run = admit(cfg)
    assert review(next_run)["outcome"] == "same_task"
    assert janitors.finish_run(next_run["run_id"])["outcome"] == "same_task"
    assert hugo not in janitors.visible_labels()
    now = db.now_ms()
    monkeypatch.setattr(db, "now_ms", lambda: now + janitors.LABEL_MAX_AGE_MS + 1)
    assert janitors.visible_labels()[hugo] == ""
    assert agents.get_by_agent_id(hugo)["custom_status"] == "Car mode UX"


def test_new_task_deterministically_hides_generated_caption_but_not_manual():
    sam, hugo, cfg = setup()
    run = admit(cfg)
    review(run)
    db.conn().execute("UPDATE task_plans SET title='New task',updated_at=2 WHERE agent_id=?", (hugo,))
    assert janitors.visible_labels()[hugo] == ""
    agents.set_custom_status(hugo, "My chosen label")
    assert hugo not in janitors.visible_labels()


def test_no_receipt_cannot_claim_changed_or_success():
    sam, hugo, cfg = setup()
    run = admit(cfg)
    with pytest.raises(janitors.JanitorError, match="receipts are incomplete|No effect receipt"):
        janitors.finish_run(run["run_id"], "changed")
    assert janitors.finish_run(run["run_id"])["outcome"] == "error"
    assert janitors.get("sam")["health"] == "needs_attention"


def test_archive_retains_agent_and_history():
    sam, hugo, cfg = setup()
    run = admit(cfg)
    assert janitors.remove("sam", cfg["revision"])
    assert janitors.get_run(run["run_id"])["outcome"] == "cancelled"
    assert agents.get_by_agent_id(sam)["archived_at"]
    assert janitors.list_janitors() == []


def pilot_payload(sam, hugo, revision):
    return dict(expected_revision=revision, expected_agent_id=sam, import_id=hashlib.sha256(b"frozen-source").hexdigest(),
                progress={"cursor": 12, "pending": {}, "reviews": {}, "seen": ["hugo:one"]},
                ownership=[{"target_session": "hugo", "label": "Car mode UX", "source_state_id": build_context_from_connection(db.conn(), "hugo")["state_id"], "at": "2026-09-06T05:00:00Z"}],
                receipts=[{"target_session": "hugo", "before": "Car tests", "after": "Car mode UX", "outcome": "changed", "reason": "Clearer task", "source_state_id": 12, "at": "2026-09-06T05:00:00Z"}])


def test_pilot_import_is_paused_exact_identity_and_preserves_receipts_once():
    sam, hugo, cfg = setup()
    agents.set_custom_status(hugo, "Car mode UX")
    payload = pilot_payload(sam, hugo, cfg["revision"])
    with pytest.raises(janitors.JanitorError, match="Pause"):
        janitors.import_pilot("sam", **payload)
    cfg = janitors.set_enabled("sam", cfg["revision"], False)
    payload["expected_revision"] = cfg["revision"]
    imported = janitors.import_pilot("sam", **payload)
    assert not imported["enabled"] and imported["revision"] == cfg["revision"] + 1
    assert janitors.owned_label(sam, "hugo") == "Car mode UX"
    assert agents.get_by_agent_id(hugo)["custom_status"] == "Car mode UX"
    assert janitors.import_pilot("sam", **payload) == imported
    exported = janitors.export_migration("sam")
    assert len(exported["receipts"]) == 1
    assert exported["progress"]["cursor"] == 12


def test_pilot_conflicting_label_rolls_back_entire_import():
    sam, hugo, cfg = setup()
    cfg = janitors.set_enabled("sam", cfg["revision"], False)
    payload = pilot_payload(sam, hugo, cfg["revision"])
    with pytest.raises(janitors.JanitorError, match="label changed"):
        janitors.import_pilot("sam", **payload)
    assert janitors.get("sam")["revision"] == cfg["revision"]
    assert not db.conn().execute("SELECT 1 FROM janitor_pilot_imports").fetchone()
    assert janitors.list_runs("sam") == []


def test_v72_migration_preserves_all_existing_data():
    worker = agent("josh", plan=True)
    schedule = scheduler.create_schedule("josh", "Existing cron", "@daily", "Keep working")
    c = db.conn()
    new_tables = ["janitor_creation_requests", "janitor_pilot_imports", "janitor_label_ownership", "janitor_effects", "janitor_runs", "janitor_progress", "janitor_attachments", "janitor_trigger_definitions", "janitor_configs"]
    for table in new_tables:
        c.execute(f"DROP TABLE {table}")
    c.execute("ALTER TABLE agents DROP COLUMN is_janitor")
    c.execute("ALTER TABLE oracle_delegations DROP COLUMN completion_trace_id")
    c.execute("PRAGMA user_version=72")
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    original = {t: [tuple(r) for r in c.execute(f'SELECT * FROM "{t}"')] for t in tables}
    original_agent_columns = [r[1] for r in c.execute("PRAGMA table_info(agents)")]
    db._migrate(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    for table, records in original.items():
        columns = ",".join(original_agent_columns) if table == "agents" else "*"
        assert [tuple(r) for r in c.execute(f'SELECT {columns} FROM "{table}"')] == records, table
    assert agents.get_by_agent_id(worker)["is_janitor"] == 0
    assert scheduler.get_schedule(schedule["schedule_id"]) == schedule
    assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def creation_payload():
    return {"name": "Sam", "backend": "codex", "cwd": "/tmp", "model": "gpt-5.3-codex-spark",
            "effort": "low", "template_id": "task-labels", "scope": {},
            "attachments": [{"trigger_id": "agent-work-completed"}]}


def create_reserved(intent):
    identity = dict(intent["identity"])
    identity["persona"] = identity.pop("name")
    return agents.create_agent(**identity, voice_id="", session=intent["session"],
                               creation_request_id=intent["request_id"])


def test_creation_reservation_retries_and_payload_conflicts():
    payload = creation_payload()
    intent = janitors.begin_creation("stable-creation", payload)
    assert janitors.begin_creation("stable-creation", dict(reversed(list(payload.items())))) == intent
    assert not intent["agent_id"] and not intent["completed"]
    assert not agents.list_agents()
    with pytest.raises(janitors.JanitorError, match="different settings"):
        janitors.begin_creation("stable-creation", {**payload, "name": "Iris"})
    assert db.conn().execute("SELECT COUNT(*) FROM janitor_creation_requests").fetchone()[0] == 1


@pytest.mark.parametrize("invalid", [{"effort": "invalid"}, {"model": False}, {"backend": "unknown"},
                                     {"attachments": [{"trigger_id": "unknown"}]}, {"name": ""}])
def test_creation_validates_before_reserving_or_creating(invalid):
    with pytest.raises(janitors.JanitorError):
        janitors.begin_creation("bad-creation", {**creation_payload(), **invalid})
    assert not db.conn().execute("SELECT 1 FROM janitor_creation_requests").fetchone()
    assert not agents.list_agents()


def test_creation_resume_after_identity_commit_keeps_one_identity_and_model():
    payload = creation_payload()
    intent = janitors.begin_creation("interrupted-creation", payload)
    agent_id = create_reserved(intent)
    # HTTP was interrupted before janitors.create / complete_creation.
    assert agents.get_by_agent_id(agent_id)["is_janitor"]
    assert agents.get_by_agent_id(agent_id)["model"] == payload["model"]
    assert not db.conn().execute("SELECT 1 FROM janitor_configs").fetchone()
    resumed = janitors.begin_creation("interrupted-creation", payload)
    assert resumed["agent_id"] == agent_id and not resumed["completed"]
    assert create_reserved(resumed) == agent_id
    configured = janitors.create(resumed["session"])
    completed = janitors.complete_creation("interrupted-creation", resumed["session"])
    assert completed == configured and not completed["enabled"]
    assert janitors.complete_creation("interrupted-creation", resumed["session"]) == completed
    assert janitors.begin_creation("interrupted-creation", payload)["response"] == completed
    assert len(agents.list_agents()) == 1
    assert db.conn().execute("SELECT COUNT(*) FROM janitor_configs").fetchone()[0] == 1


def test_creation_resume_after_config_commit_only_records_response():
    payload = creation_payload()
    intent = janitors.begin_creation("configured-creation", payload)
    create_reserved(intent)
    configured = janitors.create(intent["session"])
    resumed = janitors.begin_creation("configured-creation", payload)
    assert not resumed["completed"] and resumed["agent_id"] == configured["agent_id"]
    assert janitors.create(resumed["session"]) == configured
    assert janitors.complete_creation("configured-creation", resumed["session"])["revision"] == 1


def test_creation_cannot_adopt_foreign_occupied_reserved_session():
    payload = creation_payload()
    intent = janitors.begin_creation("foreign-creation", payload)
    foreign = agents.create_agent(persona="Sam", voice_id="", cwd="/tmp", session=intent["session"],
                                  backend="codex", model=payload["model"], effort="low")
    with pytest.raises(janitors.JanitorError, match="another creation"):
        create_reserved(intent)
    with pytest.raises(janitors.JanitorError, match="another creation"):
        janitors.begin_creation("foreign-creation", payload)
    assert not agents.get_by_agent_id(foreign)["is_janitor"]
    assert db.conn().execute("SELECT agent_id FROM janitor_creation_requests").fetchone()[0] is None


def test_creation_identity_and_provenance_roll_back_together():
    intent = janitors.begin_creation("atomic-creation", creation_payload())
    db.conn().execute("CREATE TRIGGER reject_creation_link BEFORE UPDATE OF agent_id ON janitor_creation_requests BEGIN SELECT RAISE(ABORT,'link unavailable'); END")
    with pytest.raises(Exception, match="link unavailable"):
        create_reserved(intent)
    assert not agents.session_exists(intent["session"])
    assert db.conn().execute("SELECT agent_id FROM janitor_creation_requests").fetchone()[0] is None


def test_creation_cannot_bind_changed_identity_or_unconfigured_response():
    intent = janitors.begin_creation("guarded-creation", creation_payload())
    with pytest.raises(janitors.JanitorError, match="registered creation"):
        agents.create_agent(persona="Other", voice_id="", cwd="/tmp", session=intent["session"],
                            backend="codex", creation_request_id=intent["request_id"])
    with pytest.raises(janitors.JanitorError, match="not been created"):
        janitors.complete_creation(intent["request_id"], intent["session"])
    create_reserved(intent)
    with pytest.raises(janitors.JanitorError, match="not found"):
        janitors.complete_creation(intent["request_id"], intent["session"])


def test_cancellation_pending_distinguishes_effect_pause_from_stopping_runtime():
    sam, hugo, cfg = setup()
    agents.record_state(sam, "thinking", {"origin": "janitor", "trace_id": "maintenance"})
    assert not janitors.get("sam")["cancellation_pending"]
    paused = janitors.set_enabled("sam", cfg["revision"], False)
    assert not paused["enabled"] and paused["cancellation_pending"]
    agents.record_state(sam, "idle", {"origin": "janitor", "trace_id": "maintenance"})
    assert not janitors.get("sam")["cancellation_pending"]
