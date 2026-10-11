"""The natural release drain (Peter via Solu rda0af9c6fdc2): by default the
runtime never fences admissions while anything blocks the handoff. It checks
every 30 s for a moment with zero blockers, and only then fences, re-checks
atomically and seals, or lowers the fence at once. A fence lasts only for the
seal check; nothing counts attempts, budget or backoff. ``fenced`` keeps
today's behaviour as an opt-in. CLARP_RELEASE_DRAIN=natural outranks config."""
from __future__ import annotations

from dataclasses import replace

import pytest

from lib import config, turn_queue
from lib import turn_dispatch as td
from lib.runtime_release import (NATURAL_CHECK_SEC, RuntimeReleaseMonitor,
                                 release_drain_mode)

from test_graceful_release_drain import (  # noqa: F401 - autouse fixture
    Clock, _agent, _fresh_runtime_memory, _runtime, _send, _service)
from test_runtime_release import FakeRuntime
from test_drain_recovery_transfer import selector_configured  # noqa: F401


def _mode(monkeypatch, value):
    cfg = replace(config.load(), runtime_release_drain=value)
    monkeypatch.setattr(config, "load", lambda *args, **kwargs: cfg)


class Natural(FakeRuntime):
    """FakeRuntime with the natural drain's two calls."""

    def __init__(self, blockers_seq, seal=None, **kwargs):
        super().__init__(**kwargs)
        self.pre = list(blockers_seq)
        self.seal_results = list(seal or [{}])

    def handover_blockers(self):
        self.calls.append("blockers?")
        return self.pre.pop(0) if len(self.pre) > 1 else self.pre[0]

    def seal_now(self):
        self.calls.append("seal-now")
        result = (self.seal_results.pop(0) if len(self.seal_results) > 1
                  else self.seal_results[0])
        return result


def _monitor(runtime, clock, desired=None, mode=lambda: "natural"):
    desired = desired or ["new"]
    return RuntimeReleaseMonitor(
        runtime, running_release_id="old", desired_release_id=lambda: desired[0],
        clock=clock, wall=lambda: 1_700_000_000.0 + clock.now,
        budget_sec=900, backoff_base_sec=300, backoff_cap_sec=3600, mode=mode)


# --- the mode switch ----------------------------------------------------------------

def test_natural_is_the_default(monkeypatch):
    monkeypatch.delenv("CLARP_RELEASE_DRAIN", raising=False)
    assert release_drain_mode() == "natural"


def test_config_opts_into_fenced_and_the_env_kill_switch_outranks_it(monkeypatch):
    _mode(monkeypatch, "fenced")
    monkeypatch.delenv("CLARP_RELEASE_DRAIN", raising=False)
    assert release_drain_mode() == "fenced"
    monkeypatch.setenv("CLARP_RELEASE_DRAIN", "natural")
    assert release_drain_mode() == "natural"


def test_an_unknown_mode_is_natural(monkeypatch):
    _mode(monkeypatch, "sometimes")
    monkeypatch.delenv("CLARP_RELEASE_DRAIN", raising=False)
    assert release_drain_mode() == "natural"


# --- the monitor -------------------------------------------------------------------

def test_natural_never_fences_while_blockers_exist():
    clock = Clock()
    runtime = Natural([{"active": ["a"]}])
    monitor = _monitor(runtime, clock)
    for _ in range(40):
        monitor.check_once()
        clock.now += NATURAL_CHECK_SEC
    assert "fence" not in runtime.calls and "seal-now" not in runtime.calls
    status = monitor.status()
    assert status["mode"] == "natural"
    assert status["phase"] == "waiting_idle"
    assert status["blockers"] == {"active": ["a"]}
    assert status["attempt"] == 0 and status["deadline"] is None


def test_natural_checks_on_its_interval_not_every_poll():
    clock = Clock()
    runtime = Natural([{"active": ["a"]}])
    monitor = _monitor(runtime, clock)
    for _ in range(10):
        monitor.check_once()
        clock.now += 1
    assert runtime.calls.count("blockers?") == 1
    clock.now += NATURAL_CHECK_SEC
    monitor.check_once()
    assert runtime.calls.count("blockers?") == 2


