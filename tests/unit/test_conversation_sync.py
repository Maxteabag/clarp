"""Regression coverage for the app-facing transcript synchronization contract."""
import pytest

from lib import agents as agents_db, backends, identity, message_store
from lib.conversation import load_conversation


@pytest.fixture(autouse=True)
def _no_transcript_on_disk(monkeypatch):
    """The read model asks the agent's backend for its transcript; these
    agents are Claude-backed with no jsonl on disk unless a test says so."""
    _reader(monkeypatch, lambda _session_id: None, lambda _path: [])


def _reader(monkeypatch, finder, parser):
    claude = backends.by_id("claude")
    monkeypatch.setattr(claude, "find_transcript",
                        lambda session_id, home=None, *, cwd="": finder(session_id))
    monkeypatch.setattr(claude, "parse_transcript", parser)


def _load(session: str, **kwargs):
    return load_conversation(session=session, **kwargs)


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


@pytest.mark.parametrize("background", [False, True])
def test_deferred_native_import_serves_committed_history_without_success_metrics(
        tmp_path, monkeypatch, background):
    """The native result is INPUT; real conversation/cache decide completion."""
    import threading
    from lib import eventlog, transcript_import_cache as cache, transcript_incremental

    cache.reset_for_tests()
    agent_id = _agent(tmp_path)
    _store(agent_id, [{"role": "user", "text": "committed history",
                      "timestamp": "2026-01-01T00:00:00Z"}])
    source = tmp_path / "deferred.jsonl"
    source.write_text("{}\n")
    entered = threading.Event()
    success_events = []
    monkeypatch.setattr(backends, "find_session_jsonl", lambda *args: source)

    def deferred_native_import(**kwargs):
        entered.set()
        # Keep every old metrics key, so baseline reaches unwanted-success
        # publication rather than a missing-key/API/setup failure.
        return {"rows": 0, "parse_s": 0.0, "store_s": 0.0,
                "_clarp_transcript_completion_v1": "deferred"}

    original_emit = eventlog.emit

    def observe_success(category, event, **kwargs):
        if (category, event) == ("transcript", "import"):
            success_events.append(event)
        return original_emit(category, event, **kwargs)

    monkeypatch.setattr(transcript_incremental, "import_transcript", deferred_native_import)
    monkeypatch.setattr(eventlog, "emit", observe_success)
    try:
        response = _load("reliable", background_import=background)
        assert [turn["text"] for turn in response["turns"]] == ["committed history"]
        assert response["conversation_id"] == "conversation-1"
        assert entered.wait(2), "the real import closure did not consume its native input"
        assert cache.wait_for_background(2)
        assert success_events == [], "expected deferral was emitted as completed import"
        key = f"{agent_id}\0{source}"
        assert key not in cache._imported
        assert not cache._validated_at.get(key, cache._epoch)[0]
    finally:
        assert cache.reset_for_tests()


def test_true_first_open_still_imports_native_history_before_responding(tmp_path, monkeypatch):
    from tests.readmodel import rm_n14_harness as harness
    from lib import db, transcript_import_cache as cache

    cache.reset_for_tests()
    agent_id = _agent(tmp_path)
    source = tmp_path / "first-open.jsonl"
    source.write_bytes(harness.append_bytes("claude", 0))
    claude = backends.by_id("claude")
    monkeypatch.setattr(claude, "parse_transcript",
                        type(claude).parse_transcript.__get__(claude))
    monkeypatch.setattr(backends, "find_session_jsonl", lambda *args: source)
    try:
        response = _load("reliable", background_import=True)
        assert any(turn["text"] == "synthetic request 0" for turn in response["turns"])
        assert any("synthetic answer 0" in turn["text"] for turn in response["turns"])
        assert db.conn().execute(
            "SELECT count(*) FROM messages WHERE agent_id=? AND source_file=?",
            (agent_id, str(source))).fetchone()[0] > 0
        assert cache._imported[f"{agent_id}\0{source}"] == cache._signature(source)
    finally:
        assert cache.reset_for_tests()


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


def test_unknown_older_history_cursor_is_an_error_not_the_end_of_history(tmp_path):
    from lib.message_previews import UnknownHistoryCursor
    agent_id = _agent(tmp_path)
    _store(agent_id, [{"role": "user", "text": "message 0", "timestamp": "2026-01-01T00:00:00Z"}])
    with pytest.raises(UnknownHistoryCursor):
        _load("reliable", limit=2, include_automated=False, before_message_id="live-not-stored")


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
        agent=identity.resolve("reliable"), message_id="tool-heavy-message")
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


