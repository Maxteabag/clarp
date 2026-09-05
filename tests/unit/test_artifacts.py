from __future__ import annotations

from pathlib import Path

import pytest

from lib import agents, artifacts, db, media_store, task_plans


def _agent(tmp_path, session="mike"):
    return agents.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session=session)


def _media(tmp_path, mime: str, name: str) -> dict:
    blobs = {"audio/mpeg": b"ID3\x04\x00\x00audio",
             "video/mp4": b"\x00\x00\x00\x18ftypisomvideo",
             "application/pdf": b"%PDF-1.7\nfile"}
    return media_store.publish(session="mike", blob=blobs[mime],
                               source_name=name, content_type=mime,
                               media_dir=tmp_path / "media")


def test_artifact_round_trip_and_validation(tmp_path):
    _agent(tmp_path)
    created = artifacts.create(
        session="mike", type="research", title="Docker analysis",
        summary="Recommendation", payload={"content": "Recommendation", "sources": []})
    assert created["type"] == "research"
    assert created["payload"] == {"content": "Recommendation", "sources": []}
    hidden = artifacts.create(
        session="mike", type="document", title="Visible<environment_context>secret</environment_context>",
        summary="Summary<oai-mem-citation>internal</oai-mem-citation>",
        payload={"content": "Body<environment_context>private</environment_context>",
                 "nested": {"notes": "Safe<oai-mem-citation>hidden</oai-mem-citation>"}})
    assert hidden["title"] == "Visible" and hidden["summary"] == "Summary"
    assert hidden["content"] == "Body"
    assert hidden["payload"]["nested"]["notes"] == "Safe"
    assert created["artifact_id"] in {
        row["artifact_id"] for row in artifacts.list_artifacts(session="mike")
    }
    updated = artifacts.update(created["artifact_id"], {"status": "completed"})
    assert updated["status"] == "completed"
    with pytest.raises(ValueError, match="unsupported"):
        artifacts.create(session="mike", type="shell_command", title="Unsafe")
    done = artifacts.create(session="mike", type="document", title="Done",
                            status="completed", payload={"content": "Done"})
    assert done["completed_at"] is not None


