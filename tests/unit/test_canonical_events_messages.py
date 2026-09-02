from lib import agents as agents_db
from lib.audio_stream import AudioStream
from lib.protocol import ClipStatus


def test_sse_broadcast_assigns_durable_event_ids(tmp_path):
    stream = AudioStream(tmp_path)

    stream.broadcast({"type": "agent-state", "session": "rachel", "kind": "thinking"})
    stream.broadcast({"type": "agent-state", "session": "rachel", "kind": "done"})

    recent = stream.recent()
    assert [ev["event_id"] for ev in recent] == [1, 2]
    assert all(ev["session"] == "rachel" for ev in recent)
    assert [ev["kind"] for ev in stream.recent(since_event_id=1)] == ["done"]


def test_recent_sse_compacts_stateful_singletons_in_durable_store(tmp_path):
    stream = AudioStream(tmp_path)
    stream.broadcast({"type": "agent-focus", "session": "rachel"})
    stream.broadcast({"type": "agent-focus", "session": "mike"})
    stream.broadcast({"type": "agent-state", "session": "mike", "kind": "thinking"})

    recent = stream.recent()
    assert [(event["type"], event.get("session")) for event in recent] == [
        ("agent-focus", "mike"),
        ("agent-state", "mike"),
    ]


def test_transcript_turns_are_upserted_with_stable_server_ids(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="t1")
    turns = [
        {"role": "user", "text": "hi", "timestamp": "2026-01-01T00:00:00Z"},
        {"role": "assistant", "text": "hello", "timestamp": "2026-01-01T00:00:01Z"},
    ]

    first = agents_db.store_transcript_turns(
        agent_id=agent_id,
        backend_session_id="backend-1",
        source_file="/tmp/transcript.jsonl",
        turns=turns,
    )
    second = agents_db.store_transcript_turns(
        agent_id=agent_id,
        backend_session_id="backend-1",
        source_file="/tmp/transcript.jsonl",
        turns=[{**turns[0], "text": "hi"}, {**turns[1], "text": "hello again"}],
    )

    assert [t["id"] for t in first] == [t["id"] for t in second]
    cached = agents_db.list_messages(agent_id=agent_id, backend_session_id="backend-1")
    assert [t["text"] for t in cached] == ["hi", "hello again"]
    assert first[1]["revision"] < second[1]["revision"]


def test_internal_metadata_is_removed_before_storage_and_api_reads(tmp_path):
    from lib import message_store
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    bsid = "backend-1"
    hidden = (
        "Visible reply.\n<oai-mem-citation><citation_entries>internal"
        "</citation_entries></oai-mem-citation>"
    )
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=bsid,
        source_file="/tmp/transcript.jsonl",
        turns=[{"role": "assistant", "text": hidden, "timestamp": "1"}],
    )
    stored = agents_db.conn().execute(
        "SELECT text FROM messages WHERE agent_id = ?", (agent_id,)
    ).fetchone()["text"]
    assert stored == "Visible reply.\n"

    # Defense at the server read boundary also cleans legacy rows that were
    # stored before this rule existed, without relying on an iOS/PWA filter.
    agents_db.conn().execute(
        "UPDATE messages SET text = ? WHERE agent_id = ?", (hidden, agent_id)
    )
    visible = message_store.list_messages(
        agent_id=agent_id, backend_session_id=bsid)
    assert visible[0]["text"] == "Visible reply.\n"


def test_streamed_internal_metadata_is_removed_before_live_storage(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="t1")
    live = agents_db.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id="backend-1", trace_id="t1",
        text="Visible.\n<environment_context><timezone>Europe/Oslo</timezone>",
    )
    assert live is not None
    assert live["text"] == "Visible.\n"