def test_unchanged_transcript_file_is_not_reparsed_on_every_log(tmp_path, monkeypatch):
    _agent(tmp_path)
    transcript = tmp_path / "conversation-1.jsonl"
    transcript.write_text("first version")
    parser_calls = []

    def parse(path):
        parser_calls.append(path.read_text())
        return [{"role": "assistant", "text": path.read_text(), "timestamp": "1"}]

    _reader(monkeypatch, lambda _session_id: transcript, parse)

    def load():
        return load_conversation(session="reliable")

    assert [turn["text"] for turn in load()["turns"]] == ["first version"]
    assert [turn["text"] for turn in load()["turns"]] == ["first version"]
    assert parser_calls == ["first version"]

    transcript.write_text("second version with a different size")
    assert [turn["text"] for turn in load()["turns"]] == [
        "second version with a different size"
    ]
    assert parser_calls == ["first version", "second version with a different size"]


def test_transcript_import_emits_phases_with_interaction_id(tmp_path, monkeypatch):
    from lib import telemetry
    _agent(tmp_path)
    transcript = tmp_path / "conversation-1.jsonl"
    transcript.write_text("payload")
    interaction = "11111111-2222-3333-4444-555555555555"

    _reader(monkeypatch, lambda _session_id: transcript,
            lambda _path: [{"role": "assistant", "text": "done", "timestamp": "1"}])
    load_conversation(session="reliable", interaction_id=interaction)

    row = telemetry.conn().execute(
        "SELECT detail FROM diagnostic_events "
        "WHERE source='transcript' AND event='import'").fetchone()
    assert row is not None
    assert interaction in row["detail"]
    assert '"parse_ms":' in row["detail"]
    assert '"store_ms":' in row["detail"]


def _fallback_receipt(agent_id, text, finished_iso, request_id="rq"):
    """A background-retry receipt, as model_fallbacks records it."""
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


def test_a_retry_receipt_never_becomes_a_chat_turn(tmp_path):
    """An agent never switches provider, so no receipt may surface as its reply.

    Earlier releases merged completed fallback answers into the conversation.
    """
    agent_id = _agent(tmp_path)
    _store(agent_id, [
        {"role": "user", "text": "early question", "timestamp": "2026-01-01T10:00:00Z"},
        {"role": "assistant", "text": "the newest real reply", "timestamp": "2026-01-01T12:00:05Z"},
    ])
    _fallback_receipt(agent_id, "receipt text that must stay hidden", "2026-01-01T11:00:00")

    body = _load("reliable")
    texts = [t["text"] for t in body["turns"]]
    assert "receipt text that must stay hidden" not in texts
    assert texts[-1] == "the newest real reply"
    assert body["latest_ts"] == "2026-01-01T12:00:05Z"


def test_a_delta_returns_only_rows_newer_than_the_cursor(tmp_path):
    """A delta must not repeat rows the client already has."""
    agent_id = _agent(tmp_path)
    _store(agent_id, [
        {"role": "user", "text": "early question", "timestamp": "2026-01-01T10:00:00Z"},
        {"role": "assistant", "text": "early answer", "timestamp": "2026-01-01T10:00:05Z"},
    ])
    cursor = _load("reliable")["latest_revision"]

    _store(agent_id, [
        {"role": "user", "text": "early question", "timestamp": "2026-01-01T10:00:00Z"},
        {"role": "assistant", "text": "early answer", "timestamp": "2026-01-01T10:00:05Z"},
        {"role": "assistant", "text": "brand new reply", "timestamp": "2026-01-01T12:00:00Z"},
    ])

    delta = _load("reliable", after_revision=cursor)
    texts = [t["text"] for t in delta["turns"]]
    assert "brand new reply" in texts
    assert "early answer" not in texts
    assert all(int(t.get("revision") or 0) > cursor for t in delta["turns"]), \
        [(t.get("revision"), t["text"][:24]) for t in delta["turns"]]


