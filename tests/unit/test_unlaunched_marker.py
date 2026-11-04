"""CLIENT-SEND-500 (server part): a spawn that fails before the backend got
the work answers 500 and marks the attempt 'never started', which is what lets
the client's same-id resend launch the turn exactly once. If that marker write
itself failed (a locked database), the resend got 503 'unknown' for ever and
the saved message never got its turn."""
from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace

import pytest

from lib import agents, backends as backends_module, live_hub, turn_lifecycle
from lib.turn_dispatch import DispatchError
import lib.turn_dispatch as _td
from tests.unit.test_turn_dispatch import (  # noqa: F401 - autouse fixture
    _assert_retry_delivers_once, _codex_service, _CodexSteerable, _local_dispatch_runtime)


class _SpawnDiesOnce(_CodexSteerable):
    def __init__(self):
        super().__init__()
        self.failures_left = 1

    def spawn_turn(self, backend, **kwargs):
        if self.failures_left:
            self.failures_left -= 1
            raise RuntimeError("codex app-server unreachable")
        super().spawn_turn(backend, **kwargs)


def test_a_marker_write_that_fails_once_still_lets_the_resend_launch_once(tmp_path, monkeypatch):
    real = turn_lifecycle.record_unlaunched
    calls = {"n": 0}

    def locked_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)
    monkeypatch.setattr(turn_lifecycle, "record_unlaunched", locked_once)
    backends = _SpawnDiesOnce()
    service, agent_id = _codex_service(tmp_path, backends)
    service.retry_scheduler = lambda _delay, fn: fn()   # the background retry, run now
    with pytest.raises(DispatchError):
        service.dispatch(text="full control over the pipeline",
                         requested_session="cipher", trace_id="t-died",
                         client_msg_id="u-lost")
    assert backends.spawned == []
    assert _td._INFLIGHT.get(agent_id) is None
    _assert_retry_delivers_once(service, backends, agent_id)


def test_a_marker_that_landed_before_its_error_is_not_written_again(tmp_path, monkeypatch):
    """Reviewer B's control on 5c094657: the marker's INSERT can commit and the
    split Host's observation (starting its live-forward thread) fail after it.
    The retry must find the marker, not write a second one."""
    original = live_hub.observe_transition
    original_start = threading.Thread.start

    def no_forward_worker(self):
        if self.name == "live-forward":
            raise RuntimeError("synthetic thread creation failure")
        return original_start(self)

    def observe(agent_id, event, target, detail):
        if event != turn_lifecycle.TurnEvent.UNLAUNCHED:
            return original(agent_id, event, target, detail)
        with monkeypatch.context() as failure:
            failure.setattr(live_hub, "_HUB", None)
            failure.setattr(backends_module, "_RUNTIME_CLIENT",
                            SimpleNamespace(live_observe=lambda *a: None))
            failure.setattr(threading.Thread, "start", no_forward_worker)
            return original(agent_id, event, target, detail)
    monkeypatch.setattr(live_hub, "observe_transition", observe)
    provider = _SpawnDiesOnce()
    service, agent_id = _codex_service(tmp_path, provider)
    service.retry_scheduler = lambda _delay, fn: fn()
    with pytest.raises(DispatchError):
        service.dispatch(text="synthetic request", requested_session="cipher",
                         trace_id="t-died", client_msg_id="u-lost")
    rows = agents.conn().execute(
        "SELECT COUNT(*) AS n FROM state_log WHERE agent_id=? AND json_extract(detail,'$.trace_id')='t-died' "
        "AND json_extract(detail,'$.dispatch_not_started')=1", (agent_id,)).fetchone()["n"]
    assert rows == 1
    _assert_retry_delivers_once(service, provider, agent_id)



def test_a_failed_retry_scheduling_keeps_one_marker_and_the_dispatch_failure(tmp_path, monkeypatch):
    """Reviewer B on ca75ed42: the same resource exhaustion that failed the
    observation's thread can fail the retry's timer. That must stay inside the
    abandonment: no second abandonment, no second marker, the original error."""
    original = live_hub.observe_transition
    original_start = threading.Thread.start

    def no_forward_worker(self):
        if self.name == "live-forward":
            raise RuntimeError("synthetic thread creation failure")
        return original_start(self)

    def observe(agent_id, event, target, detail):
        if event != turn_lifecycle.TurnEvent.UNLAUNCHED:
            return original(agent_id, event, target, detail)
        with monkeypatch.context() as failure:
            failure.setattr(live_hub, "_HUB", None)
            failure.setattr(backends_module, "_RUNTIME_CLIENT",
                            SimpleNamespace(live_observe=lambda *a: None))
            failure.setattr(threading.Thread, "start", no_forward_worker)
            return original(agent_id, event, target, detail)
    monkeypatch.setattr(live_hub, "observe_transition", observe)
    provider = _SpawnDiesOnce()
    service, agent_id = _codex_service(tmp_path, provider)

    def no_timer(_delay, _fn):
        raise RuntimeError("synthetic timer thread creation failure")
    service.retry_scheduler = no_timer
    with pytest.raises(DispatchError):
        service.dispatch(text="synthetic request", requested_session="cipher",
                         trace_id="t-died", client_msg_id="u-lost")
    rows = agents.conn().execute(
        "SELECT COUNT(*) AS n FROM state_log WHERE agent_id=? AND json_extract(detail,'$.trace_id')='t-died' "
        "AND json_extract(detail,'$.dispatch_not_started')=1", (agent_id,)).fetchone()["n"]
    assert rows == 1
    service.retry_scheduler = lambda _delay, fn: None
    _assert_retry_delivers_once(service, provider, agent_id)


def test_a_failed_marker_adds_no_database_write_before_the_500(tmp_path, monkeypatch):
    """Reviewer B on ca75ed42: as before the fix, a failed marker skips the
    turn's close, so a locked database costs no second critical-write wait on
    the way to the 500; the marker is retried in the background only."""
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(turn_lifecycle, "record_unlaunched", locked)
    closed = []
    monkeypatch.setattr(turn_lifecycle, "close_turn", lambda turn_id: closed.append(turn_id))
    backends = _SpawnDiesOnce()
    service, _agent_id = _codex_service(tmp_path, backends)
    scheduled = []
    service.retry_scheduler = lambda delay, fn: scheduled.append(delay)
    with pytest.raises(DispatchError):
        service.dispatch(text="full control over the pipeline",
                         requested_session="cipher", trace_id="t-died",
                         client_msg_id="u-lost")
    assert closed == []
    assert scheduled == [_td._UNLAUNCHED_MARKER_RETRY_SEC[0]]