def test_artifact_listing_can_page_by_creation_time(tmp_path, monkeypatch):
    _agent(tmp_path)
    clock = [1000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    old = artifacts.create(session="mike", type="document", title="Old",
                           payload={"content": "Old"})
    clock[0] = 2000
    new = artifacts.create(session="mike", type="document", title="New",
                           payload={"content": "New"})
    clock[0] = 3000
    artifacts.update(old["artifact_id"], {"summary": "updated later"})
    assert [row["artifact_id"] for row in artifacts.list_artifacts(
        session="mike", order="updated")] == [old["artifact_id"], new["artifact_id"]]
    assert [row["artifact_id"] for row in artifacts.list_artifacts(
        session="mike", order="created")] == [new["artifact_id"], old["artifact_id"]]


def test_decision_resolution_is_revision_safe_and_idempotent(tmp_path):
    _agent(tmp_path)
    created = artifacts.create_decision(
        session="mike", title="Send email", question="Send it?",
        yes_label="Send email", no_label="Don't send")
    decision = created["decision"]
    with pytest.raises(ValueError, match="dedicated lifecycle"):
        artifacts.update(created["artifact_id"], {"status": "cancelled"})
    assert len(artifacts.attention()) == 1
    resolved, changed = artifacts.resolve(
        decision["decision_id"], choice="accepted", expected_revision=1)
    assert changed is True
    assert resolved["decision"]["status"] == "accepted"
    assert artifacts.attention() == []
    delivery = artifacts.pending_deliveries()[0]
    assert delivery["choice"] == "accepted"
    artifacts.mark_delivered(decision["decision_id"])
    assert artifacts.pending_deliveries() == []
    same, changed = artifacts.resolve(
        decision["decision_id"], choice="accepted", expected_revision=1)
    assert changed is False and same["decision"]["status"] == "accepted"
    with pytest.raises(ValueError, match="different choice"):
        artifacts.resolve(decision["decision_id"], choice="rejected", expected_revision=1)


def test_plan_creation_gets_single_artifact_wrapper(tmp_path):
    _agent(tmp_path)
    plan = task_plans.create(session="mike", title="Ship", items=[{"id": "one", "title": "Build"}])
    artifact = artifacts.list_artifacts(session="mike", type="plan")[0]
    assert artifact["reference_id"] == plan["plan_id"]
    assert artifact["payload"]["plan_id"] == plan["plan_id"]
    assert artifact["plan"]["items"][0]["title"] == "Build"


def test_replaced_plan_artifact_is_cancelled(tmp_path):
    _agent(tmp_path)
    first = task_plans.create(session="mike", title="First", items=[{"title": "One"}])
    task_plans.create(session="mike", title="Second", items=[{"title": "Two"}])
    old = next(row for row in artifacts.list_artifacts(session="mike", type="plan")
               if row["reference_id"] == first["plan_id"])
    assert old["status"] == "cancelled"


def test_plan_sync_repairs_missing_wrapper(tmp_path):
    _agent(tmp_path)
    plan = task_plans.create(session="mike", title="Repair", items=[{"id": "one", "title": "One"}])
    db.conn().execute("DELETE FROM artifacts WHERE type='plan' AND reference_id=?", (plan["plan_id"],))
    task_plans.update_item(task_plans.item_key(plan["plan_id"], "one"), "in_progress")
    assert artifacts.list_artifacts(session="mike", type="plan")[0]["reference_id"] == plan["plan_id"]


def test_expired_decision_becomes_non_actionable(tmp_path, monkeypatch):
    _agent(tmp_path)
    created = artifacts.create_decision(
        session="mike", title="Old", question="Still do it?", expires_at=1)
    assert artifacts.attention() == []
    expired = artifacts.get(created["artifact_id"])
    assert expired["status"] == "expired"
    assert expired["decision"]["status"] == "expired"
    with pytest.raises(ValueError, match="expired"):
        artifacts.resolve(
            created["decision"]["decision_id"], choice="accepted", expected_revision=1)


def test_caller_artifact_id_cannot_escape_client_cache(tmp_path):
    _agent(tmp_path)
    with pytest.raises(ValueError, match="invalid artifact id"):
        artifacts.create(session="mike", type="file", title="Unsafe",
                         artifact_id="../../Library/unsafe")


def test_payload_fields_used_by_native_have_stable_types(tmp_path):
    _agent(tmp_path)
    with pytest.raises(ValueError, match="size_bytes must be an integer"):
        artifacts.create(session="mike", type="file", title="Bad",
                         payload={"size_bytes": "large"})
    with pytest.raises(ValueError, match="step counts"):
        artifacts.create(session="mike",type="workflow_run",title="Bad",payload={"provider":"github","run_id":"1","run_url":"https://github.com/a/b/actions/runs/1","workflow_name":"CI","total_steps":1,"completed_steps":2})
    with pytest.raises(ValueError, match="row_count must be an integer"):
        artifacts.create(session="mike", type="data", title="Bad",
                         payload={"row_count": 1.5})
    with pytest.raises(ValueError, match="finite JSON"):
        artifacts.create(session="mike", type="data", title="Bad",
                         payload={"metric": float("nan")})


def test_every_interactive_type_requires_and_exposes_a_functional_contract(tmp_path):
    _agent(tmp_path)
    audio = _media(tmp_path, "audio/mpeg", "sample.mp3")
    video = _media(tmp_path, "video/mp4", "sample.mp4")
    file = _media(tmp_path, "application/pdf", "sample.pdf")
    member = artifacts.create(session="mike", type="document", title="Member",
                              payload={"content": "Member"})
    payloads = {
        "document": {"content": "# Document"},
        "research": {"content": "Finding", "sources": [{"title": "Source", "url": "https://example.com"}]},
        "code_change": {"repository": "clarp", "files_changed": 2, "additions": 4, "deletions": 1},
        "data": {"columns": ["Name", "Value"], "rows": [["A", 2]], "chart": {"kind": "bar", "category_column": "Name", "value_column": "Value"}},
        "audio": {"url": audio["url"], "mime_type": "audio/mpeg", "file_name": "sample.mp3"},
        "video": {"url": video["url"], "mime_type": "video/mp4", "file_name": "sample.mp4"},
        "file": {"url": file["url"], "mime_type": "application/pdf", "file_name": "sample.pdf"},
        "release": {"version": "1.2.3", "build": "42"},
        "directory":{"root":"workspace","relative_path":"ios-native"},
        "workflow_run":{"provider":"github","run_id":"1","run_url":"https://github.com/a/b/actions/runs/1","workflow_name":"CI","total_steps":2,"completed_steps":1},
    }
    for type, payload in payloads.items():
        created = artifacts.create(session="mike", type=type, title=type, payload=payload)
        for key, value in payload.items():
            assert created["payload"][key] == value
            assert created[key] == value

    for type in payloads:
        with pytest.raises(ValueError, match="requires payload"):
            artifacts.create(session="mike", type=type, title=f"invalid-{type}")


def test_typed_contract_rejects_broken_media_tables_and_removed_types(tmp_path):
    _agent(tmp_path)
    with pytest.raises(ValueError, match="mime_type"):
        artifacts.create(session="mike", type="audio", title="Wrong",
                         payload={"url": "/x", "mime_type": "video/mp4", "file_name": "x.mp4"})
    with pytest.raises(ValueError, match="column count"):
        artifacts.create(session="mike", type="data", title="Jagged",
                         payload={"columns": ["A", "B"], "rows": [[1]]})
    for removed in ("link","message_draft","collection","live_task","deployment","workspace","image","image_gallery","event"):
        with pytest.raises(ValueError,match="unsupported"): artifacts.create(session="mike",type=removed,title="Removed")


def test_retired_visual_rows_stay_readable_but_are_not_listed(tmp_path):
    _agent(tmp_path)
    agent = agents.get_by_session("mike")
    now = db.now_ms()
    for index, artifact_type in enumerate(("image", "image_gallery", "live_task", "event")):
        artifact_id = f"legacy-{index}"
        db.conn().execute(
            """INSERT INTO artifacts(artifact_id,agent_id,session,type,title,summary,status,
                       payload_json,reference_id,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (artifact_id, agent["agent_id"], "mike", artifact_type, artifact_type,
             "", "ready", "{}", "", now, now),
        )
        assert artifacts.get(artifact_id)["type"] == artifact_type
    assert artifacts.list_artifacts(session="mike") == []


def test_updates_preserve_contracts_and_merge_payload_patches(tmp_path):
    _agent(tmp_path)
    audio_asset = _media(tmp_path, "audio/mpeg", "a.mp3")
    audio = artifacts.create(
        session="mike", type="audio", title="Audio",
        payload={"url": audio_asset["url"], "mime_type": "audio/mpeg", "file_name": "a.mp3"})
    with pytest.raises(ValueError, match="requires payload"):
        artifacts.update(audio["artifact_id"], {"payload": {"content": "lost file"}})

    run=artifacts.create(session="mike",type="workflow_run",title="CI",payload={"provider":"github","run_id":"1","run_url":"https://github.com/a/b/actions/runs/1","workflow_name":"CI","current_step":"Build"})
    progressed=artifacts.update(run["artifact_id"],{"status":"active","payload_patch":{"current_step":"Test","completed_steps":1,"total_steps":2}}); assert progressed["current_step"]=="Test"


def test_external_artifact_urls_are_https_and_never_embed_credentials(tmp_path):
    _agent(tmp_path)
    for unsafe in ("http://example.com", "javascript:alert(1)",
                   "https://user:pass@example.com", "//example.com/file"):
        with pytest.raises(ValueError, match="HTTPS URL"):
            artifacts.create(session="mike",type="workflow_run",title="Unsafe",payload={"provider":"github","run_id":"1","run_url":unsafe,"workflow_name":"CI"})
    with pytest.raises(ValueError, match="same-origin"):
        artifacts.create(session="mike", type="video", title="Tracking video",
                         payload={"url": "https://tracker.example/video.mp4",
                                  "mime_type": "video/mp4", "file_name": "video.mp4"})
    with pytest.raises(media_store.MediaError, match="unsupported"):
        media_store.publish(session="mike", blob=b"not a pdf", source_name="fake.pdf",
                            content_type="application/octet-stream",
                            media_dir=tmp_path / "media")


def test_native_renderer_fields_reject_undecodable_shapes(tmp_path):
    _agent(tmp_path)
    with pytest.raises(ValueError, match="cells"):
        artifacts.create(session="mike", type="data", title="Nested",
                         payload={"columns": ["Value"], "rows": [[{"nested": True}]]})
    with pytest.raises(ValueError, match="chart"):
        artifacts.create(session="mike", type="data", title="Chart",
                         payload={"columns": ["Value"], "rows": [[1]],
                                  "chart": {"kind": "arbitrary-script"}})
    with pytest.raises(ValueError, match="title and url"):
        artifacts.create(session="mike", type="research", title="Sources",
                         payload={"content": "Finding", "sources": [{"title": 1, "url": []}]})
    with pytest.raises(ValueError, match="columns must exist"):
        artifacts.create(session="mike", type="data", title="Missing column",
                         payload={"columns": ["Name", "Value"], "rows": [["A", 1]],
                                  "chart": {"kind": "bar", "category_column": "Missing",
                                            "value_column": "Value"}})
    with pytest.raises(ValueError, match="contain numbers"):
        artifacts.create(session="mike", type="data", title="Text chart",
                         payload={"columns": ["Name", "Value"], "rows": [["A", "many"]],
                                  "chart": {"kind": "bar", "category_column": "Name",
                                            "value_column": "Value"}})


def test_legacy_malformed_renderer_fields_are_not_promoted(tmp_path):
    _agent(tmp_path)
    con = db.conn()
    now = db.now_ms()
    con.execute(
        """INSERT INTO artifacts(artifact_id,agent_id,session,type,title,summary,status,
               reference_id,payload_json,created_at,updated_at)
           SELECT 'legacy',agent_id,session,'research','Legacy','','ready','',?, ?, ?
             FROM agents WHERE session='mike'""",
        ('{"content":"Still readable","sources":4,"all_day":"yes","rows":{"bad":true}}', now, now))
    legacy = artifacts.get("legacy")
    assert legacy["content"] == "Still readable"
    assert "sources" not in legacy and "all_day" not in legacy and "rows" not in legacy
    updated = artifacts.update("legacy", {"status": "completed"})
    assert updated["status"] == "completed"


def test_data_rejects_numbers_outside_native_double_range(tmp_path):
    _agent(tmp_path)
    with pytest.raises(ValueError, match="cells"):
        artifacts.create(session="mike", type="data", title="Huge",
                         payload={"columns": ["Value"], "rows": [[10 ** 400]]})
    empty = artifacts.create(session="mike", type="data", title="No results",
                             payload={"columns": ["Name", "Value"], "rows": []})
    assert empty["rows"] == []


def test_legacy_invalid_progress_is_not_promoted(tmp_path):
    _agent(tmp_path)
    now = db.now_ms()
    db.conn().execute(
        """INSERT INTO artifacts(artifact_id,agent_id,session,type,title,summary,status,
               reference_id,payload_json,created_at,updated_at)
           SELECT 'legacy-progress',agent_id,session,'live_task','Legacy','','active','',?, ?, ?
             FROM agents WHERE session='mike'""",
        ('{"progress":2}', now, now))
    assert "progress" not in artifacts.get("legacy-progress")


def test_artifact_listing_is_pageable(tmp_path):
    _agent(tmp_path)
    for index in range(3):
        artifacts.create(session="mike", type="document", title=f"Doc {index}",
                         payload={"content": f"Doc {index}"})
    first = artifacts.list_artifacts(session="mike", limit=2, offset=0)
    second = artifacts.list_artifacts(session="mike", limit=2, offset=2)
    assert len(first) == 2 and len(second) == 1
    assert {row["artifact_id"] for row in first}.isdisjoint(
        {row["artifact_id"] for row in second})


def test_invalid_decision_expiry_rolls_back_artifact(tmp_path):
    _agent(tmp_path)
    with pytest.raises(ValueError, match="expires_at"):
        artifacts.create_decision(session="mike", title="Bad", question="Do it?",
                                  expires_at={"bad": True})
    assert artifacts.list_artifacts(session="mike") == []


def test_agent_deletion_cancels_pending_decisions(tmp_path):
    agent_id = _agent(tmp_path)
    created = artifacts.create_decision(session="mike", title="Approval", question="Proceed?")
    agents.soft_delete(agent_id)
    assert artifacts.attention() == []
    row = artifacts.get(created["artifact_id"])
    assert row["decision"]["status"] == "cancelled"


def _question(**kwargs):
    return artifacts.create_decision(
        session="mike", title="Layout choice", question="Which layout should I use?",
        response_type="single_choice", **{
            "options": [{"id": "current", "label": "Keep the layout", "description": "Least disruption"},
                        {"id": "compact", "label": "Use a compact layout"}],
            **kwargs})


def test_questions_have_typed_answers_and_safe_legacy_attention(tmp_path):
    _agent(tmp_path)
    approval = artifacts.create_decision(session="mike", title="Send", question="Send the email?")
    question = _question(recommended_option_id="current", blocks_progress=True,
                         priority_reason="Cannot implement the layout until this is decided",
                         response_effort="quick", deadline_at=9000000000000)
    assert question["type"] == "question"
    decision = question["decision"]
    assert decision["allow_custom_text"] is True
    assert decision["blocks_progress"] is True
    assert decision["priority"] == 200
    assert decision["answer"] is None and decision["archived_at"] is None
    assert decision["delivery_pending"] is False
    assert decision["recommended_option_id"] == "current"
    assert "options_json" not in decision and "answer_json" not in decision
    assert [item["artifact_id"] for item in artifacts.attention()] == [approval["artifact_id"]]
    pending = artifacts.attention(include_questions=True)
    assert [item["artifact_id"] for item in pending] == [question["artifact_id"], approval["artifact_id"]]
    assert pending[0]["options"] == decision["options"]
    assert pending[0]["updated_at"] == question["updated_at"]
    for binary in ("accepted", "rejected"):
        with pytest.raises(ValueError, match="typed answer"):
            artifacts.resolve(decision["decision_id"], choice=binary, expected_revision=1)
    resolved, changed = artifacts.resolve(decision["decision_id"], answer={"option_id": "compact"}, expected_revision=1)
    assert changed and resolved["status"] == "completed"
    assert resolved["decision"]["status"] == "answered"
    assert resolved["decision"]["answer"] == {"option_id": "compact", "label": "Use a compact layout"}
    assert resolved["decision"]["delivery_pending"] is True
    delivery = artifacts.pending_deliveries()[0]
    assert delivery["response_type"] == "single_choice" and delivery["choice"] == "answered"
    assert delivery["answer"] == resolved["decision"]["answer"]
    prompt = artifacts.format_delivery_prompt(delivery)
    assert "Use a compact layout" in prompt and "does not grant approval" in prompt
    # Editing a caller-owned object cannot alter the recorded answer or delivery.
    resolved["decision"]["answer"]["label"] = "Different"
    assert artifacts.pending_deliveries()[0]["answer"]["label"] == "Use a compact layout"
    _, changed = artifacts.resolve(decision["decision_id"], answer={"option_id": " compact "}, expected_revision=1)
    assert changed is False and len(artifacts.pending_deliveries()) == 1
    with pytest.raises(ValueError, match="different choice or answer"):
        artifacts.resolve(decision["decision_id"], answer={"option_id": "current"}, expected_revision=1)
    artifacts.mark_delivered(decision["decision_id"])
    assert artifacts.pending_deliveries() == []
    assert artifacts.get(question["artifact_id"])["decision"]["delivery_pending"] is False


def test_custom_answer_preserves_wording_and_exact_retry_semantics(tmp_path):
    _agent(tmp_path)
    question = _question()
    decision_id = question["decision"]["decision_id"]
    answer = {"text": "  Keep Navigation.\n  Change only <title>Case</title>.  "}
    resolved, _ = artifacts.resolve(decision_id, answer=answer, expected_revision=1)
    assert resolved["decision"]["answer"] == {"text": answer["text"].strip()}
    snapshot = artifacts.pending_deliveries()[0]
    assert snapshot["answer"] == resolved["decision"]["answer"]
    assert "Keep Navigation." in artifacts.format_delivery_prompt(snapshot)
    _, changed = artifacts.resolve(decision_id, answer={"text": answer["text"].strip()}, expected_revision=1)
    assert not changed
    with pytest.raises(ValueError, match="different choice or answer"):
        artifacts.resolve(decision_id, answer={"text": answer["text"].lower()}, expected_revision=2)


@pytest.mark.parametrize("bad", [
    {"options": None}, {"options": []}, {"options": [{"id": "a", "label": "A"}]},
    {"options": [{"id": str(i), "label": "Option"} for i in range(4)]},
    {"options": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]},
    {"options": [{"id": "", "label": "A"}, {"id": "b", "label": "B"}]},
    {"options": [{"id": "a", "label": " "}, {"id": "b", "label": "B"}]},
    {"options": [{"id": 1, "label": "A"}, {"id": "b", "label": "B"}]},
    {"options": [{"id": "a", "label": "A", "description": 2}, {"id": "b", "label": "B"}]},
    {"options": [{"id": "a", "label": "A" * 201}, {"id": "b", "label": "B"}]},
    {"recommended_option_id": "missing"}, {"recommended_option_id": False},
    {"allow_custom_text": 1}, {"blocks_progress": "true"}, {"blocks_progress": True},
    {"urgency": "time_sensitive"}, {"urgency": "critical"}, {"response_effort": "instant"},
    {"priority_reason": "a" * 2001}, {"priority_reason": []},
    {"deadline_at": float("nan")}, {"deadline_at": float("inf")}, {"deadline_at": True},
    {"deadline_at": -1}, {"deadline_at": 10 ** 30}, {"expires_at": -1},
])
def test_invalid_question_contracts_leave_no_partial_artifact(tmp_path, bad):
    _agent(tmp_path)
    with pytest.raises(ValueError):
        _question(**bad)
    assert artifacts.list_artifacts(session="mike") == []
    assert artifacts.pending_deliveries() == []


@pytest.mark.parametrize("answer", [
    None, {}, {"option_id": "absent"}, {"option_id": 1}, {"text": " "}, {"text": 1},
    {"text": "a" * 4001}, {"option_id": "compact", "text": "Both"},
    {"option_id": "compact", "label": "User supplied label"},
])
def test_invalid_answers_do_not_resolve_or_queue(tmp_path, answer):
    _agent(tmp_path)
    question = _question()
    with pytest.raises(ValueError):
        artifacts.resolve(question["decision"]["decision_id"], answer=answer, expected_revision=1)
    assert artifacts.get(question["artifact_id"])["decision"]["status"] == "pending"
    assert artifacts.pending_deliveries() == []


def test_question_and_approval_modes_cannot_be_confused(tmp_path):
    _agent(tmp_path)
    approval = artifacts.create_decision(session="mike", title="Send", question="Send?")
    with pytest.raises(ValueError, match="explicit accepted"):
        artifacts.resolve(approval["decision"]["decision_id"], answer={"text": "yes"}, expected_revision=1)
    with pytest.raises(ValueError, match="do not support"):
        artifacts.create_decision(session="mike", title="Send", question="Send?", allow_custom_text=True)
    question = _question(allow_custom_text=False)
    with pytest.raises(ValueError, match="not enabled"):
        artifacts.resolve(question["decision"]["decision_id"], answer={"text": "custom"}, expected_revision=1)
    for artifact_type in ("question", "decision", "plan"):
        with pytest.raises(ValueError, match="dedicated lifecycle"):
            artifacts.create(session="mike", type=artifact_type, title="Fake")
    with pytest.raises(ValueError, match="dedicated lifecycle"):
        artifacts.update(question["artifact_id"], {"status": "completed", "payload": {"options": []}})


def test_question_revision_expiry_and_agent_deletion(tmp_path, monkeypatch):
    agent_id = _agent(tmp_path)
    clock = [1000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    question = _question(expires_at=2000)
    decision_id = question["decision"]["decision_id"]
    for revision in (0, True, "1", 1.2, 10 ** 30):
        with pytest.raises(ValueError, match="expected_revision"):
            artifacts.resolve(decision_id, answer={"option_id": "compact"}, expected_revision=revision)
    with pytest.raises(ValueError, match="revision changed"):
        artifacts.resolve(decision_id, answer={"option_id": "compact"}, expected_revision=2)
    clock[0] = 2000
    assert artifacts.attention(include_questions=True) == []
    assert artifacts.get(question["artifact_id"])["decision"]["status"] == "expired"
    delivery = artifacts.pending_deliveries()[0]
    assert delivery["choice"] == "expired" and delivery["answer"] is None
    assert "without an answer or approval" in artifacts.format_delivery_prompt(delivery)
    with pytest.raises(ValueError, match="expired"):
        artifacts.resolve(decision_id, answer={"option_id": "compact"}, expected_revision=1)
    second = _question()
    agents.soft_delete(agent_id)
    assert artifacts.get(second["artifact_id"])["decision"]["status"] == "cancelled"
    assert artifacts.attention(include_questions=True) == []
    assert artifacts.pending_deliveries() == []


def test_decision_expiring_while_waiting_for_lock_cannot_resolve(tmp_path, monkeypatch):
    _agent(tmp_path)
    monkeypatch.setattr(db, "now_ms", lambda: 1000)
    question = _question(expires_at=1001)
    # Expiry sweep runs just before expiration; resolution reads after it.
    ticks = iter([1000, 1001])
    monkeypatch.setattr(db, "now_ms", lambda: next(ticks))
    with pytest.raises(ValueError, match="expired"):
        artifacts.resolve(question["decision"]["decision_id"], answer={"option_id": "current"}, expected_revision=1)
    assert artifacts.pending_deliveries() == []


def test_attention_order_separates_urgency_and_effort_and_is_stable(tmp_path, monkeypatch):
    _agent(tmp_path)
    monkeypatch.setattr(db, "now_ms", lambda: 1000)
    quick = _question(response_effort="quick")
    slow = _question(blocks_progress=True, priority_reason="Work is blocked", response_effort="review")
    deadline = _question(blocks_progress=True, priority_reason="Review before the meeting", deadline_at=2000)
    urgent = _question(urgency="time_sensitive", priority_reason="Near deadline", deadline_at=1500)
    blocked_urgent = _question(blocks_progress=True, urgency="time_sensitive", priority_reason="Blocked and due soon")
    rows = artifacts.attention(include_questions=True)
    assert [item["artifact_id"] for item in rows] == [
        blocked_urgent["artifact_id"], deadline["artifact_id"], slow["artifact_id"],
        urgent["artifact_id"], quick["artifact_id"]]
    assert [item["priority"] for item in rows] == [210, 200, 200, 110, 100]
    assert rows == artifacts.attention(include_questions=True)


def test_archive_restore_are_durable_idempotent_and_timestamp_guarded(tmp_path, monkeypatch):
    _agent(tmp_path)
    monkeypatch.setattr(db, "now_ms", lambda: 1000)
    question = _question()
    identifier, version = question["artifact_id"], question["updated_at"]
    with pytest.raises(ValueError, match="artifact changed"):
        artifacts.archive(identifier, archived=True, expected_updated_at=version - 1)
    archived, changed = artifacts.archive(identifier, archived=True, expected_updated_at=version)
    assert changed and archived["archived_at"] == archived["updated_at"] == version + 1
    assert archived["decision"]["status"] == "pending" and archived["decision"]["revision"] == 1
    assert archived["decision"]["archived_at"] == archived["archived_at"]
    assert artifacts.attention(include_questions=True) == []
    assert artifacts.attention(include_questions=True, include_archived=True)[0]["artifact_id"] == identifier
    assert artifacts.list_artifacts(session="mike")[0]["artifact_id"] == identifier
    assert artifacts.pending_deliveries() == []
    _, changed = artifacts.archive(identifier, archived=True, expected_updated_at=version)
    assert changed is False
    with pytest.raises(ValueError, match="artifact changed"):
        artifacts.archive(identifier, archived=False, expected_updated_at=version)
    restored, changed = artifacts.archive(identifier, archived=False, expected_updated_at=archived["updated_at"])
    assert changed and restored["archived_at"] is None and restored["updated_at"] > archived["updated_at"]
    assert len(artifacts.attention(include_questions=True)) == 1


@pytest.mark.parametrize("question_mode", [False, True])
def test_dismissal_cancels_without_answer_or_permission_and_notifies_once(tmp_path, question_mode):
    _agent(tmp_path)
    item = _question() if question_mode else artifacts.create_decision(session="mike", title="Send", question="Send?")
    decision_id = item["decision"]["decision_id"]
    with pytest.raises(ValueError, match="revision changed"):
        artifacts.dismiss(decision_id, expected_revision=2)
    dismissed, changed = artifacts.dismiss(decision_id, expected_revision=1)
    assert changed and dismissed["status"] == "cancelled"
    assert dismissed["decision"]["status"] == "cancelled"
    assert dismissed["decision"]["answer"] is None
    assert dismissed["decision"]["resolved_by"] == "user"
    delivery = artifacts.pending_deliveries()[0]
    assert delivery["choice"] == "dismissed" and delivery["answer"] is None
    prompt = artifacts.format_delivery_prompt(delivery)
    assert "not an answer or approval" in prompt and "repeat the unchanged request" in prompt
    _, changed = artifacts.dismiss(decision_id, expected_revision=1)
    assert not changed and len(artifacts.pending_deliveries()) == 1
    with pytest.raises(ValueError, match="already resolved"):
        artifacts.resolve(decision_id, expected_revision=1,
                          **({"answer": {"option_id": "current"}} if question_mode else {"choice": "accepted"}))
    assert artifacts.attention(include_questions=True) == []


def test_discard_removes_only_artifact_record_and_keeps_files_and_work(tmp_path):
    agent_id = _agent(tmp_path)
    media = _media(tmp_path, "application/pdf", "result.pdf")
    source = Path(media_store.get(media["asset_id"])["storage_path"])
    original_bytes = source.read_bytes()
    item = artifacts.create(session="mike", type="file", title="Result",
                            payload={"url": media["url"], "mime_type": media["mime_type"], "file_name": "result.pdf"})
    with pytest.raises(ValueError, match="artifact changed"):
        artifacts.discard(item["artifact_id"], expected_updated_at=item["updated_at"] - 1)
    deleted, changed = artifacts.discard(item["artifact_id"], expected_updated_at=item["updated_at"])
    assert changed and deleted["deleted_at"] is not None
    assert artifacts.get(item["artifact_id"]) is None
    assert artifacts.list_artifacts(session="mike") == []
    assert source.read_bytes() == original_bytes
    assert media_store.get(media["asset_id"])["deleted_at"] is None
    assert agents.get_by_session("mike")["agent_id"] == agent_id
    _, changed = artifacts.discard(item["artifact_id"], expected_updated_at=item["updated_at"])
    assert not changed
    question = _question()
    with pytest.raises(ValueError, match="dedicated dismissal"):
        artifacts.discard(question["artifact_id"], expected_updated_at=question["updated_at"])
    plan = task_plans.create(session="mike", title="Keep working", items=[{"title": "Build"}])
    wrapper = artifacts.list_artifacts(session="mike", type="plan")[0]
    artifacts.discard(wrapper["artifact_id"], expected_updated_at=wrapper["updated_at"])
    assert task_plans.get(plan["plan_id"])["status"] == "active"
    assert artifacts.pending_deliveries() == []


def test_archived_decision_still_resolves_and_delivery_survives_reconnection(tmp_path):
    _agent(tmp_path)
    question = _question()
    decision_id = question["decision"]["decision_id"]
    archived, _ = artifacts.archive(question["artifact_id"], archived=True,
                                   expected_updated_at=question["updated_at"])
    db.close_local()
    restored_row = artifacts.get(question["artifact_id"])
    assert restored_row["archived_at"] == archived["archived_at"]
    resolved, _ = artifacts.resolve(decision_id, answer={"text": "Keep only the header"}, expected_revision=1)
    assert resolved["decision"]["status"] == "answered"
    assert resolved["archived_at"] == archived["archived_at"]
    expected_prompt = artifacts.format_delivery_prompt(artifacts.pending_deliveries()[0])
    db.close_local()
    pending = artifacts.pending_deliveries()
    assert len(pending) == 1 and pending[0]["answer"] == {"text": "Keep only the header"}
    assert artifacts.format_delivery_prompt(pending[0]) == expected_prompt
    assert artifacts.delivery_pending(decision_id)


def test_archive_marker_survives_update_but_old_timestamp_cannot_discard(tmp_path, monkeypatch):
    _agent(tmp_path)
    monkeypatch.setattr(db, "now_ms", lambda: 1000)
    item = artifacts.create(session="mike", type="document", title="Result", payload={"content": "First"})
    archived, _ = artifacts.archive(item["artifact_id"], archived=True, expected_updated_at=item["updated_at"])
    edited = artifacts.update(item["artifact_id"], {"payload_patch": {"content": "Changed"}})
    assert edited["archived_at"] == archived["archived_at"]
    assert edited["updated_at"] > archived["updated_at"]
    with pytest.raises(ValueError, match="artifact changed"):
        artifacts.discard(item["artifact_id"], expected_updated_at=archived["updated_at"])


def test_plan_sync_invalidates_archive_timestamp_without_changing_marker(tmp_path, monkeypatch):
    _agent(tmp_path)
    monkeypatch.setattr(db, "now_ms", lambda: 1000)
    plan = task_plans.create(session="mike", title="Build", items=[{"id": "build", "title": "Build"}])
    wrapper = artifacts.list_artifacts(session="mike", type="plan")[0]
    archived, _ = artifacts.archive(wrapper["artifact_id"], archived=True, expected_updated_at=wrapper["updated_at"])
    task_plans.update_item(task_plans.item_key(plan["plan_id"], "build"), "in_progress")
    updated = artifacts.get(wrapper["artifact_id"])
    assert updated["archived_at"] == archived["archived_at"]
    assert updated["updated_at"] > archived["updated_at"]
    with pytest.raises(ValueError, match="artifact changed"):
        artifacts.archive(wrapper["artifact_id"], archived=False, expected_updated_at=archived["updated_at"])


def test_competing_devices_cannot_overwrite_question_answer(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _agent(tmp_path)
    question = _question()
    decision_id = question["decision"]["decision_id"]
    ready = Barrier(2)

    def answer(option_id):
        try:
            ready.wait(timeout=5)
            artifacts.resolve(decision_id, answer={"option_id": option_id}, expected_revision=1)
            return option_id, True
        except ValueError as exc:
            assert "different choice or answer" in str(exc)
            return option_id, False
        finally:
            db.close_local()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(answer, ["current", "compact"]))
    winner = [option for option, succeeded in results if succeeded]
    assert len(winner) == 1
    resolved = artifacts.get(question["artifact_id"])["decision"]
    assert resolved["answer"]["option_id"] == winner[0]
    assert resolved["revision"] == 2
    pending = artifacts.pending_deliveries()
    assert len(pending) == 1 and pending[0]["answer"] == resolved["answer"]


@pytest.mark.parametrize("terminal", ["answered", "cancelled", "expired"])
def test_archived_terminal_questions_remain_in_history_without_returning_to_attention(tmp_path, monkeypatch, terminal):
    _agent(tmp_path)
    clock = [1000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    question = _question(expires_at=2000 if terminal == "expired" else None)
    identifier, decision_id = question["artifact_id"], question["decision"]["decision_id"]
    archived, _ = artifacts.archive(identifier, archived=True, expected_updated_at=question["updated_at"])
    if terminal == "answered":
        artifacts.resolve(decision_id, expected_revision=1, answer={"option_id": "current"})
    elif terminal == "cancelled":
        artifacts.dismiss(decision_id, expected_revision=1)
    else:
        clock[0] = 2000
    # Generic history enumeration applies expiry, while keeping every terminal record.
    history = artifacts.list_artifacts(session="mike", type="question")
    assert len(history) == 1
    assert history[0] == artifacts.get(identifier)
    assert history[0]["archived_at"] == archived["archived_at"]
    assert history[0]["decision"]["status"] == terminal
    assert history[0]["decision"]["options"] == question["decision"]["options"]
    assert bool(history[0]["decision"]["answer"]) == (terminal == "answered")
    assert artifacts.attention(include_questions=True, include_archived=True) == []
    with pytest.raises(ValueError, match="dedicated dismissal"):
        artifacts.discard(identifier, expected_updated_at=history[0]["updated_at"])


def test_future_response_type_is_not_treated_as_a_single_choice_question(tmp_path):
    _agent(tmp_path)
    question = _question()
    decision_id = question["decision"]["decision_id"]
    db.conn().execute("UPDATE artifact_decisions SET response_type='future_format' WHERE decision_id=?", (decision_id,))
    with pytest.raises(ValueError, match="unsupported response_type"):
        artifacts.resolve(decision_id, expected_revision=1, answer={"text": "Do it"})
    assert artifacts.pending_deliveries() == []