def test_natural_seals_at_the_first_zero_blocker_moment():
    clock = Clock()
    runtime = Natural([{"active": ["a"]}, {}], seal=[{}])
    monitor = _monitor(runtime, clock)
    assert monitor.check_once() is False
    clock.now += NATURAL_CHECK_SEC
    assert monitor.check_once() is True
    assert runtime.calls[-2:] == ["seal-now", "shutdown"]
    assert monitor.status()["last_event"]["kind"] == "sealed"


def test_natural_lowers_at_once_when_the_seal_check_finds_something():
    clock = Clock()
    runtime = Natural([{}], seal=[{"active": ["arrived"]}, {}])
    monitor = _monitor(runtime, clock)
    assert monitor.check_once() is False
    status = monitor.status()
    assert status["phase"] == "waiting_idle"     # no backoff, no attempt
    assert status["attempt"] == 0
    assert status["last_event"]["kind"] == "seal_missed"
    assert status["blockers"] == {"active": ["arrived"]}
    clock.now += NATURAL_CHECK_SEC
    assert monitor.check_once() is True


def test_an_idle_runtime_still_hands_over_at_once_in_natural():
    clock = Clock()
    runtime = Natural([{}])
    runtime.idle_results = iter([True])
    assert _monitor(runtime, clock).check_once() is True
    assert runtime.calls == ["idle?", "shutdown"]


def test_switching_live_from_fenced_to_natural_lifts_the_fence_at_once():
    clock, mode = Clock(), ["fenced"]
    runtime = Natural([{"active": ["a"]}], blockers=[{"active": ["a"]}])
    monitor = _monitor(runtime, clock, mode=lambda: mode[0])
    monitor.check_once()
    assert runtime.fenced is True and monitor.status()["phase"] == "draining"
    mode[0] = "natural"
    clock.now += 1
    monitor.check_once()
    assert runtime.fenced is False
    assert monitor.status()["phase"] == "waiting_idle"
    assert monitor.status()["mode"] == "natural"


def test_switching_live_from_natural_to_fenced_starts_a_fenced_attempt():
    clock, mode = Clock(), ["natural"]
    runtime = Natural([{"active": ["a"]}], blockers=[{"active": ["a"]}])
    monitor = _monitor(runtime, clock, mode=lambda: mode[0])
    monitor.check_once()
    assert runtime.fenced is False
    mode[0] = "fenced"
    clock.now += 1
    monitor.check_once()
    assert runtime.fenced is True
    assert monitor.status()["phase"] == "draining"
    assert monitor.status()["attempt"] == 1


def test_fenced_mode_is_unchanged():
    clock = Clock()
    runtime = Natural([{}], blockers=[{"active": ["hung"]}])
    monitor = _monitor(runtime, clock, mode=lambda: "fenced")
    monitor.check_once()
    assert runtime.fenced is True
    clock.now += 900
    monitor.check_once()
    assert monitor.status()["phase"] == "backoff"
    assert "seal-now" not in runtime.calls


# --- end to end: real dispatcher, queue and runtime ---------------------------------

def _natural_runtime(tmp_path, service):
    runtime, _ = _runtime(tmp_path, service)
    return runtime


def test_natural_never_holds_a_user_message_while_a_turn_runs(tmp_path):
    agent = _agent(tmp_path, "mike")
    other = _agent(tmp_path, "nadia")
    service, provider = _service(tmp_path)
    runtime = _natural_runtime(tmp_path, service)
    clock = Clock()
    monitor = _monitor(runtime, clock)
    try:
        _send(service, "long")                       # a long turn: a blocker
        for _ in range(5):
            monitor.check_once()
            clock.now += NATURAL_CHECK_SEC
            service.dispatch(text=f"hi {clock.now}", requested_session="nadia",
                             trace_id=f"t-{clock.now}", client_msg_id=f"hi {clock.now}",
                             synthesize_audio=False)
            provider.finish(f"hi {clock.now}")
        # Every message to the idle agent started at once; none was held.
        assert len([t for t in provider.texts() if t.startswith("hi")]) == 5
        assert td._SLOTS.fenced is False
        assert td._SLOTS.held() == {}
        assert monitor.status()["phase"] == "waiting_idle"
    finally:
        runtime.server_close()