@pytest.mark.parametrize('sparse_large', [False, True])
def test_chat_read_returns_stored_rows_without_waiting_for_import(tmp_path, monkeypatch, sparse_large):
    import threading
    from lib import backends, transcript_import_cache, db
    agent = _agent(tmp_path)
    _store(agent, [{'role':'user','text':'stored message','timestamp':'2026-01-01T00:00:00Z'}])
    source = tmp_path / 'growing.jsonl'
    source.write_text('changed')
    if sparse_large:
        monkeypatch.setattr(transcript_import_cache, '_host_started_at', None)
        assert transcript_import_cache.note_host_started(transcript_import_cache.time.monotonic())
        with source.open('r+b') as stream:
            stream.truncate(50 * 1024 * 1024)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    answers, errors = [], []
    monkeypatch.setattr(backends, 'find_session_jsonl', lambda *a: source)
    def parse(*args):
        entered.set()
        assert release.wait(3)
        return [{'role':'user','text':'new message','timestamp':'2026-01-01T00:00:00Z'}]
    monkeypatch.setattr(backends, 'parse_turns', parse)
    def reading():
        try:
            answers.append(load_conversation(session='reliable', background_import=True))
        except Exception as exc:
            errors.append(exc)
        finally:
            returned.set()
            db.close_local()
    t = threading.Thread(target=reading)
    t.start()
    try:
        assert returned.wait(.5), 'chat read blocked behind transcript import'
        assert not errors
        assert answers[0]['turns'][0]['text'] == 'stored message'
        assert entered.wait(1)
    finally:
        release.set()
        t.join(4)
        if hasattr(transcript_import_cache, 'wait_for_background'):
            assert transcript_import_cache.wait_for_background(4)

    refreshed = load_conversation(session='reliable', background_import=True)
    assert refreshed['turns'][0]['text'] == 'new message'
    assert transcript_import_cache.wait_for_background(3)


def test_only_client_rows_do_not_make_adopted_history_warm(tmp_path, monkeypatch):
    from lib import transcript_import_cache
    agent = _agent(tmp_path)
    _store(agent, [{'role':'user','text':'new request','timestamp':'2026-01-01T00:00:00Z'}])
    agents_db.conn().execute("UPDATE messages SET seq=-1, source_file='client:request' WHERE agent_id=?", (agent,))
    source = tmp_path / 'adopted.jsonl'
    source.write_text('prior history')
    def unexpected(*args, **kwargs):
        raise AssertionError('client-only rows must not defer first transcript import')
    monkeypatch.setattr(transcript_import_cache, 'schedule_import', unexpected)
    _reader(monkeypatch, lambda _: source,
            lambda _: [{'role':'assistant','text':'prior history','timestamp':'2025-01-01T00:00:00Z'}])
    result = load_conversation(session='reliable', background_import=True)
    assert any(t['text'] == 'prior history' for t in result['turns'])


def test_claude_subagent_cell_reaches_the_log_intact(tmp_path):
    import json
    from lib.claude_transcript import parse_turns

    agent_id = _agent(tmp_path)
    transcript = tmp_path / "parent.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": [
            {"type": "tool_use", "id": "toolu_bg", "name": "Agent", "input": {
                "description": "Map auth", "prompt": "Read it",
                "subagent_type": "Explore", "run_in_background": True}}]}},
        {"type": "user", "timestamp": "2026-01-01T00:00:01Z", "toolUseResult": {
            "isAsync": True, "status": "async_launched", "agentId": "a0bg"},
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_bg",
                                  "content": "Async agent launched successfully."}]}},
    ]) + "\n")
    _store(agent_id, parse_turns(transcript))

    [turn] = _load("reliable", limit=20)["turns"]
    [cell] = turn["display_cells"]
    assert cell["kind"] == "subagents"
    assert cell["status"] == "running"
    assert cell["ephemeral"] is True
    assert cell["background"] is True
    assert turn["tools"] == []

    [compact] = _load("reliable", limit=20, include_tool_details=False)["turns"]
    assert compact["display_cells"][0]["kind"] == "subagents"
    assert compact["display_cells"][0]["ephemeral"] is True
    assert compact["activity_count"] == 1


def test_a_delta_after_a_relink_carries_the_new_trace(tmp_path):
    """RM-N5: re-owning an admitted-but-never-launched user row under a new
    attempt rewrote its trace with no revision, so delta clients kept the old
    trace until a full reload."""
    agent_id = _agent(tmp_path)
    message_store.record_user_message(
        agent_id=agent_id, backend_session_id="conversation-1", client_msg_id="c-1",
        text="please do it", origin="user", sender_agent_id=None, trace_id="t-first")
    before = _load("reliable")["latest_revision"]

    message_store.relink_client_message("c-1", trace_id="t-second")

    delta = _load("reliable", after_revision=before)
    rows = [t for t in delta["turns"] if t.get("id") == "u-c-1"]
    assert [t.get("trace_id") for t in rows] == ["t-second"]
    assert delta["latest_revision"] > before
