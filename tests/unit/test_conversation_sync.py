"""Regression coverage for the app-facing transcript synchronization contract."""
from lib import agents as agents_db, message_store
from lib.conversation import load_conversation


def _load(session: str, **kwargs):
    return load_conversation(
        session=session,
        claude_finder=lambda _session_id: None,
        claude_parser=lambda _path: [],
        **kwargs,
    )


def _agent(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Reliable", voice_id="v", cwd=str(tmp_path), session="reliable",
    )
    agents_db.start_runtime(agent_id, "codex")
    agents_db.bind_backend_session(agent_id, "conversation-1")
    return agent_id


def _store(agent_id: str, turns: list[dict], *, session_id="conversation-1"):
    agents_db.store_transcript_turns(
        agent_id=agent_id,
        backend_session_id=session_id,
        source_file=f"/tmp/{session_id}.jsonl",
        turns=turns,
    )


def test_hidden_automation_does_not_consume_visible_snapshot_limit(tmp_path):
    agent_id = _agent(tmp_path)
    turns = [{"role": "user", "text": "old", "timestamp": "2026-01-01T00:00:00Z"}]
    turns.extend(
        {"role": "assistant", "text": f"heartbeat {i}",
         "timestamp": f"2026-01-01T00:{i + 1:02d}:00Z", "origin": "heartbeat"}
        for i in range(40)
    )
    turns.extend([
        {"role": "user", "text": "new question", "timestamp": "2026-01-02T00:00:00Z"},
        {"role": "assistant", "text": "new answer", "timestamp": "2026-01-02T00:00:01Z"},
    ])
    _store(agent_id, turns)
    agents_db.conn().execute(
        "UPDATE messages SET origin = 'heartbeat' WHERE agent_id = ? AND text LIKE 'heartbeat %'",
        (agent_id,),
    )

    response = _load("reliable", limit=3, include_automated=False)

    assert [turn["text"] for turn in response["turns"]] == [
        "old", "new question", "new answer",
    ]
    assert response["conversation_id"] == "conversation-1"
    assert response["includes_automated"] is False


def test_snapshot_reports_more_visible_history(tmp_path):
    agent_id = _agent(tmp_path)
    _store(agent_id, [
        {"role": "user", "text": f"message {i}",
         "timestamp": f"2026-01-01T00:00:{i:02d}Z"}
        for i in range(5)
    ])

    response = _load("reliable", limit=2, include_automated=False)

    assert [turn["text"] for turn in response["turns"]] == ["message 3", "message 4"]
    assert response["has_more"] is True

    older = _load(
        "reliable",
        limit=2,
        include_automated=False,
        before_message_id=response["turns"][0]["id"],
    )
    assert [turn["text"] for turn in older["turns"]] == ["message 1", "message 2"]
    assert older["has_more"] is True


def test_truncated_delta_cursor_stops_at_last_delivered_revision(tmp_path):
    agent_id = _agent(tmp_path)
    _store(agent_id, [
        {"role": "user", "text": f"message {i}",
         "timestamp": f"2026-01-01T00:00:{i:02d}Z"}
        for i in range(5)
    ])

    first = _load("reliable", after_revision=1, limit=2, include_automated=False)
    assert len(first["turns"]) == 2
    assert first["has_more"] is True
    assert first["latest_revision"] == first["turns"][-1]["revision"]

    second = _load(
        "reliable",
        after_revision=first["latest_revision"],
        limit=2,
        include_automated=False,
    )
    assert second["turns"]
    assert {turn["id"] for turn in first["turns"]}.isdisjoint(
        turn["id"] for turn in second["turns"]
    )


def test_new_backend_conversation_has_distinct_identity(tmp_path):
    agent_id = _agent(tmp_path)
    _store(agent_id, [{"role": "assistant", "text": "old answer", "timestamp": "1"}])
    old = _load("reliable")

    agents_db.bind_backend_session(agent_id, "conversation-2")
    _store(
        agent_id,
        [{"role": "assistant", "text": "new answer", "timestamp": "2"}],
        session_id="conversation-2",
    )
    new = _load("reliable")

    assert old["conversation_id"] == "conversation-1"
    assert new["conversation_id"] == "conversation-2"
    assert [turn["text"] for turn in new["turns"]] == ["new answer"]


def test_tool_details_are_compact_in_transcript_and_loadable_on_demand(tmp_path):
    agent_id = _agent(tmp_path)
    huge = "command output\n" * 10_000
    _store(agent_id, [{
        "id": "tool-heavy-message",
        "role": "assistant",
        "text": "Finished inspection.",
        "timestamp": "2026-01-01T00:00:00Z",
        "display_cells": [{
            "id": "command-1", "kind": "command", "title": "Bash",
            "summary": "pytest", "status": "ok",
            "lines": [{"text": huge, "kind": "output"}],
        }],
    }])

    compact = _load(
        "reliable", limit=20, include_automated=False,
        include_tool_details=False)
    turn = compact["turns"][0]
    assert turn["tool_details_available"] is True
    assert turn["activity_count"] == 1
    assert "lines" not in turn["display_cells"][0]
    assert turn["display_cells"][0]["detail_count"] == 1
    assert len(str(compact)) < 5_000

    details = message_store.message_tool_details(
        session="reliable", message_id="tool-heavy-message")
    assert details["display_cells"][0]["lines"][0]["text"] == huge


