"""BACKENDS-06 (c) and 07b (stage 1), on the shared Codex app-server client.

06: a usage-limit failure from a client still running credentials older than
auth.json (the switch waits because another agent is busy) is a pending
switch, not a reason for another account failover.
07b: a stopped turn the server never ends is detached after the stop deadline
(it no longer counts as busy); the shared process is recycled once no live
turn runs on it; until then the stopped agent's next turn is refused with a
visible reason, since its thread is still held by the stuck process."""
from __future__ import annotations

from tests.codex_request import result_reply

import threading
from unittest.mock import Mock

import pytest

from lib import codex_app_server as cas


def _client(fingerprint=b"old"):
    client = object.__new__(cas._Client)
    client.agent_id, client.active, client.thread_id = "", None, "synthetic-thread"
    client._actives, client._loaded_threads, client.rate_limits = {}, set(), {}
    client._lock = threading.Lock()
    client.proc = Mock()
    client.proc.poll.return_value = None
    client.auth_fingerprint = fingerprint
    client.refresh_requested = False
    client.request = result_reply(lambda method, params, timeout=30: {})
    return client


def _turn(client, agent_id, turn_id):
    handle = cas.AppTurnHandle(client, agent_id=agent_id)
    client._actives[agent_id] = cas._ActiveTurn(
        turn_id, "synthetic-thread", agent_id, "synthetic", f"trace-{turn_id}",
        cas.TurnState(live_backend_session_id="synthetic-thread"), handle, None, None, None,
        lambda *a, **k: 0)
    return handle


def test_a_failure_on_a_client_with_older_credentials_is_a_pending_switch(monkeypatch):
    client = _client(b"old")
    monkeypatch.setattr(cas, "_auth_fingerprint", lambda: b"new")
    failure = cas.CodexTurnFailure("usage limit", client, False)
    assert cas.credential_switch_pending(failure) is True
    monkeypatch.setattr(cas, "_auth_fingerprint", lambda: b"old")
    assert cas.credential_switch_pending(failure) is False
    assert cas.credential_switch_pending("usage limit") is False


@pytest.fixture
def shared(monkeypatch):
    client = _client()
    monkeypatch.setattr(cas, "_CLIENTS", {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, "STOP_DETACH_SEC", 0.2, raising=False)
    return client


def _wait(predicate, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        time.sleep(0.02)
    return predicate()


def test_a_stuck_stopped_turn_is_detached_and_the_quiet_process_recycled(shared):
    stuck = _turn(shared, "stopped-agent", "turn-stuck")
    stuck.terminate()                                  # the server never ends it
    assert _wait(lambda: not stuck.is_alive()), "the stuck turn was never detached"
    assert not cas._busy(shared)
    assert _wait(lambda: shared.proc.kill.called), "the quiet stuck process was not recycled"
    assert cas._SHARED_KEY not in cas._CLIENTS


def test_the_stuck_process_waits_for_other_live_turns_and_refuses_the_stopped_agent(shared):
    stuck = _turn(shared, "stopped-agent", "turn-stuck")
    other = _turn(shared, "other-agent", "turn-other")
    stuck.terminate()
    assert _wait(lambda: not stuck.is_alive())
    assert other.is_alive() and not shared.proc.kill.called   # never cut another agent's turn
    with pytest.raises(cas.CodexSessionStuck):
        cas.admit_turn(shared, "stopped-agent")
    cas.admit_turn(shared, "other-agent")                     # the others carry on
    shared._notification("turn/completed", {"threadId": "synthetic-thread",
                                           "turn": {"id": "turn-other", "status": "completed"}})
    assert _wait(lambda: shared.proc.kill.called), "not recycled once the last live turn ended"
