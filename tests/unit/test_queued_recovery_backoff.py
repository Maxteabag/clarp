"""Queued recovery backs off a launch that keeps failing.

Miso (codex), 2026-10-10 18:53-19:01: a broken Codex thread made every
thread/resume fail. Durable queued recovery retried once a second, logging
queuedRecoveryFail 441 times in nine minutes, because a launch that fails
before the backend starts rescheduled recover_queued after a flat 1 s.
"""
import itertools
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import turn_queue
from lib.protocol import AgentState
from lib.turn_dispatch import DispatchError, TurnDispatchService
import lib.turn_dispatch as _td


@pytest.fixture(autouse=True)
def _local_dispatch_runtime(monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    _td.reset_for_tests()
    yield
    _td.reset_for_tests()


class _Backends:
    CLAUDE = "claude"

    def __init__(self):
        self.spawned = []
        self.broken = set()
        self.live = set()

    def normalize(self, backend):
        return backend or self.CLAUDE

    def interrupt(self, backend, agent_id):
        return 0

    def active_handles(self, backend, agent_id):
        return ["handle"] if agent_id in self.live else []

    def spawn_turn(self, backend, **kwargs):
        if kwargs["agent_id"] in self.broken:
            raise RuntimeError("thread/resume: failed to read thread 019a-x")
        self.spawned.append(kwargs)
        self.live.add(kwargs["agent_id"])


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _agent(tmp_path, name):
    agent_id = agents_db.create_agent(
        persona=name.title(), voice_id="V", cwd=str(tmp_path),
        session=name, backend="claude")
    agents_db.start_runtime(agent_id, name)
    return agent_id


def _service(tmp_path):
    backends = _Backends()
    clock = _Clock()
    timers = []
    ids = itertools.count(1)
    ctx = SimpleNamespace(default_session="miso",
                          agents_path=tmp_path / "unused.json",
                          stream=SimpleNamespace(broadcast=lambda _e: None))
    service = TurnDispatchService(
        ctx, backend_registry=backends, home=tmp_path,
        uuid_factory=lambda: f"backend-session-{next(ids)}", now=clock,
        retry_scheduler=lambda delay, fn: timers.append((clock.t + delay, fn)))
    return service, backends, clock, timers


def _send(service, session, trace, *, origin="agent"):
    try:
        return service.dispatch(
            text=f"message {trace}", requested_session=session,
            forced_session=session,
            trace_id=trace, client_msg_id=f"q-{trace}",
            synthesize_audio=False, queue_if_busy=True, origin=origin)
    except DispatchError as exc:
        return None


def _run_until(clock, timers, until):
    """Fire due timers in time order up to ``until``; return the fire times."""
    fired = []
    while True:
        at = min((t for t, _ in timers if t <= until), default=None)
        if at is None:
            break
        for i, (t, fn) in enumerate(timers):
            if t == at:
                timers.pop(i)
                break
        clock.t = max(clock.t, at)
        fired.append(at)
        fn()
    clock.t = until
    return fired


def _count_launches(monkeypatch, backends, clock):
    """Times of every launch attempt for a broken agent."""
    attempts = []
    real = backends.spawn_turn

    def counted(backend, **kwargs):
        if kwargs["agent_id"] in backends.broken:
            attempts.append(clock.t)
        return real(backend, **kwargs)

    monkeypatch.setattr(backends, "spawn_turn", counted)
    return attempts


def _gaps(times):
    return [round(b - a) for a, b in zip(times, times[1:])]


def _broken_miso(tmp_path, monkeypatch):
    service, backends, clock, timers = _service(tmp_path)
    miso = _agent(tmp_path, "miso")
    backends.broken.add(miso)
    attempts = _count_launches(monkeypatch, backends, clock)
    _send(service, "miso", "t1")
    _send(service, "miso", "t2")
    # Each send tried its own launch; count recovery's attempts from here.
    attempts[:] = [clock.t]
    return service, backends, clock, timers, miso, attempts


def test_a_permanently_failing_launch_backs_off_then_parks(tmp_path, monkeypatch):
    service, backends, clock, timers, miso, attempts = _broken_miso(tmp_path, monkeypatch)
    queued_before = turn_queue.pending(miso)

    _run_until(clock, timers, clock.t + 3600)

    # A flat second would be ~3600 launches. The waits double to a 60 s cap,
    # and the eighth identical failure parks recovery.
    assert _gaps(attempts) == [1, 1, 2, 4, 8, 16, 32, 60]
    assert not timers
    # The queued messages are still queued, untouched, in order; nothing
    # paused the queue.
    assert turn_queue.pending(miso) == queued_before
    assert [r["trace_id"] for r in queued_before] == ["t1", "t2"]
    assert not turn_queue.is_paused(miso)
    # The park is visible: an interrupted state naming the failure.
    state = agents_db.latest_state(miso)
    assert state["kind"] == AgentState.INTERRUPTED
    assert state["detail"]["queued_recovery_parked"] is True
    assert "failed to read thread" in state["detail"]["message"]


def test_the_wait_between_attempts_is_capped_at_a_minute(tmp_path, monkeypatch):
    from lib import recovery_backoff
    monkeypatch.setattr(recovery_backoff, "PARK_AFTER", 100)
    service, backends, clock, timers, miso, attempts = _broken_miso(tmp_path, monkeypatch)

    _run_until(clock, timers, clock.t + 600)

    assert _gaps(attempts)[:9] == [1, 1, 2, 4, 8, 16, 32, 60, 60]


def test_one_broken_agent_does_not_delay_the_others(tmp_path, monkeypatch):
    service, backends, clock, timers, miso, attempts = _broken_miso(tmp_path, monkeypatch)
    _run_until(clock, timers, clock.t + 30)
    pebble = _agent(tmp_path, "pebble")
    backends.broken.add(pebble)
    _send(service, "pebble", "p1")
    backends.broken.discard(pebble)
    started = clock.t

    _run_until(clock, timers, clock.t + 2)

    # Pebble's first retry still comes after a second, while Miso is in a
    # long wait.
    assert [kw["trace_id"] for kw in backends.spawned] == ["p1"]
    assert turn_queue.pending_count(pebble) == 0
    assert clock.t - started <= 2


def test_success_after_a_transient_failure_resets_the_backoff(tmp_path, monkeypatch):
    service, backends, clock, timers, miso, attempts = _broken_miso(tmp_path, monkeypatch)
    _run_until(clock, timers, clock.t + 20)
    assert len(attempts) == 6
    backends.broken.discard(miso)

    _run_until(clock, timers, clock.t + 20)

    assert [kw["trace_id"] for kw in backends.spawned] == ["t1"]
    assert _td._RECOVERY_BACKOFF.snapshot() == {}


def test_a_new_send_clears_the_park(tmp_path, monkeypatch):
    service, backends, clock, timers, miso, attempts = _broken_miso(tmp_path, monkeypatch)
    _run_until(clock, timers, clock.t + 3600)
    parked_attempts = len(attempts)
    backends.broken.discard(miso)

    _send(service, "miso", "t3", origin="user")
    _run_until(clock, timers, clock.t + 1)

    assert parked_attempts == 9
    assert miso not in _td._RECOVERY_BACKOFF.snapshot()
    # The new send runs; the queued messages are still queued behind it,
    # in order, and run when it finishes.
    assert [kw["trace_id"] for kw in backends.spawned] == ["t3"]
    assert [r["trace_id"] for r in turn_queue.pending(miso)] == ["t1", "t2"]


def test_automation_does_not_clear_the_park(tmp_path, monkeypatch):
    service, backends, clock, timers, miso, attempts = _broken_miso(tmp_path, monkeypatch)
    _run_until(clock, timers, clock.t + 3600)

    _send(service, "miso", "h1", origin="heartbeat")
    _run_until(clock, timers, clock.t + 600)

    assert _td._RECOVERY_BACKOFF.snapshot()[miso]["parked"] is True


def test_an_explicit_send_of_the_queued_item_clears_the_park(tmp_path, monkeypatch):
    service, backends, clock, timers, miso, attempts = _broken_miso(tmp_path, monkeypatch)
    _run_until(clock, timers, clock.t + 3600)
    backends.broken.discard(miso)
    head = turn_queue.pending(miso)[0]["queue_id"]

    service.dispatch_queued(head)

    assert miso not in _td._RECOVERY_BACKOFF.snapshot()
    assert [kw["trace_id"] for kw in backends.spawned] == ["t1"]
    assert [r["trace_id"] for r in turn_queue.pending(miso)] == ["t2"]