def test_long_assistant_message_text_is_never_compacted(tmp_path):
    agent_id = _agent(tmp_path)
    full_text = "Long answer paragraph. " * 2_000
    _store(agent_id, [{
        "id": "long-answer",
        "role": "assistant",
        "text": full_text,
        "timestamp": "2026-01-01T00:00:00Z",
        "display_cells": [{
            "id": "tool", "kind": "command", "title": "Explored",
            "summary": "compact me", "lines": [{"text": "detail"}],
        }],
    }])

    response = _load(
        "reliable", limit=20, include_automated=False,
        include_tool_details=False)
    assert response["turns"][0]["text"] == full_text


def test_compact_turn_keeps_authoritative_activity_count(tmp_path):
    agent_id = _agent(tmp_path)
    _store(agent_id, [{
        "id": "multi-tool", "role": "assistant", "timestamp": "2026-01-01T00:00:00Z",
        "text": "", "tools": [
            {"id": "read", "name": "Read", "summary": "a"},
            {"id": "edit", "name": "Edit", "summary": "b"},
        ],
        "display_cells": [
            {"id": "command", "kind": "command", "title": "Bash", "lines": []},
            {"id": "patch", "kind": "patch", "title": "Edit", "lines": []},
        ],
    }])
    compact = _load(
        "reliable", limit=20, include_automated=False,
        include_tool_details=False)
    turn = compact["turns"][0]
    # Native presentation shows the command cell plus the rich Edit tool; the
    # duplicate patch cell and non-edit Read tool are intentionally suppressed.
    assert turn["activity_count"] == 2
    assert len(turn["display_cells"]) == 1


def test_unchanged_transcript_file_is_not_reparsed_on_every_log(tmp_path):
    _agent(tmp_path)
    transcript = tmp_path / "conversation-1.jsonl"
    transcript.write_text("first version")
    parser_calls = []

    def parse(path):
        parser_calls.append(path.read_text())
        return [{"role": "assistant", "text": path.read_text(), "timestamp": "1"}]

    def load():
        return load_conversation(
            session="reliable",
            claude_finder=lambda _session_id: transcript,
            claude_parser=parse,
        )

    assert [turn["text"] for turn in load()["turns"]] == ["first version"]
    assert [turn["text"] for turn in load()["turns"]] == ["first version"]
    assert parser_calls == ["first version"]

    transcript.write_text("second version with a different size")
    assert [turn["text"] for turn in load()["turns"]] == [
        "second version with a different size"
    ]
    assert parser_calls == ["first version", "second version with a different size"]


def test_transcript_import_emits_phases_with_interaction_id(tmp_path):
    from lib import telemetry
    _agent(tmp_path)
    transcript = tmp_path / "conversation-1.jsonl"
    transcript.write_text("payload")
    interaction = "11111111-2222-3333-4444-555555555555"

    load_conversation(
        session="reliable",
        claude_finder=lambda _session_id: transcript,
        claude_parser=lambda _path: [
            {"role": "assistant", "text": "done", "timestamp": "1"}],
        interaction_id=interaction,
    )

    row = telemetry.conn().execute(
        "SELECT detail FROM diagnostic_events "
        "WHERE source='transcript' AND event='import'").fetchone()
    assert row is not None
    assert interaction in row["detail"]
    assert '"parse_ms":' in row["detail"]
    assert '"store_ms":' in row["detail"]


def _fallback_answer(agent_id, text, finished_iso, request_id="rq"):
    """A delivered fallback answer, as model_fallbacks records it."""
    import json
    from datetime import datetime, timezone
    from lib import db
    finished = int(datetime.fromisoformat(finished_iso).replace(
        tzinfo=timezone.utc).timestamp() * 1000)
    db.conn().execute(
        """INSERT INTO model_fallback_attempts
             (request_id, agent_id, runtime_id, attempt, backend, model, effort,
              status, reason, started_at, finished_at, result_json)
           VALUES (?, ?, ?, 0, 'agy', 'gemini-test', '', 'completed', 'limit',
                   ?, ?, ?)""",
        (request_id, agent_id, agents_db.current_runtime_id(agent_id),
         finished - 1000, finished, json.dumps({"text": text})),
    )