def test_codex_display_cells_trim_duplicate_non_edit_tools_for_clients(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    agents_db.store_transcript_turns(
        agent_id=agent_id,
        backend_session_id="backend-1",
        source_file="/tmp/transcript.jsonl",
        turns=[{
            "role": "assistant",
            "text": "",
            "timestamp": "2026-01-01T00:00:01Z",
            "display_cells": [{
                "kind": "command",
                "title": "Ran",
                "summary": "pytest",
                "status": "ok",
                "lines": [{"text": "ok", "kind": "output"}],
            }],
            "tools": [
                {"name": "Bash", "command": "pytest", "result": "x" * 1000},
                {"name": "Edit", "old": "a", "new": "b"},
            ],
        }],
    )

    visible = agents_db.list_messages(
        agent_id=agent_id, backend_session_id="backend-1")

    assert [tool["name"] for tool in visible[0]["tools"]] == ["Edit"]


def test_reimporting_unchanged_transcript_does_not_refresh_activity(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Adam", voice_id="V", cwd=str(tmp_path), session="adam"
    )
    turns = [{
        "role": "assistant",
        "text": "old reply",
        "timestamp": "2026-06-08T15:43:06.040Z",
    }]
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id="backend-1",
        source_file="/tmp/transcript.jsonl", turns=turns,
    )
    first_activity = agents_db.last_activity(agent_id)
    first_updated_at = agents_db.conn().execute(
        "SELECT updated_at FROM messages WHERE agent_id = ?", (agent_id,)
    ).fetchone()["updated_at"]

    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id="backend-1",
        source_file="/tmp/transcript.jsonl", turns=turns,
    )
    row = agents_db.conn().execute(
        "SELECT updated_at FROM messages WHERE agent_id = ?", (agent_id,)
    ).fetchone()

    assert row["updated_at"] == first_updated_at
    assert agents_db.last_activity(agent_id) == first_activity


def test_last_activity_ignores_routine_automation_state_and_messages(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Lena", voice_id="V", cwd=str(tmp_path), session="lena"
    )
    c = agents_db.conn()
    c.execute("DELETE FROM state_log WHERE agent_id = ?", (agent_id,))
    c.execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, timestamp, text,"
        " tools_json, updated_at, origin) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "m-real", agent_id, 0, "assistant",
            "1970-01-01T00:00:01.000Z", "Real reply", "[]", 1_000, "user",
        ),
    )
    c.execute(
        "INSERT INTO state_log (agent_id, runtime_id, ts, kind, detail) "
        "VALUES (?, NULL, ?, ?, ?)",
        (agent_id, 2_000, "idle", '{"origin":"heartbeat"}'),
    )
    c.execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, timestamp, text,"
        " tools_json, updated_at, origin) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "m-heartbeat", agent_id, 1, "assistant",
            "1970-01-01T00:00:03.000Z",
            "Heartbeat check: no action needed.",
            "[]", 3_000, "heartbeat",
        ),
    )

    real_activity = agents_db.last_activity(agent_id)
    assert 999 <= real_activity <= 1_000

    c.execute(
        "INSERT INTO state_log (agent_id, runtime_id, ts, kind, detail) "
        "VALUES (?, NULL, ?, ?, ?)",
        (agent_id, 3_500, "background", '{"label":"Watching WhatsApp"}'),
    )

    assert agents_db.last_activity(agent_id) == real_activity

    c.execute(
        "INSERT INTO state_log (agent_id, runtime_id, ts, kind, detail) "
        "VALUES (?, NULL, ?, ?, ?)",
        (agent_id, 4_000, "thinking", '{"origin":"user"}'),
    )

    assert agents_db.last_activity(agent_id) == real_activity


def test_live_assistant_row_superseded_despite_speak_markup(tmp_path):
    # Regression: the streamed live row carries <speak> markup; the durable
    # transcript turn has it stripped. The raw prefix-match missed, leaving both
    # (the duplicate-message bug). Markup-normalized match must delete the live row.
    agent_id = agents_db.create_agent(
        persona="Arnold", voice_id="V", cwd=str(tmp_path), session="arnold"
    )
    bsid = "backend-live"
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-1")
    agents_db.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=bsid, trace_id="trace-1",
        text="<speak>Right, on it</speak>",
    )
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=bsid, source_file="/tmp/t.jsonl",
        turns=[{"role": "assistant", "text": "Right, on it",
                "timestamp": "2026-01-01T00:00:01Z"}],
    )
    assistant = [m for m in agents_db.list_messages(agent_id=agent_id,
                 backend_session_id=bsid) if m["role"] == "assistant"]
    assert len(assistant) == 1, \
        f"live row must be superseded, not duplicated: {[m['text'] for m in assistant]}"