def test_natural_seals_once_the_runtime_is_idle(tmp_path):
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    runtime = _natural_runtime(tmp_path, service)
    shut = []
    runtime.shutdown = lambda: shut.append(True)
    clock = Clock()
    monitor = _monitor(runtime, clock)
    try:
        _send(service, "long")
        monitor.check_once()
        assert shut == []
        provider.finish("long")
        clock.now += NATURAL_CHECK_SEC
        assert monitor.check_once() is True
        assert runtime._draining is True and shut == [True]
    finally:
        runtime.server_close()


def test_an_arrival_during_the_seal_check_lowers_the_fence_and_runs(tmp_path, monkeypatch):
    """Something appears while the fence is up for the seal check (a shared
    provider turn starts and a message arrives inside the process): the
    fence comes down at once and the message runs, nothing lost."""
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    runtime = _natural_runtime(tmp_path, service)
    clock = Clock()
    monitor = _monitor(runtime, clock)
    original = service.persist_held
    arrived = []

    def arrival_during_the_check(*args, **kwargs):
        if not arrived:
            arrived.append(True)
            monkeypatch.setattr(td, "provider_turns", lambda: ["codex-agent"])
            _send(service, "during the seal check")      # held by the fence
            assert provider.texts() == []
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "persist_held", arrival_during_the_check)
    # Not trivially idle (the fast path would hand over without a check).
    monkeypatch.setattr(runtime, "begin_drain_if_idle", lambda: False)
    try:
        assert monitor.check_once() is False
        assert runtime._draining is False
        assert td._SLOTS.fenced is False
        assert provider.texts() == ["during the seal check"]
        assert turn_queue.parked(agent) == []
        assert monitor.status()["phase"] == "waiting_idle"
    finally:
        runtime.server_close()


def test_a_rollback_during_the_natural_seal_unseals_and_keeps_waiting(tmp_path):
    _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    runtime = _natural_runtime(tmp_path, service)
    shut = []
    runtime.shutdown = lambda: shut.append(True)
    reads = iter(["new", "old", "old"])
    monitor = RuntimeReleaseMonitor(
        runtime, running_release_id="old", desired_release_id=lambda: next(reads),
        clock=Clock(), mode=lambda: "natural")
    runtime.begin_drain_if_idle = lambda: False
    try:
        assert monitor.check_once() is False   # sealed, then the re-read says old
        assert runtime._draining is False and td._SLOTS.fenced is False
        assert shut == []
    finally:
        runtime.server_close()


# --- with item 1: account-recovery parks --------------------------------------------

def _parked_natural(tmp_path, monkeypatch):
    from test_drain_recovery_transfer import Pool, _bound_agent
    from test_drain_recovery_transfer import _runtime as carry_runtime
    pool = Pool(monkeypatch)
    agent = _bound_agent(tmp_path)
    service, provider = _service(tmp_path)
    pool.coordinator.recovering = True
    pool.coordinator.parked_since = 40.0
    pool.coordinator.command = ("selector",)
    _send(service, "work")
    assert provider.spawned == [] and pool.coordinator.status()["waiting"] == [agent]
    runtime = carry_runtime(tmp_path, service)
    return agent, service, provider, pool, runtime


def test_natural_hands_a_certain_park_over_at_its_seal(tmp_path, monkeypatch, selector_configured):
    agent, service, provider, pool, runtime = _parked_natural(tmp_path, monkeypatch)
    shut = []
    runtime.shutdown = lambda: shut.append(True)
    try:
        assert _monitor(runtime, Clock()).check_once() is True
        assert shut == [True]
        assert [r["queue_id"] for r in turn_queue.parked(agent)] == ["recovery-park-t-work"]
    finally:
        runtime.server_close()


def test_natural_does_not_refence_for_the_same_refused_parks(tmp_path, monkeypatch,
                                                             selector_configured):
    agent, service, provider, pool, runtime = _parked_natural(tmp_path, monkeypatch)
    turn_queue.set_paused(agent, True)                 # refused: queue_paused
    fences = []
    original = service.begin_admission_fence
    monkeypatch.setattr(service, "begin_admission_fence",
                        lambda: (fences.append(1), original())[1])
    clock = Clock()
    monitor = _monitor(runtime, clock)
    try:
        for _ in range(5):
            monitor.check_once()
            clock.now += NATURAL_CHECK_SEC
        assert len(fences) == 1                        # one brief check only
        assert td._SLOTS.fenced is False
        assert monitor.status()["phase"] == "waiting_idle"
    finally:
        runtime.server_close()
