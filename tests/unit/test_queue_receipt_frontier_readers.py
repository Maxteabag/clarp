"""Administrative transfer proof is queue-local; public state stays unchanged."""
from __future__ import annotations

import json

import pytest

from lib import agents, db, turn_lifecycle
from lib.protocol import SSEType
from lib.state_watcher import StateLogWatcher
from lib.turn_command import _unlaunched_marked
from tests.unit.test_state_watcher import FakeStream
from tests.unit.test_turn_dispatch import _make_service


F = "dispatch_not_started_through_turn_id"


def _handoff(agent, *, frontier=True):
    turn = turn_lifecycle.open_turn(
        agent_id=agent, source="pwa", trace_id="t", synthesize_audio=False)
    turn_lifecycle.close_turn(turn)
    detail = {"trace_id": "t", "reason": "update_handoff",
              "message": "synthetic hold", "dispatch_not_started": True}
    if frontier:
        detail[F] = turn
    agents.record_state(agent, "idle", detail)
    return turn


@pytest.mark.parametrize("frontier", [False, True])
@pytest.mark.parametrize("later", [None, "open", "settled"])
def test_handoff_never_grants_global_retry_or_substitutes_for_failure_marker(
        tmp_path, frontier, later):
    _service, _backend, agent = _make_service(tmp_path)
    _handoff(agent, frontier=frontier)
    if later:
        turn = turn_lifecycle.open_turn(
            agent_id=agent, source="pwa", trace_id="t", synthesize_audio=False)
        if later == "settled":
            db.conn().execute(
                "UPDATE turns SET ended_at=1,settled_at=1,outcome='completed' WHERE turn_id=?",
                (turn,))
    assert agents.trace_launch_status(agent, "t") != "retryable"
    assert not _unlaunched_marked(agent, "t")


@pytest.mark.parametrize("later_attempt", [False, True])
def test_actual_marker_retry_records_fresh_failure_despite_historical_handoff(tmp_path, monkeypatch, later_attempt):
    from lib import backends
    callbacks = []
    service, backend, agent = _make_service(
        tmp_path, retry_scheduler=lambda delay, fn: callbacks.append(fn))
    _handoff(agent)
    real_record = turn_lifecycle.record_unlaunched
    calls = []
    def fail_first_marker(*args, **kwargs):
        calls.append(kwargs.get("turn_id"))
        if len(calls) == 1:
            raise OSError("synthetic marker write failure")
        return real_record(*args, **kwargs)
    def preinvoke(*args, **kwargs):
        raise OSError("synthetic source-marker preparation failure")
    monkeypatch.setattr(turn_lifecycle, "record_unlaunched", fail_first_marker)
    monkeypatch.setattr(backends.by_id("claude"), "arm_source_marker", preinvoke)
    with pytest.raises(OSError, match="source-marker preparation"):
        service.dispatch(text="synthetic", requested_session="mike", trace_id="t",
                         client_msg_id="retry", synthesize_audio=False)
    assert backend.spawned == [] and len(callbacks) == 1
    newer = (turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="t", synthesize_audio=False)
             if later_attempt else None)
    before = db.conn().execute("SELECT MAX(state_id) FROM state_log").fetchone()[0]
    callbacks[0]()
    rows = db.conn().execute(
        "SELECT detail FROM state_log WHERE agent_id=? AND state_id>?", (agent, before)).fetchall()
    assert len(rows) == 1
    fresh = json.loads(rows[0]["detail"])
    assert fresh["dispatch_not_started"] is True and fresh["trace_id"] == "t"
    assert calls[0] == calls[1]
    if later_attempt:
        assert "dispatch_not_started_turn_id" not in fresh
        assert db.conn().execute("SELECT settled_at FROM turns WHERE turn_id=?", (newer,)).fetchone()[0] is None
        assert db.conn().execute("SELECT settled_at FROM turns WHERE turn_id=?", (calls[0],)).fetchone()[0] is None
        assert agents.trace_launch_status(agent, "t") != "retryable"
    else:
        assert fresh["dispatch_not_started_turn_id"] == calls[0]
    assert fresh.get("reason") != "update_handoff"
    # record_unlaunched backdates ts: insertion, not latest timestamp, is the oracle.
    assert _unlaunched_marked(agent, "t")


def test_genuine_nonhandoff_marker_retains_its_existing_historical_semantics(tmp_path):
    _service, _backend, agent = _make_service(tmp_path)
    own = turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="t", synthesize_audio=False)
    turn_lifecycle.record_unlaunched(agent, "t", turn_id=own)
    assert agents.trace_launch_status(agent, "t") == "retryable"
    assert _unlaunched_marked(agent, "t")
    _handoff(agent)
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="t", synthesize_audio=False)
    assert _unlaunched_marked(agent, "t")  # independently valid NON-handoff marker
    assert agents.trace_launch_status(agent, "t") != "retryable"


@pytest.mark.parametrize("kind,reason", [
    ("idle", "update_handoff"), ("idle", "launch_failure"), ("thinking", "update_handoff")])
def test_whole_handoff_sse_matches_raw_6ad_event_O_while_storage_keeps_proof(kind, reason):
    agent = agents.create_agent(persona="Mike", voice_id="V", cwd="/tmp", session="mike")
    baseline_O = {"reason": reason, "trace_id": "t", "message": "synthetic hold"}
    stored = {**baseline_O, "dispatch_not_started": True, F: 17}
    stream = FakeStream()
    watcher = StateLogWatcher(stream)
    def write(detail):
        db.conn().execute(
            "INSERT INTO state_log(agent_id,kind,ts,detail) VALUES (?,?,123,?)",
            (agent, kind, json.dumps(detail)))
        watcher._poll_once()
        event = next(e for e in stream.events if e["type"] == SSEType.AGENT_STATE)
        stream.events.clear()
        return json.dumps(event).encode()  # AudioStream's whole-event wire encoding
    expected = write(baseline_O if (kind, reason) == ("idle", "update_handoff") else stored)
    assert write(stored) == expected
    raw = db.conn().execute(
        "SELECT detail FROM state_log WHERE agent_id=? ORDER BY state_id DESC LIMIT 1",
        (agent,)).fetchone()[0]
    assert json.loads(raw) == stored