def test_transcript_message_revision_cursor_returns_changed_rows(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    first = agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id="backend-1",
        source_file="/tmp/transcript.jsonl",
        turns=[{"role": "assistant", "text": "hel", "timestamp": "same"}],
    )
    second = agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id="backend-1",
        source_file="/tmp/transcript.jsonl",
        turns=[{"role": "assistant", "text": "hello", "timestamp": "same"}],
    )

    changed = agents_db.list_messages(
        agent_id=agent_id, backend_session_id="backend-1",
        after_revision=first[0]["revision"],
    )
    assert len(changed) == 1
    assert changed[0]["id"] == second[0]["id"]
    assert changed[0]["text"] == "hello"
    assert changed[0]["revision"] == second[0]["revision"]


def test_transcript_truncation_advances_cursor_and_requires_replacement(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    turns = [
        {"role": "user", "text": "hi", "timestamp": "1"},
        {"role": "assistant", "text": "hello", "timestamp": "2"},
    ]
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id="backend-1",
        source_file="/tmp/transcript.jsonl", turns=turns,
    )
    first_revision = agents_db.latest_message_revision(
        agent_id=agent_id, backend_session_id="backend-1",
    )

    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id="backend-1",
        source_file="/tmp/transcript.jsonl", turns=turns[:1],
    )

    assert agents_db.latest_message_revision(
        agent_id=agent_id, backend_session_id="backend-1",
    ) > first_revision
    assert agents_db.conversation_requires_replace(
        agent_id=agent_id, backend_session_id="backend-1",
        after_revision=first_revision,
    )


def test_recorded_user_message_survives_and_links_to_transcript(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Adam", voice_id="V", cwd=str(tmp_path), session="adam"
    )
    old_turns = [
        {"role": "user", "text": "old question", "timestamp": "1"},
        {"role": "assistant", "text": "old answer", "timestamp": "2"},
    ]
    agents_db.store_transcript_turns(
        agent_id=agent_id,
        backend_session_id="backend-1",
        source_file="/tmp/transcript.jsonl",
        turns=old_turns,
    )

    # The user message is recorded durably the moment /send accepts it, keyed
    # by a stable client-authored id.
    recorded = agents_db.record_user_message(
        agent_id=agent_id,
        backend_session_id="backend-1",
        text="long dictated message",
        client_msg_id="trace-1",
    )
    assert recorded is not None
    assert recorded["kind"] is None  # durable, not a placeholder

    # Polling /log before Claude's hook has written the prompt re-imports the
    # old transcript. The accepted user message must not disappear.
    agents_db.store_transcript_turns(
        agent_id=agent_id,
        backend_session_id="backend-1",
        source_file="/tmp/transcript.jsonl",
        turns=old_turns,
    )
    assert not agents_db.conversation_requires_replace(
        agent_id=agent_id,
        backend_session_id="backend-1",
        after_revision=recorded["revision"],
    )
    visible = agents_db.list_messages(
        agent_id=agent_id, backend_session_id="backend-1"
    )
    assert [(m["role"], m["text"], m["kind"]) for m in visible] == [
        ("user", "old question", None),
        ("assistant", "old answer", None),
        ("user", "long dictated message", None),
    ]

    # When Claude's transcript catches up, its copy of the user turn is LINKED
    # to the durable client row (by send order) — not inserted as a second row.
    agents_db.store_transcript_turns(
        agent_id=agent_id,
        backend_session_id="backend-1",
        source_file="/tmp/transcript.jsonl",
        turns=[
            *old_turns,
            {"role": "user", "text": "long dictated message", "timestamp": "3"},
        ],
    )
    visible = agents_db.list_messages(
        agent_id=agent_id, backend_session_id="backend-1"
    )
    # Still exactly one "long dictated message" row — no duplicate.
    assert [(m["role"], m["text"], m["kind"]) for m in visible] == [
        ("user", "old question", None),
        ("assistant", "old answer", None),
        ("user", "long dictated message", None),
    ]
    assert sum(1 for m in visible if m["text"] == "long dictated message") == 1


