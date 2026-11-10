"""Tests for the structured eventlog writer."""
import datetime
import json
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import eventlog  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _diagnostics_on_for_the_whole_test():
    # conftest seeds the diagnostics settings in the cache only, which a re-read
    # replaces after CACHE_TTL_SEC; a slow run (50 threads under load) then emitted
    # into a disabled log (G1 flake: 47-52 of 1000 lines lost). Stored, the seed
    # survives the re-read.
    from lib import diagnostics_settings
    diagnostics_settings.update({"enabled": True, "categories": list(diagnostics_settings.CATEGORIES)})


def _read_lines(p: pathlib.Path) -> list[dict]:
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln]


def _db_rows(sql: str, params: tuple = ()) -> list:
    from lib import telemetry
    return telemetry.conn().execute(sql, params).fetchall()


def test_emit_writes_a_json_line(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    eventlog.emit("server", "httpRequest", duration_ms=12, detail={"path": "/agents"})
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    rows = _read_lines(files[0])
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "server"
    assert row["event"] == "httpRequest"
    assert row["level"] == "info"
    assert row["duration_ms"] == 12
    assert row["detail"]["path"] == "/agents"
    assert row["ts"].endswith("Z")
    db_row = _db_rows(
        "SELECT source, event, duration_ms, path, status, detail FROM diagnostic_events"
    )[0]
    assert db_row["source"] == "server"
    assert db_row["event"] == "httpRequest"
    assert db_row["duration_ms"] == 12
    assert json.loads(db_row["detail"])["path"] == "/agents"


def test_emit_is_a_noop_when_diagnostics_are_disabled(tmp_path, monkeypatch):
    from lib import diagnostics_settings, telemetry
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    diagnostics_settings.update({"enabled": False, "categories": []})
    eventlog.emit("server", "httpRequest", path="/private", duration_ms=10)
    assert not list(tmp_path.glob("*.jsonl"))
    assert telemetry.conn().execute(
        "SELECT count(*) FROM diagnostic_events").fetchone()[0] == 0


def test_emit_with_no_optional_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    eventlog.emit("client", "playStart")
    rows = _read_lines(next(tmp_path.glob("*.jsonl")))
    # Optional columns should be omitted, not nulled.
    assert "session" not in rows[0]
    assert "backend_session_id" not in rows[0]
    assert "detail" not in rows[0]
    row = _db_rows(
        "SELECT session, backend_session_id, detail FROM diagnostic_events"
    )[0]
    assert row["session"] is None
    assert row["backend_session_id"] is None
    assert row["detail"] is None


def test_emit_promotes_known_columns_for_easy_sql(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    eventlog.emit("scheduler", "tickPicked",
                  session="claude", persona="Mike",
                  agent_id="agent-1",
                  clip_id=7,
                  clip_url="/audio/1.mp3",
                  request_id="req-1",
                  client_id="ios-1",
                  sse_event_id=42,
                  path="/send",
                  status=200)
    row = _read_lines(next(tmp_path.glob("*.jsonl")))[0]
    assert row["session"] == "claude"
    assert row["persona"] == "Mike"
    assert row["clip_url"] == "/audio/1.mp3"
    db_row = _db_rows(
        """SELECT session, persona, agent_id, clip_id, clip_url, request_id,
                  client_id, sse_event_id, path, status
             FROM diagnostic_events"""
    )[0]
    assert dict(db_row) == {
        "session": "claude",
        "persona": "Mike",
        "agent_id": "agent-1",
        "clip_id": 7,
        "clip_url": "/audio/1.mp3",
        "request_id": "req-1",
        "client_id": "ios-1",
        "sse_event_id": 42,
        "path": "/send",
        "status": 200,
    }


def test_emit_accepts_typed_event_context(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    context = eventlog.EventContext(
        trace_id="trace-1",
        agent_id="agent-1",
        session="mike",
        backend_session_id="backend-1",
    )

    eventlog.emit("server", "dispatch", context=context, detail={"backend": "claude"})

    row = _read_lines(next(tmp_path.glob("*.jsonl")))[0]
    assert row["trace_id"] == "trace-1"
    assert row["session"] == "mike"
    assert row["backend_session_id"] == "backend-1"
    assert row["detail"] == {"agent_id": "agent-1", "backend": "claude"}
    db_row = _db_rows(
        "SELECT trace_id, session, agent_id, backend_session_id, detail FROM diagnostic_events"
    )[0]
    assert db_row["trace_id"] == "trace-1"
    assert db_row["session"] == "mike"
    assert db_row["agent_id"] == "agent-1"
    assert db_row["backend_session_id"] == "backend-1"
    assert json.loads(db_row["detail"]) == {"agent_id": "agent-1", "backend": "claude"}


def test_emit_auto_attaches_db_trace_for_session(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    from lib import agents as agents_db
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V1", cwd="/tmp", session="claude")
    agents_db.set_trace(agent_id, "trace-from-db")

    eventlog.emit("server", "thing", session="claude")

    row = _read_lines(next(tmp_path.glob("*.jsonl")))[0]
    assert row["trace_id"] == "trace-from-db"
    db_row = _db_rows("SELECT trace_id FROM diagnostic_events")[0]
    assert db_row["trace_id"] == "trace-from-db"


def test_emit_daily_files(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    d1 = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc)
    d2 = datetime.datetime(2026, 1, 2, 10, 0, tzinfo=datetime.timezone.utc)
    eventlog.emit("server", "a", now=d1)
    eventlog.emit("server", "b", now=d2)
    assert (tmp_path / "2026-01-01.jsonl").is_file()
    assert (tmp_path / "2026-01-02.jsonl").is_file()


def test_emit_handles_non_serialisable_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    eventlog.emit("server", "weird", detail={"obj": object()})
    rows = _read_lines(next(tmp_path.glob("*.jsonl")))
    assert rows[0]["event"] == "weird"
    # Either fell back to _repr or skipped detail — both acceptable.
    if "detail" in rows[0]:
        assert "_repr" in rows[0]["detail"]
    db_row = _db_rows("SELECT detail FROM diagnostic_events")[0]
    if db_row["detail"] is not None:
        assert "_repr" in json.loads(db_row["detail"])


def test_emit_redacts_message_content_and_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    eventlog.emit("server", "send", detail={
        "text": "private words", "token": "secret-token",
        "forced_session": "arnold-e871",
    })
    detail = _read_lines(next(tmp_path.glob("*.jsonl")))[0]["detail"]
    assert "private words" not in detail["text"]
    assert "secret-token" not in detail["token"]
    assert "length=13" in detail["text"]
    assert detail["forced_session"] == "arnold-e871"


def test_emit_exception_records_traceback(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    try:
        raise ValueError("kaboom")
    except ValueError as e:
        eventlog.emit_exception("server", "thingFailed", e, detail={"path": "/x"})
    row = _read_lines(next(tmp_path.glob("*.jsonl")))[0]
    assert row["level"] == "error"
    assert row["detail"]["error_type"] == "ValueError"
    assert "kaboom" in row["detail"]["error"]
    assert "ValueError" in row["detail"]["traceback"]
    assert row["detail"]["path"] == "/x"
    db_row = _db_rows(
        "SELECT level, detail FROM diagnostic_events WHERE event = 'thingFailed'"
    )[0]
    assert db_row["level"] == "error"
    detail = json.loads(db_row["detail"])
    assert detail["error_type"] == "ValueError"
    assert "kaboom" in detail["error"]


def test_emit_concurrent_threads_dont_lose_lines(tmp_path, monkeypatch):
    import uuid
    import warnings

    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    marker = uuid.uuid4().hex
    N, per_thread = 50, 20
    def writer(i):
        for j in range(per_thread):
            eventlog.emit("test", "burst", detail={"test_marker": marker, "i": i, "j": j})
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(N)]
    for t in threads: t.start()
    for t in threads: t.join()
    rows = [row for path in sorted(tmp_path.glob("*.jsonl")) for row in _read_lines(path)]
    db_rows = [
        {"source": row["source"], "event": row["event"],
         "detail": json.loads(row["detail"]) if row["detail"] is not None else None}
        for row in _db_rows("SELECT source, event, detail FROM diagnostic_events")
    ]
    def is_ours(row):
        return isinstance(row.get("detail"), dict) and row["detail"].get("test_marker") == marker
    owned = {"jsonl": [row for row in rows if is_ours(row)],
             "sqlite": [row for row in db_rows if is_ours(row)]}
    foreign = {"jsonl": [row for row in rows if not is_ours(row)],
               "sqlite": [row for row in db_rows if not is_ours(row)]}
    audit = {"test_marker": marker,
             "total_rows": {"jsonl": len(rows), "sqlite": len(db_rows)},
             "owned_rows": {sink: len(items) for sink, items in owned.items()},
             "foreign_rows": foreign,
             "eventlogDropped": [row for row in rows if row.get("event") == "eventlogDropped"]}
    if any(foreign.values()):
        # Visible on passing runs too: neither an unrelated callback nor a
        # JSONL-only drop summary is silently discarded by marker selection.
        warnings.warn("eventlog concurrent foreign-row audit: " + json.dumps(audit, sort_keys=True),
                      pytest.PytestWarning, stacklevel=1)
    expected = {(i, j) for i in range(N) for j in range(per_thread)}
    for sink, emitted in owned.items():
        assert len(emitted) == N * per_thread, (sink, audit)
        assert {(row["detail"]["i"], row["detail"]["j"]) for row in emitted} == expected, (sink, audit)
        assert all(row["source"] == "test" and row["event"] == "burst" for row in emitted), (sink, audit)


def test_sqlite_event_writers_do_not_hold_a_process_lock_while_waiting(
    tmp_path, monkeypatch
):
    """A blocked logger must not prevent the current DB owner from logging.

    The owner can only commit after its log call returns. Serializing event
    writers with a Python lock therefore deadlocks when a waiter acquired that
    lock before blocking on SQLite.
    """
    from lib import telemetry

    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    waiter_entered = threading.Event()
    release_waiter = threading.Event()
    owner_executed = threading.Event()

    class BlockingConnection:
        def execute(self, _sql, _params):
            waiter_entered.set()
            assert release_waiter.wait(timeout=2)

    class OwnerConnection:
        def execute(self, _sql, _params):
            owner_executed.set()

    def connection_for_thread():
        if threading.current_thread().name == "blocked-event-writer":
            return BlockingConnection()
        return OwnerConnection()

    monkeypatch.setattr(telemetry, "conn", connection_for_thread)
    blocked = threading.Thread(
        target=eventlog.emit,
        args=("test", "blocked"),
        name="blocked-event-writer",
    )
    owner = threading.Thread(
        target=eventlog.emit,
        args=("test", "owner"),
        name="transaction-owner",
    )
    blocked.start()
    assert waiter_entered.wait(timeout=1)
    try:
        owner.start()
        assert owner_executed.wait(timeout=0.25)
    finally:
        release_waiter.set()
        blocked.join(timeout=2)
        owner.join(timeout=2)

    assert not blocked.is_alive()
    assert not owner.is_alive()


def test_eventlog_can_be_repointed_for_test_isolation(tmp_path):
    eventlog.reset_for_tests(tmp_path)
    eventlog.emit("test", "isolated")

    assert list(tmp_path.glob("*.jsonl"))
    eventlog.reset_for_tests()


def test_sqlite_diagnostic_views_are_queryable(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    eventlog.emit(
        "server", "httpRequest",
        trace_id="trace-a",
        request_id="req-a",
        path="/send",
        status=200,
        duration_ms=11,
        detail={"method": "POST", "client": "127.0.0.1"},
    )
    eventlog.emit(
        "audio_stream", "broadcast",
        trace_id="trace-a",
        clip_id=9,
        clip_url="/audio/9.mp3",
        sse_event_id=3,
    )
    eventlog.emit("server", "boom", level="error", trace_id="trace-a")

    request = _db_rows("SELECT trace_id, request_id, path, status FROM requests")[0]
    assert dict(request) == {
        "trace_id": "trace-a",
        "request_id": "req-a",
        "path": "/send",
        "status": 200,
    }
    trace = _db_rows("SELECT events, path FROM trace_paths WHERE trace_id = ?", ("trace-a",))[0]
    assert trace["events"] == 3
    assert trace["path"] == "server:httpRequest -> audio_stream:broadcast -> server:boom"
    error = _db_rows("SELECT event, trace_id FROM errors")[0]
    assert dict(error) == {"event": "boom", "trace_id": "trace-a"}


def test_the_seeded_diagnostics_settings_outlive_their_cache_window(tmp_path, monkeypatch):
    # A test of this file running past the settings cache's window (CACHE_TTL_SEC)
    # under load must not see events switched off.
    from lib import diagnostics_settings
    monkeypatch.setattr(eventlog, "LOG_DIR", tmp_path)
    eventlog.emit("test", "before")
    later = diagnostics_settings.time.monotonic() + 10 * diagnostics_settings.CACHE_TTL_SEC
    monkeypatch.setattr(diagnostics_settings.time, "monotonic", lambda: later)
    eventlog.emit("test", "after")
    rows = _read_lines(next(tmp_path.glob("*.jsonl")))
    assert [r["event"] for r in rows] == ["before", "after"]
