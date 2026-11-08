"""Attempt-owned never-launched receipts: real writer and dispatcher boundaries."""
import json
import sqlite3

import pytest

from lib import agents, db, recovery_backoff, turn_lifecycle, turn_queue
from lib.protocol import SSEType
from lib.state_watcher import StateLogWatcher
from tests.unit.test_state_watcher import FakeStream
from tests.unit.test_turn_dispatch import _make_service

KEY = "dispatch_not_started_turn_id"


def _opened(agent, trace="t"):
    return turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id=trace,
                                    synthesize_audio=False)


def _last(agent):
    return json.loads(db.conn().execute(
        "SELECT detail FROM state_log WHERE agent_id=? ORDER BY state_id DESC LIMIT 1",
        (agent,)).fetchone()[0])


@pytest.mark.parametrize("mode", ["unknown", "settled", "newer", "foreign"])
def test_unbound_or_superseded_marker_cannot_settle_any_other_attempt(tmp_path, mode):
    _service, _backend, agent = _make_service(tmp_path)
    own = _opened(agent)
    if mode == "settled":
        turn_lifecycle.record_unlaunched(agent, "t", turn_id=own)
    later = _opened(agent) if mode == "newer" else None
    before = [tuple(row) for row in db.conn().execute("SELECT * FROM turns ORDER BY turn_id")]
    requested = None if mode == "unknown" else own
    turn_lifecycle.record_unlaunched(agent, "foreign" if mode == "foreign" else "t", turn_id=requested)
    assert KEY not in _last(agent)
    assert [tuple(row) for row in db.conn().execute("SELECT * FROM turns ORDER BY turn_id")] == before
    if later:
        assert agents.trace_launch_status(agent, "t") != "retryable"


def test_known_unlaunched_is_atomic_and_joins_caller_rollback(tmp_path):
    _service, _backend, agent = _make_service(tmp_path)
    own = _opened(agent)
    before = db.conn().execute("SELECT COUNT(*) FROM state_log").fetchone()[0]
    db.conn().execute("BEGIN IMMEDIATE")
    try:
        turn_lifecycle.record_unlaunched(agent, "t", turn_id=own)
        assert _last(agent)[KEY] == own
        assert db.conn().execute("SELECT outcome FROM turns WHERE turn_id=?", (own,)).fetchone()[0] == "failed"
        assert db.conn().in_transaction
    finally:
        db.conn().execute("ROLLBACK")
    assert db.conn().execute("SELECT COUNT(*) FROM state_log").fetchone()[0] == before
    assert db.conn().execute("SELECT settled_at FROM turns WHERE turn_id=?", (own,)).fetchone()[0] is None


def test_preopen_old_callback_cannot_settle_later_actual_provider_attempt(tmp_path, monkeypatch):
    callbacks = []
    service, backend, agent = _make_service(tmp_path,
        retry_scheduler=lambda delay, fn: callbacks.append(fn))
    backend.live = False
    real_open = turn_lifecycle.open_turn
    real_record = turn_lifecycle.record_unlaunched
    failures = {"open": 1, "marker": 1}
    def fail_open_once(*args, **kwargs):
        if failures["open"]:
            failures["open"] -= 1
            raise sqlite3.OperationalError("synthetic open write failure")
        return real_open(*args, **kwargs)
    def fail_marker_once(*args, **kwargs):
        if failures["marker"]:
            failures["marker"] -= 1
            raise sqlite3.OperationalError("synthetic marker write failure")
        return real_record(*args, **kwargs)
    monkeypatch.setattr(turn_lifecycle, "open_turn", fail_open_once)
    monkeypatch.setattr(turn_lifecycle, "record_unlaunched", fail_marker_once)
    with pytest.raises(sqlite3.OperationalError, match="open write failure"):
        service.dispatch(text="synthetic", requested_session="mike", trace_id="t",
                         client_msg_id="q", queue_if_busy=True, synthesize_audio=False)
    assert len(callbacks) == 1 and backend.spawned == []
    from lib import launch_receipts
    assert launch_receipts.get("t") is None
    assert not db.conn().execute("SELECT 1 FROM turns WHERE agent_id=?", (agent,)).fetchone()
    old_callback = callbacks.pop()
    error = sqlite3.OperationalError("synthetic postspawn start write failure")
    def lost_start(_queue_id):
        raise error
    monkeypatch.setattr(turn_queue, "mark_started", lost_start)
    assert service.recover_queued() == 0
    assert len(backend.spawned) == 1
    later = db.conn().execute("SELECT * FROM turns WHERE agent_id=?", (agent,)).fetchone()
    assert later["settled_at"] is None and turn_queue.status("q") in ("queued", "claimed")
    old_callback()
    assert KEY not in _last(agent)
    assert db.conn().execute("SELECT settled_at FROM turns WHERE turn_id=?", (later["turn_id"],)).fetchone()[0] is None
    assert turn_queue.pending_for_dispatch() == []
    assert agents.trace_launch_status(agent, "t") != "retryable"
    # Reach the actual broad-catch parked branch with the SAME post-spawn failure.
    row = dict(db.conn().execute("SELECT * FROM queued_turns WHERE queue_id='q'").fetchone())
    before = db.conn().execute("SELECT MAX(state_id) FROM state_log").fetchone()[0]
    for _ in range(recovery_backoff.PARK_AFTER):
        service._recovery_failed(row, error)
    assert not db.conn().execute("SELECT 1 FROM state_log WHERE state_id>?", (before,)).fetchone()
    assert len(backend.spawned) == 1


