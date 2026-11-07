"""Characterization tests for lib.sse_store (durable SSE replay rows).

Pins: the stored payload carries its own event_id, the ts/type/session/
agent_id columns are derived from the payload, replay ordering is by
event_id, limits are clamped, and stateful singleton types collapse to
their latest row in `recent_events`.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from lib import db, sse_store


def _row(event_id: int):
    return db.conn().execute(
        "SELECT event_id, ts, type, session, agent_id, payload "
        "FROM sse_events WHERE event_id = ?", (event_id,)).fetchone()


def test_record_returns_rowid_and_embeds_event_id_in_payload():
    event_id = sse_store.record_sse_event(
        {"type": "agent-state", "session": "theo", "agent_id": "a1",
         "ts": 1234, "state": "working"})
    row = _row(event_id)
    assert row["ts"] == 1234
    assert row["type"] == "agent-state"
    assert row["session"] == "theo"
    assert row["agent_id"] == "a1"
    payload = json.loads(row["payload"])
    assert payload == {"type": "agent-state", "session": "theo",
                       "agent_id": "a1", "ts": 1234, "state": "working",
                       "event_id": event_id}
    # Compact separators: no spaces in the stored JSON.
    assert ": " not in row["payload"] and ", " not in row["payload"]


def test_record_defaults_ts_and_blank_columns(monkeypatch):
    monkeypatch.setattr(sse_store, "now_ms", lambda: 777_000)
    event_id = sse_store.record_sse_event({"type": "ping"})
    row = _row(event_id)
    assert row["ts"] == 777_000
    assert row["session"] == "" and row["agent_id"] == ""
    assert json.loads(row["payload"])["event_id"] == event_id


def test_record_does_not_mutate_caller_dict():
    event = {"type": "x", "ts": 1}
    sse_store.record_sse_event(event)
    assert event == {"type": "x", "ts": 1}


def test_record_ids_are_monotonic():
    ids = [sse_store.record_sse_event({"type": "t", "ts": i}) for i in range(5)]
    assert ids == sorted(ids) and len(set(ids)) == 5


def test_events_after_returns_strictly_newer_in_id_order():
    ids = [sse_store.record_sse_event({"type": "t", "ts": i, "n": i})
           for i in range(4)]
    events = sse_store.events_after(ids[1])
    assert [e["event_id"] for e in events] == ids[2:]
    assert [e["n"] for e in events] == [2, 3]


@pytest.mark.parametrize("event_id", [0, -5])
def test_events_after_zero_or_negative_returns_everything(event_id):
    ids = [sse_store.record_sse_event({"type": "t", "ts": 1}) for _ in range(3)]
    assert [e["event_id"] for e in sse_store.events_after(event_id)] == ids


@pytest.mark.parametrize("limit,expected", [(0, 1), (-3, 1), (2, 2), (10_000, 5)])
def test_events_after_limit_is_clamped_between_1_and_5000(limit, expected):
    for _ in range(5):
        sse_store.record_sse_event({"type": "t", "ts": 1})
    assert len(sse_store.events_after(0, limit=limit)) == expected


def test_decode_fills_missing_fields_from_columns():
    # A row whose payload lost its envelope still replays with the
    # column-derived event_id / ts / type.
    db.conn().execute(
        "INSERT INTO sse_events (ts, type, session, agent_id, payload) "
        "VALUES (?, ?, ?, ?, ?)", (42, "legacy", "", "", "{}"))
    event = sse_store.events_after(0)[0]
    assert event["ts"] == 42 and event["type"] == "legacy"
    assert event["event_id"] >= 1


def test_decode_tolerates_corrupt_payload():
    db.conn().execute(
        "INSERT INTO sse_events (ts, type, session, agent_id, payload) "
        "VALUES (?, ?, ?, ?, ?)", (7, "broken", "", "", "not json"))
    [event] = sse_store.events_after(0)
    assert event["type"] == "broken" and event["ts"] == 7


def test_recent_events_respects_window(monkeypatch):
    now = 1_000_000
    monkeypatch.setattr(sse_store, "now_ms", lambda: now)
    old = sse_store.record_sse_event({"type": "t", "ts": now - 10_000})
    fresh = sse_store.record_sse_event({"type": "t", "ts": now - 1_000})
    ids = [e["event_id"] for e in sse_store.recent_events(5_000)]
    assert ids == [fresh]
    assert old not in ids


def test_recent_events_negative_window_only_keeps_events_at_or_after_now(monkeypatch):
    monkeypatch.setattr(sse_store, "now_ms", lambda: 500)
    sse_store.record_sse_event({"type": "t", "ts": 499})
    kept = sse_store.record_sse_event({"type": "t", "ts": 500})
    assert [e["event_id"] for e in sse_store.recent_events(-100)] == [kept]


@pytest.mark.parametrize("singleton", sorted(sse_store._STATEFUL_SINGLETON_TYPES))
def test_recent_events_collapses_singleton_types_to_latest(monkeypatch, singleton):
    monkeypatch.setattr(sse_store, "now_ms", lambda: 10_000)
    first = sse_store.record_sse_event({"type": singleton, "ts": 9_000, "v": 1})
    plain = sse_store.record_sse_event({"type": "agent-state", "ts": 9_100})
    latest = sse_store.record_sse_event({"type": singleton, "ts": 9_200, "v": 2})
    events = sse_store.recent_events(5_000)
    ids = [e["event_id"] for e in events]
    assert ids == [plain, latest]
    assert first not in ids
    assert [e for e in events if e["type"] == singleton][0]["v"] == 2


def test_recent_events_keeps_one_row_per_singleton_type_sorted_by_id(monkeypatch):
    monkeypatch.setattr(sse_store, "now_ms", lambda: 10_000)
    focus_a = sse_store.record_sse_event({"type": "agent-focus", "ts": 9_000})
    version = sse_store.record_sse_event({"type": "server-version", "ts": 9_001})
    other = sse_store.record_sse_event({"type": "transcript-updated", "ts": 9_002})
    focus_b = sse_store.record_sse_event({"type": "agent-focus", "ts": 9_003})
    ids = [e["event_id"] for e in sse_store.recent_events(60_000)]
    assert ids == [version, other, focus_b]
    assert focus_a not in ids


def test_event_row_is_never_left_without_its_event_id(monkeypatch):
    """A failure after the INSERT must not leave a replayable row lacking event_id."""
    real = db.conn()
    before = real.execute("SELECT COUNT(*) FROM sse_events").fetchone()[0]

    class FailingUpdate:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            if sql.lstrip().startswith("UPDATE sse_events"):
                raise db.sqlite3.OperationalError("database is locked")
            return real.execute(sql, *args)

    monkeypatch.setattr(sse_store, "conn", lambda: FailingUpdate())
    with pytest.raises(db.sqlite3.OperationalError):
        sse_store.record_sse_event({"type": "agent-state", "session": "theo"})
    assert real.execute("SELECT COUNT(*) FROM sse_events").fetchone()[0] == before
    monkeypatch.undo()
    for (payload,) in real.execute("SELECT payload FROM sse_events"):
        assert "event_id" in json.loads(payload)



def test_standalone_admission_takes_a_short_writer_gap_without_reminting(monkeypatch):
    """Native busy backoff misses a gap; known-prewrite polled admission yields.

    Both paths contend with a real independent SQLite writer. Only a Python
    prewrite admission poll releases that writer, so the baseline native
    INSERT wait is the intended RED (short test busy budget, not a timing race).
    """
    connection = db.conn()
    connection.execute("PRAGMA busy_timeout=60")
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    held = threading.Event()
    release = threading.Event()
    errors = []

    def holder():
        try:
            with sqlite3.connect(path, isolation_level=None, timeout=0) as other:
                other.execute("BEGIN IMMEDIATE")
                held.set()
                if not release.wait(2):
                    raise RuntimeError("test holder release deadline")
                other.execute("COMMIT")
        except BaseException as exc:
            errors.append(exc)
            held.set()

    thread = threading.Thread(target=holder)
    thread.start()
    real_sleep = time.sleep
    caller = threading.get_ident()
    polls = []

    def yielding_poll(seconds):
        if threading.get_ident() == caller and seconds == db._LOCK_POLL_SECONDS:
            polls.append(connection.in_transaction)
            release.set()
        return real_sleep(seconds)

    monkeypatch.setattr(db.time, "sleep", yielding_poll)
    try:
        assert held.wait(2) and not errors
        event_id = sse_store.record_sse_event({"type": "ping", "ts": 123, "n": 1})
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and not errors
    assert polls and not any(polls), "retry must precede write transaction admission"
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 60
    records = connection.execute("SELECT event_id,payload FROM sse_events").fetchall()
    assert [tuple(row) for row in records] == [(event_id, '{"type":"ping","ts":123,"n":1,"event_id":'+str(event_id)+'}')]
    assert [event["event_id"] for event in sse_store.events_after(event_id-1)] == [event_id]


def test_nested_event_rolls_back_with_its_caller():
    connection = db.conn()
    connection.execute("BEGIN IMMEDIATE")
    event_id = sse_store.record_sse_event({"type": "ping", "ts": 7})
    assert connection.in_transaction
    assert _row(event_id) is not None
    connection.execute("ROLLBACK")
    assert _row(event_id) is None


@pytest.mark.parametrize("stage", ["UPDATE sse_events", "RELEASE record_sse_event", "COMMIT"])
def test_after_insert_failure_never_replays_or_remints(monkeypatch, stage):
    real = db.conn()
    statements = []

    class Fault:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            statements.append(sql)
            if sql.startswith(stage):
                raise sqlite3.OperationalError("injected uncertain outcome")
            return real.execute(sql, *args)

        def commit(self):
            statements.append("COMMIT")
            if stage == "COMMIT":
                # Actually committed, then an acknowledgement is lost. No retry
                # may mint another row even though rollback is now impossible.
                real.commit()
                raise sqlite3.OperationalError("injected uncertain outcome")
            return real.commit()

    monkeypatch.setattr(sse_store, "conn", lambda: Fault())
    with pytest.raises(sqlite3.OperationalError, match="injected uncertain"):
        sse_store.record_sse_event({"type": "ping", "ts": 7})
    assert len([sql for sql in statements if sql.lstrip().startswith("INSERT INTO sse_events")]) == 1
    count = real.execute("SELECT COUNT(*) FROM sse_events").fetchone()[0]
    assert count == (1 if stage == "COMMIT" else 0)
    assert not real.in_transaction


def test_first_payload_encoding_precedes_standalone_writer_admission(monkeypatch):
    connection = db.conn()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    real_dumps = json.dumps
    encoded = []

    def encode(payload, *args, **kwargs):
        if payload.get("n") == "prepare-before-lock" and "event_id" not in payload:
            with sqlite3.connect(path, isolation_level=None, timeout=0) as other:
                other.execute("BEGIN IMMEDIATE")
                other.execute("COMMIT")
            encoded.append(dict(payload))
        return real_dumps(payload, *args, **kwargs)

    monkeypatch.setattr(sse_store.json, "dumps", encode)
    event = {"type": "ping", "ts": 123, "n": "prepare-before-lock"}
    event_id = sse_store.record_sse_event(event)
    assert encoded == [event]
    assert _row(event_id)["payload"] == '{"type":"ping","ts":123,"n":"prepare-before-lock","event_id":'+str(event_id)+'}'


def test_exhausted_standalone_admission_keeps_hub_failure_fanout_without_id(tmp_path):
    from lib import health
    from lib.audio_stream import AudioStream
    connection = db.conn()
    connection.execute("PRAGMA busy_timeout=20")
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path, isolation_level=None, timeout=0) as holder:
        holder.execute("BEGIN IMMEDIATE")
        stream = AudioStream(tmp_path / "audio")
        subscriber = stream.subscribe()
        try:
            stream.broadcast({"type": "agent-roster", "kind": "unchanged-failure"})
            payload = json.loads(subscriber.get(timeout=1))
        finally:
            stream.unsubscribe(subscriber)
            holder.execute("ROLLBACK")
    assert payload == {"type": "agent-roster", "kind": "unchanged-failure"}
    assert "event_id" not in payload
    assert connection.execute("SELECT COUNT(*) FROM sse_events").fetchone()[0] == 0
    assert not connection.in_transaction
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 20
    assert "locked" in health.snapshot()["sse"]["last_error"]


def test_nested_record_never_starts_or_commits_a_separate_transaction():
    connection = db.conn()
    connection.execute("BEGIN IMMEDIATE")
    statements = []
    connection.set_trace_callback(statements.append)
    try:
        first = sse_store.record_sse_event({"type": "ping", "ts": 1})
        second = sse_store.record_sse_event({"type": "ping", "ts": 2})
    finally:
        connection.set_trace_callback(None)
        connection.execute("ROLLBACK")
    assert not any(sql.upper().startswith(("BEGIN", "COMMIT", "END")) for sql in statements)
    assert second > first
    assert sse_store.events_after(0) == []