def test_recovered_answers_merge_by_time_instead_of_landing_after_the_newest_turn(tmp_path):
    """A fallback answer appended last stranded the agent's latest reply above it.

    The bottom of the chat became an old recovered answer, so the user could
    not see the most recent real message at all.
    """
    agent_id = _agent(tmp_path)
    _store(agent_id, [
        {"role": "user", "text": "early question", "timestamp": "2026-01-01T10:00:00Z"},
        {"role": "assistant", "text": "early answer", "timestamp": "2026-01-01T10:00:05Z"},
        {"role": "user", "text": "later question", "timestamp": "2026-01-01T12:00:00Z"},
        {"role": "assistant", "text": "the newest real reply", "timestamp": "2026-01-01T12:00:05Z"},
    ])
    # Delivered while the primary model was unavailable, between the two turns.
    _fallback_answer(agent_id, "recovered mid-conversation answer", "2026-01-01T11:00:00")

    body = _load("reliable")
    texts = [t["text"] for t in body["turns"]]
    assert "recovered mid-conversation answer" in texts
    # It belongs at its own time, not at the end.
    assert texts[-1] == "the newest real reply"
    assert texts.index("recovered mid-conversation answer") < texts.index("the newest real reply")
    stamps = [t.get("timestamp") or "" for t in body["turns"]]
    assert stamps == sorted(stamps)
    # latest_ts followed the last element, so it moved backwards.
    assert body["latest_ts"] == "2026-01-01T12:00:05Z"


def test_merged_recovery_still_respects_the_requested_limit(tmp_path):
    agent_id = _agent(tmp_path)
    _store(agent_id, [
        {"role": "assistant", "text": f"turn {i}",
         "timestamp": f"2026-01-01T10:{i:02d}:00Z"} for i in range(5)
    ])
    _fallback_answer(agent_id, "recovered", "2026-01-01T10:02:30")

    body = _load("reliable", limit=3)
    assert len(body["turns"]) == 3
    assert body["has_more"] is True
    stamps = [t.get("timestamp") or "" for t in body["turns"]]
    assert stamps == sorted(stamps)


def _fallback_answer(agent_id, text, finished_iso, request_id="rq"):
    """A delivered fallback answer, as model_fallbacks records it."""
    import json
    from datetime import datetime, timezone
    from lib import db
    finished = int(datetime.fromisoformat(finished_iso).replace(
        tzinfo=timezone.utc).timestamp() * 1000)
    db.conn().execute(
        """INSERT INTO model_fallback_attempts
             (request_id, agent_id, runtime_id, attempt, backend, model, effort,
              status, reason, started_at, finished_at, result_json)
           VALUES (?, ?, ?, 0, 'agy', 'gemini-test', '', 'completed', 'limit',
                   ?, ?, ?)""",
        (request_id, agent_id, agents_db.current_runtime_id(agent_id),
         finished - 1000, finished, json.dumps({"text": text})),
    )


def test_recovered_answers_appear_in_a_full_load_in_time_order(tmp_path):
    agent_id = _agent(tmp_path)
    _store(agent_id, [
        {"role": "user", "text": "early question", "timestamp": "2026-01-01T10:00:00Z"},
        {"role": "assistant", "text": "the newest real reply", "timestamp": "2026-01-01T12:00:05Z"},
    ])
    _fallback_answer(agent_id, "recovered answer", "2026-01-01T11:00:00")

    body = _load("reliable")
    texts = [t["text"] for t in body["turns"]]
    assert "recovered answer" in texts
    assert texts[-1] == "the newest real reply"
    stamps = [t.get("timestamp") or "" for t in body["turns"]]
    assert stamps == sorted(stamps)


def test_a_delta_returns_only_rows_newer_than_the_cursor(tmp_path):
    """Recovered rows have no revision, so a delta repeated them forever.

    The client re-applied the same rows on every poll and never converged on
    the newest turn, leaving the pane stuck on an old recovered answer.
    """
    agent_id = _agent(tmp_path)
    _store(agent_id, [
        {"role": "user", "text": "early question", "timestamp": "2026-01-01T10:00:00Z"},
        {"role": "assistant", "text": "early answer", "timestamp": "2026-01-01T10:00:05Z"},
    ])
    _fallback_answer(agent_id, "recovered answer", "2026-01-01T11:00:00")
    cursor = _load("reliable")["latest_revision"]

    _store(agent_id, [
        {"role": "user", "text": "early question", "timestamp": "2026-01-01T10:00:00Z"},
        {"role": "assistant", "text": "early answer", "timestamp": "2026-01-01T10:00:05Z"},
        {"role": "assistant", "text": "brand new reply", "timestamp": "2026-01-01T12:00:00Z"},
    ])

    delta = _load("reliable", after_revision=cursor)
    texts = [t["text"] for t in delta["turns"]]
    assert "brand new reply" in texts
    assert "recovered answer" not in texts
    assert all(int(t.get("revision") or 0) > cursor for t in delta["turns"]), \
        [(t.get("revision"), t["text"][:24]) for t in delta["turns"]]