@pytest.mark.parametrize("kind,negative", [("interrupted", True), ("thinking", True), ("interrupted", False)])
def test_unlaunched_whole_event_equals_raw_6ad_O_with_attempt_key_stored(kind, negative):
    agent = agents.create_agent(persona="Mike", voice_id="V", cwd="/tmp", session="mike")
    original_O = {"trace_id": "t", "message": "synthetic retry"}
    if negative:
        original_O["dispatch_not_started"] = True
    stored = {**original_O, KEY: 17}
    stream = FakeStream()
    watcher = StateLogWatcher(stream)
    def emit(detail):
        db.conn().execute("INSERT INTO state_log(agent_id,kind,ts,detail) VALUES (?,?,123,?)",
                          (agent, kind, json.dumps(detail)))
        watcher._poll_once()
        result = json.dumps(next(e for e in stream.events if e["type"] == SSEType.AGENT_STATE)).encode()
        stream.events.clear()
        return result
    expected = emit(original_O if (kind, negative) == ("interrupted", True) else stored)
    assert emit(stored) == expected
    assert _last(agent) == stored


def test_keyless_with_attempted_receipt_is_protected_not_consumed(tmp_path):
    from lib import launch_receipts
    _service, _backend, agent = _make_service(tmp_path)
    own = _opened(agent)
    turn_queue.enqueue(queue_id="q", agent_id=agent, session="mike", text="synthetic",
                       trace_id="t", client_msg_id="q", synthesize_audio=False,
                       origin="user", sender_agent_id="")
    launch_receipts.record_pending(agent_id=agent, trace_id="t", client_msg_id="q", origin="user")
    assert launch_receipts.mark_attempted("t") == "attempted"
    turn_lifecycle.record_unlaunched(agent, "t")
    assert KEY not in _last(agent)
    assert db.conn().execute("SELECT settled_at FROM turns WHERE turn_id=?", (own,)).fetchone()[0] is None
    before = dict(db.conn().execute("SELECT * FROM queued_turns WHERE queue_id='q'").fetchone())
    turn_queue.reconcile_receipts()
    assert dict(db.conn().execute("SELECT * FROM queued_turns WHERE queue_id='q'").fetchone()) == before
    assert agents.trace_launch_status(agent, "t", client_msg_id="q", origin="user", sender_agent_id="") != "retryable"
    assert turn_queue.pending_for_dispatch() == []
    assert not turn_queue.claim_for_dispatch("q") and turn_queue.claim("q") is None


def test_late_backdated_marker_retained_after_pruning_cannot_revive_later_turn(tmp_path):
    from lib import maintenance
    _service, _backend, agent = _make_service(tmp_path)
    old = _opened(agent)
    turn_lifecycle.record_unlaunched(agent, "t", turn_id=old)
    later = _opened(agent)
    turn_lifecycle.record_unlaunched(agent, "t", turn_id=old)
    assert KEY not in _last(agent)
    maintenance._prune_state_log(db.conn(), db.now_ms() + 1)
    assert db.conn().execute("SELECT COUNT(*) FROM state_log WHERE agent_id=?", (agent,)).fetchone()[0] == 1
    assert KEY not in _last(agent)
    assert db.conn().execute("SELECT settled_at FROM turns WHERE turn_id=?", (later,)).fetchone()[0] is None
    assert agents.trace_launch_status(agent, "t") != "retryable"
    turn_queue.enqueue(queue_id="q", agent_id=agent, session="mike", text="synthetic",
                       trace_id="t", client_msg_id="q", synthesize_audio=False,
                       origin="agent", sender_agent_id="")
    before = dict(db.conn().execute("SELECT * FROM queued_turns WHERE queue_id='q'").fetchone())
    turn_queue.reconcile_receipts()
    assert dict(db.conn().execute("SELECT * FROM queued_turns WHERE queue_id='q'").fetchone()) == before
    assert turn_queue.pending_for_dispatch() == []


def test_recovery_snapshot_cannot_replay_receipt_completed_by_manual_delivery(tmp_path, monkeypatch):
    service, backend, agent = _make_service(tmp_path)
    backend.live = False
    turn_queue.enqueue(queue_id="q", agent_id=agent, session="mike", text="synthetic",
                       trace_id="t", client_msg_id="q", synthesize_audio=False,
                       origin="agent", sender_agent_id="")
    pending = turn_queue.pending_for_dispatch
    entered = []
    def stale_snapshot():
        rows = pending()
        if not entered:
            entered.append(True)
            service.dispatch_queued("q")
            assert len(backend.spawned) == 1
            backend.spawned[0][1]["on_result"]({"duration_ms": 1})
            assert turn_queue.status("q") == "done"
        return rows
    monkeypatch.setattr(turn_queue, "pending_for_dispatch", stale_snapshot)
    assert service.recover_queued() == 0
    assert len(backend.spawned) == 1
    assert db.conn().execute("SELECT COUNT(*) FROM turns WHERE agent_id=? AND trace_id='t'", (agent,)).fetchone()[0] == 1
