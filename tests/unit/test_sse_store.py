"""Characterization tests for lib.sse_store (durable SSE replay rows).

Pins: the stored payload carries its own event_id, the ts/type/session/
agent_id columns are derived from the payload, replay ordering is by
event_id, limits are clamped, and stateful singleton types collapse to
their latest row in `recent_events`.
"""
from __future__ import annotations

import json

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
