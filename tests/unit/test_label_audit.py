"""The Label checker Janitor: asks Jev, reports in Updates, changes nothing.

No network: `judgments._post` is replaced by a fake that answers every
question it is sent with the verdict the test chose.
"""
from __future__ import annotations

import pytest

from lib import (agents, artifacts, attention_index, background_jobs, db, heartbeat,
                 janitor_builtins, janitors, judgments, label_audit, settings_store)
from lib import janitor_autonomy as service


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(label_audit, "FRESH_GRACE_MS", 0)
    judgments.reset_breaker()
    monkeypatch.setattr(service.backends, "active_handles", lambda *a: [])
    owners = janitor_builtins.ensure_builtins(cwd="/tmp")
    service.setup()
    yield owners["label-auditor"]
    judgments.reset_breaker()


def _enable_site(monkeypatch):
    monkeypatch.setattr(judgments, "api_key", lambda: "test-key")
    settings_store.set_bool(judgments.KEY_ENABLED, True)
    settings_store.set_bool("judgments.labels", True)


def _jev(monkeypatch, verdict: str, probabilities: dict[str, float]) -> list[dict]:
    """Fake transport: every question gets `verdict`. Returns the requests."""
    seen: list[dict] = []

    def fake_post(body, key, seconds):
        seen.append(body)
        return {"answers": {qid: {"choice": verdict, "probabilities": probabilities}
                            for qid in body["questions"]}, "usage": {}}

    monkeypatch.setattr(judgments, "_post", fake_post)
    return seen


def _lena(state: str = "background", status: str = "Building") -> str:
    aid = agents.create_agent(persona="Lena", voice_id="", cwd="/tmp", session="lena")
    agents.record_state(aid, state, {})
    if status:
        agents.set_custom_status(aid, status)
    return aid


def _worker():
    return service.AutonomyJanitors(lambda *_: pytest.fail("dispatched"), lambda _: None)


def _reports() -> list[dict]:
    return [row for row in (dict(r) for r in db.conn().execute(
        "SELECT * FROM artifacts WHERE reference_id LIKE ?", (label_audit.PREFIX + "%",)))]


WAITING = {"accurate": 0.05, "waiting_for_user": 0.9, "finished": 0.05}


def test_the_janitor_is_installed_with_an_hourly_interval_and_autocorrect_off(env):
    assert env["enabled"]
    assert env["options"] == {"interval_seconds": 3600, "autocorrect": False}
    assert janitors.template("label-auditor")["allowed_effects"] == ["label_report"]


def test_a_wrong_label_is_reported_once_and_nothing_changes(monkeypatch, env):
    _enable_site(monkeypatch)
    seen = _jev(monkeypatch, "waiting_for_user", WAITING)
    aid = _lena()

    _worker().label_audit_once()

    assert len(seen) == 1
    packet = seen[0]["state"]["agents"]["a1"]
    assert packet["label"] == "Building" and packet["state"] == "background"
    assert aid not in str(seen[0])  # Jev never sees agent ids
    [report] = _reports()
    assert report["status"] == "completed" and report["agent_id"] == env["agent_id"]
    assert report["title"] == "1 working label looks wrong"
    assert "Lena says 'Building', but it looks like it is waiting for your answer" in report["summary"]
    public = artifacts.get(report["artifact_id"])
    assert public["payload"]["attention_kind"] == label_audit.KIND
    assert public["payload"]["items"][0]["verdict"] == "waiting_for_user"
    # It shows in Updates for review.
    [listed] = [item for item in attention_index.page(limit=50)["artifacts"]
                if item["artifact_id"] == report["artifact_id"]]
    assert listed["attention_bucket"] == "review"
    # Report only: the label and state are untouched.
    row = agents.get_by_agent_id(aid)
    assert row["custom_status"] == "Building"
    assert agents.latest_state(aid)["kind"] == "background"
    [run] = janitors.list_runs(env["session"])
    assert run["outcome"] == "completed"


