from __future__ import annotations

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


def test_artifact_creation_revalidates_session_owner_inside_transaction(
    tmp_path, monkeypatch,
):
    agent_id = _agent(tmp_path)
    stale = agents.get_by_session("mike")
    agents.soft_delete(agent_id)
    monkeypatch.setattr(artifacts, "_agent", lambda _session: stale)

    with pytest.raises(ValueError, match="session changed"):
        artifacts.create(
            session="mike", type="document", title="Stale",
            payload={"content": "Must not appear"},
        )

    assert artifacts.list_artifacts(agent_id=agent_id) == []


def test_artifact_update_rejects_deleted_owner(tmp_path):
    agent_id = _agent(tmp_path)
    artifact = artifacts.create(
        session="mike", type="document", title="Before",
        payload={"content": "Before"},
    )
    agents.soft_delete(agent_id)

    with pytest.raises(ValueError, match="owner is no longer active"):
        artifacts.update(artifact["artifact_id"], {"title": "After"})


def test_media_publish_revalidates_session_owner_inside_transaction(
    tmp_path, monkeypatch,
):
    agent_id = _agent(tmp_path)
    stale = agents.get_by_session("mike")
    agents.soft_delete(agent_id)
    monkeypatch.setattr(
        media_store.agents_db, "get_by_session", lambda _session: stale)

    with pytest.raises(media_store.MediaError, match="session changed") as error:
        media_store.publish(
            session="mike", blob=b"%PDF-1.7\nstale", source_name="stale.pdf",
            content_type="application/pdf", media_dir=tmp_path / "media",
        )

    assert error.value.status == 409
    assert db.conn().execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
    assert list((tmp_path / "media/blobs").rglob("*.pdf")) == []