def test_live_assistant_message_updates_and_final_transcript_replaces_it(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    bsid = "backend-1"
    old_turns = [
        {"role": "user", "text": "old question", "timestamp": "1"},
        {"role": "assistant", "text": "old answer", "timestamp": "2"},
    ]
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=bsid,
        source_file="/tmp/transcript.jsonl", turns=old_turns,
    )
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-1")

    live = agents_db.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=bsid,
        trace_id="trace-1", text="streaming hel",
    )
    assert live is not None
    agents_db.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=bsid,
        trace_id="trace-1", text="streaming hello",
    )

    # Re-importing an old transcript while the turn is still streaming must not
    # delete the live row.
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=bsid,
        source_file="/tmp/transcript.jsonl", turns=old_turns,
    )
    visible = agents_db.list_messages(agent_id=agent_id, backend_session_id=bsid)
    assert [m["text"] for m in visible] == [
        "old question", "old answer", "streaming hello",
    ]
    assert visible[-1]["kind"] == "live"

    # When the durable transcript catches up, the final assistant row replaces
    # the mutable live row instead of creating a duplicate bubble.
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=bsid,
        source_file="/tmp/transcript.jsonl",
        turns=[
            *old_turns,
            {"role": "user", "text": "new question", "timestamp": "3"},
            {"role": "assistant", "text": "streaming hello world", "timestamp": "4"},
        ],
    )
    visible = agents_db.list_messages(agent_id=agent_id, backend_session_id=bsid)
    assert [m["text"] for m in visible] == [
        "old question", "old answer", "new question", "streaming hello world",
    ]
    assert all(m["kind"] != "live" for m in visible)
    assert agents_db.conversation_requires_replace(
        agent_id=agent_id, backend_session_id=bsid,
        after_revision=live["revision"],
    )


def test_stable_client_id_is_idempotent_and_handles_repeats(tmp_path):
    from lib import message_store
    agent_id = agents_db.create_agent(
        persona="Adam", voice_id="V", cwd=str(tmp_path), session="adam"
    )
    bsid = "backend-1"

    # Same client id sent twice (retry) → ONE row, not two.
    a1 = message_store.record_user_message(
        agent_id=agent_id, backend_session_id=bsid, client_msg_id="m1", text="lol")
    a2 = message_store.record_user_message(
        agent_id=agent_id, backend_session_id=bsid, client_msg_id="m1", text="lol")
    assert a1["id"] == a2["id"]

    # Two DIFFERENT client ids with identical text (repeated sends) → two rows.
    message_store.record_user_message(
        agent_id=agent_id, backend_session_id=bsid, client_msg_id="m2", text="lol")

    visible = agents_db.list_messages(agent_id=agent_id, backend_session_id=bsid)
    lols = [m for m in visible if m["text"] == "lol"]
    assert len(lols) == 2, "same id dedupes; distinct ids for repeated text both survive"
    # Stable, client-authored ids surface verbatim (namespaced).
    assert {m["id"] for m in lols} == {"u-m1", "u-m2"}

    # A transcript import links its two 'lol' user turns to the two client rows
    # (by order) and appends the assistant reply — no duplicate user bubbles.
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=bsid,
        source_file="/tmp/t.jsonl",
        turns=[
            {"role": "user", "text": "lol", "timestamp": "1"},
            {"role": "user", "text": "lol", "timestamp": "2"},
            {"role": "assistant", "text": "reply", "timestamp": "3"},
        ],
    )
    visible = agents_db.list_messages(agent_id=agent_id, backend_session_id=bsid)
    assert sum(1 for m in visible if m["text"] == "lol") == 2
    assert sum(1 for m in visible if m["text"] == "reply") == 1


def test_record_clip_rejects_unknown_playback_status(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike"
    )

    try:
        agents_db.record_clip(agent_id=agent_id, path="/tmp/x.mp3", status="")
    except ValueError as exc:
        assert "invalid clip status" in str(exc)
    else:
        raise AssertionError("record_clip accepted an invalid status")

    clip_id = agents_db.record_clip(agent_id=agent_id, path="/tmp/y.mp3")
    assert clip_id
    row = agents_db.conn().execute(
        "SELECT status FROM clips WHERE clip_id = ?", (clip_id,)
    ).fetchone()
    assert row["status"] == ClipStatus.SYNTHESIZED