def test_the_same_mismatch_is_not_reported_again_next_hour(monkeypatch, env):
    _enable_site(monkeypatch)
    seen = _jev(monkeypatch, "waiting_for_user", WAITING)
    _lena()
    worker = _worker()
    worker.label_audit_once()
    worker.label_audit_once()  # inside the interval: no call
    assert len(seen) == 1
    settings_store.set_int("label-auditor.last-check", 0)
    worker.label_audit_once()
    assert len(seen) == 2
    assert len(_reports()) == 1


@pytest.mark.parametrize("probabilities", [
    {"accurate": 0.9, "finished": 0.1},        # right
    {"accurate": 0.45, "finished": 0.55},      # unsure: not reported
])
def test_accurate_or_unsure_labels_are_not_reported(monkeypatch, probabilities):
    _enable_site(monkeypatch)
    verdict = max(probabilities, key=probabilities.get)
    _jev(monkeypatch, verdict, probabilities)
    _lena()
    _worker().label_audit_once()
    assert _reports() == []


def test_nothing_shown_as_working_means_no_call(monkeypatch, env):
    _enable_site(monkeypatch)
    seen = _jev(monkeypatch, "finished", {"finished": 1.0})
    _lena(state="done", status="")
    _worker().label_audit_once()
    assert seen == []
    assert janitors.list_runs(env["session"]) == []


def test_an_agent_in_a_live_turn_is_not_judged(monkeypatch):
    _enable_site(monkeypatch)
    seen = _jev(monkeypatch, "finished", {"finished": 1.0})
    _lena(state="thinking")
    _worker().label_audit_once()
    assert seen == []


@pytest.mark.parametrize("gate", ["site_off", "quiet_hours", "paused"])
def test_admission_gates_stop_the_call(monkeypatch, env, gate):
    _enable_site(monkeypatch)
    seen = _jev(monkeypatch, "finished", {"finished": 1.0})
    _lena()
    if gate == "site_off":
        settings_store.set_bool("judgments.labels", False)
    if gate == "quiet_hours":
        monkeypatch.setattr(heartbeat, "outside_active_hours", lambda _now: True)
    if gate == "paused":
        janitors.set_enabled(env["session"], env["revision"], False)
    _worker().label_audit_once()
    assert seen == []
    assert _reports() == []


def test_no_answer_fails_the_run_and_reports_nothing(monkeypatch, env):
    _enable_site(monkeypatch)

    def broken(*_args):
        raise TimeoutError("slow")

    monkeypatch.setattr(judgments, "_post", broken)
    _lena()
    _worker().label_audit_once()
    assert _reports() == []
    [run] = janitors.list_runs(env["session"])
    assert run["status"] == "failed" and "Jev did not answer" in run["error"]


def test_evidence_is_small_and_carries_job_progress():
    aid = _lena(state="done", status="")
    background_jobs.upsert(session="lena", job_id="ci", kind="ci", title="Wait for CI")
    background_jobs.set_progress("ci", session="lena", generation=1, text="3/9 checks green")
    [entry] = label_audit.candidates(db.now_ms())
    packet = entry["packet"]
    assert entry["agent_id"] == aid
    assert packet["label"] == "Wait for CI: 3/9 checks green"
    assert packet["jobs"][0]["title"] == "Wait for CI"
    assert packet["jobs"][0]["heartbeat_age_min"] == 0
    assert set(packet) == {"label", "state", "state_detail", "state_age_min",
                           "turn_ended_min_ago", "jobs", "helpers", "recent_messages"}


def test_a_label_that_just_changed_is_not_judged(monkeypatch):
    monkeypatch.setattr(label_audit, "FRESH_GRACE_MS", 15 * 60 * 1000)
    _lena(state="background")
    now = db.now_ms()
    assert label_audit.candidates(now) == []
    assert len(label_audit.candidates(now + 16 * 60 * 1000)) == 1
