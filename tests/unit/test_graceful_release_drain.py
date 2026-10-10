"""The graceful release drain, end to end in one process with a fake provider.

The real dispatcher, durable queue, runtime RPC server and release monitor run
together; only the provider (spawn/interrupt) and the clock are fakes. No
process is started, signalled or killed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import live_hub, message_store, turn_queue
from lib import turn_dispatch as td
from lib.runtime_bridge import RuntimeRPCServer
from lib.runtime_release import RuntimeReleaseMonitor
from lib.turn_dispatch import TurnDispatchService


@pytest.fixture(autouse=True)
def _fresh_runtime_memory(monkeypatch):
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    live_hub.install(None)
    yield
    td.reset_for_tests()
    live_hub.install(None)


class Provider:
    """Fake backend registry: records spawns and interrupts, never runs."""
    CLAUDE = "claude"

    def __init__(self, *, steerable=False):
        self.spawned: list[dict] = []
        self.interrupted: list[tuple] = []
        self.steered: list[tuple] = []
        self.live: set[str] = set()
        if steerable:
            self.steer_turn = self._steer

    def normalize(self, backend):
        return backend or self.CLAUDE

    def interrupt(self, backend, agent_id):
        self.interrupted.append((backend, agent_id))
        return 0

    def interrupt_any(self, agent_id):
        self.interrupted.append(("any", agent_id))
        return 0

    def active_handles(self, backend, agent_id):
        return ["handle"] if agent_id in self.live else []

    def spawn_turn(self, backend, **kwargs):
        self.spawned.append(kwargs)
        self.live.add(kwargs["agent_id"])

    def _steer(self, backend, agent_id, text, *, client_msg_id="",
               synthesize_audio=False):
        self.steered.append((agent_id, text))
        return True

    def texts(self):
        return [call["text"] for call in self.spawned]

    def finish(self, text):
        call = next(c for c in self.spawned if c["text"] == text)
        self.live.discard(call["agent_id"])
        call["on_result"]({"result": "done"})


def _agent(tmp_path, session, backend="claude"):
    agent_id = agents_db.create_agent(
        persona=session.title(), voice_id="V", cwd=str(tmp_path),
        session=session, backend=backend)
    agents_db.start_runtime(agent_id, session)
    return agent_id


_SESSIONS = iter(range(10**6))


def _service(tmp_path, provider=None, retries=None):
    provider = provider or Provider()
    ctx = SimpleNamespace(default_session="mike",
                          agents_path=tmp_path / "unused.json",
                          stream=SimpleNamespace(broadcast=lambda _event: None))
    service = TurnDispatchService(
        ctx, backend_registry=provider, home=tmp_path,
        uuid_factory=lambda: f"backend-session-{next(_SESSIONS)}",
        retry_scheduler=(lambda delay, fn: retries.append((delay, fn)))
        if retries is not None else (lambda _delay, _fn: None))
    return service, provider


def _runtime(tmp_path, service):
    runtime = RuntimeRPCServer(tmp_path / "runtime.sock", dispatch_service=service,
                               release_id="old")
    shutdowns = []
    runtime.shutdown = lambda: shutdowns.append("shutdown")
    return runtime, shutdowns


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _monitor(runtime, desired, clock, order=None, **kwargs):
    return RuntimeReleaseMonitor(
        runtime, running_release_id="old", desired_release_id=lambda: desired[0],
        before_shutdown=lambda: (order if order is not None else []).append("handoff"),
        clock=clock, wall=lambda: 1_700_000_000.0 + clock.now - 1000.0,
        budget_sec=900,
        backoff_base_sec=300, backoff_cap_sec=3600, **kwargs)


def _send(service, text, *, session="mike", cid=None, queue=False, origin="user"):
    return service.dispatch(
        text=text, requested_session=session, trace_id=f"t-{cid or text}",
        client_msg_id=cid or text, synthesize_audio=False,
        queue_if_busy=queue, origin=origin)


def _restart(tmp_path):
    """The handoff or a crash: process memory is gone, the database is not.
    The new runtime recovers the durable queue as it does at every boot."""
    td.reset_for_tests()
    service, provider = _service(tmp_path)
    service.recover_queued()
    return service, provider


def _finish_recovered(service, provider, texts):
    """Finish each recovered turn; recovery's scheduled retry (every second in
    production) then admits the agent's next durable row."""
    for text in texts:
        provider.finish(text)
        service.recover_queued()


# --- the fence -----------------------------------------------------------------

def test_in_flight_turn_finishes_while_the_next_one_is_held(tmp_path):
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    _send(service, "running")
    _send(service, "queued next", queue=True)

    service.begin_admission_fence()
    provider.finish("running")

    assert provider.texts() == ["running"]          # nothing new started
    assert provider.interrupted == []               # nothing was cut short
    assert agent not in td._INFLIGHT                # the slot is free...
    assert td._SLOTS.held() == {agent: 1}           # ...and the next turn held
    assert turn_queue.status("queued next") == "queued"


def test_fresh_send_to_an_idle_agent_is_admitted_durably_not_started(tmp_path):
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    service.begin_admission_fence()

    result = _send(service, "hello during drain")

    assert result.queued is True
    assert provider.spawned == []
    [row] = turn_queue.parked(agent)
    assert (row["client_msg_id"], row["text"], row["allow_paused"]) == (
        "hello during drain", "hello during drain", 1)
    # The message is in the conversation (message ids are unique per client id).
    assert message_store.has_client_message("hello during drain")


def test_a_busy_send_behind_the_fence_never_preempts(tmp_path):
    _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    _send(service, "long turn")
    service.begin_admission_fence()

    _send(service, "would have preempted")

    assert provider.interrupted == []
    assert provider.texts() == ["long turn"]


def test_a_codex_follow_up_behind_the_fence_waits_instead_of_steering(tmp_path):
    agent = _agent(tmp_path, "mike", backend="codex")
    service, provider = _service(tmp_path, Provider(steerable=True))
    _send(service, "codex turn")
    service.begin_admission_fence()

    result = _send(service, "follow-up")

    assert provider.steered == []
    assert result.queued is True
    assert td._SLOTS.queue_depth(agent) == 1


def test_a_client_retry_of_a_held_send_is_deduplicated(tmp_path):
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    service.begin_admission_fence()
    _send(service, "once", cid="u-once")

    service.dispatch(text="once", requested_session="mike", trace_id="t-retry",
                     client_msg_id="u-once", synthesize_audio=False)

    assert len(turn_queue.parked(agent)) == 1
    assert td._SLOTS.queue_depth(agent) == 1
    service.lower_admission_fence()
    service.start_held_work()
    assert provider.texts() == ["once"]


# --- arrivals keep their order, through a revert and through a handoff ------------

def _held_mixture(tmp_path):
    """Agent mike: a turn running, then (in arrival order) a normal send that
    queued behind the spawning slot before the drain (memory only), a peer
    message (durable), and two arrivals during the drain."""
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    _send(service, "running")
    td._CLAIMED_AT[agent] = 1.0  # still spawning: a normal send queues in memory
    _send(service, "pre-drain normal")
    td._CLAIMED_AT.pop(agent, None)
    service.dispatch(text="peer note", requested_session="mike", trace_id="t-peer",
                     client_msg_id="peer note", synthesize_audio=False,
                     origin="agent", sender_agent_id="other-agent")
    service.begin_admission_fence()
    _send(service, "user during drain")
    _send(service, "queued during drain", queue=True, origin="automation")
    return agent, service, provider


def test_held_work_starts_in_arrival_order_when_the_fence_is_lifted(tmp_path):
    agent, service, provider = _held_mixture(tmp_path)
    provider.finish("running")
    assert provider.texts() == ["running"]

    service.lower_admission_fence()
    service.start_held_work()
    for text in ("pre-drain normal", "peer note", "user during drain"):
        provider.finish(text)

    assert provider.texts() == ["running", "pre-drain normal", "peer note",
                                "user during drain", "queued during drain"]
    assert td._SLOTS.queue_depth(agent) == 0
    assert turn_queue.parked(agent) == []
    assert turn_queue.pending(agent) == []


def test_held_work_survives_the_handoff_in_arrival_order(tmp_path):
    agent, service, provider = _held_mixture(tmp_path)
    provider.finish("running")

    new_service, new_provider = _restart(tmp_path)
    _finish_recovered(new_service, new_provider,
                      ("pre-drain normal", "peer note", "user during drain"))

    assert new_provider.texts() == ["pre-drain normal", "peer note",
                                    "user during drain", "queued during drain"]
    # Identity is kept: each message is the same row, launched once.
    assert [c["trace_id"] for c in new_provider.spawned] == [
        "t-pre-drain normal", "t-peer", "t-user during drain",
        "t-queued during drain"]
    assert all(message_store.has_client_message(cid) for cid in (
        "pre-drain normal", "peer note", "user during drain", "queued during drain"))


def test_a_crash_mid_drain_neither_loses_nor_sticks_the_fence(tmp_path):
    agent, service, provider = _held_mixture(tmp_path)
    # Crash with the turn still running: memory and the fence are gone.
    new_service, new_provider = _restart(tmp_path)

    assert td._SLOTS.fenced is False   # a new process starts unfenced
    # The open turn's process died with the runtime; the held work is all
    # still durable and recovers in order once the agent is free.
    td._INFLIGHT.pop(agent, None)
    new_service.recover_queued()
    _finish_recovered(new_service, new_provider,
                      ("pre-drain normal", "peer note", "user during drain"))
    assert new_provider.texts() == ["pre-drain normal", "peer note",
                                    "user during drain", "queued during drain"]


# --- pauses, cancels -----------------------------------------------------------------

def test_a_stop_pause_survives_the_drain_and_fresh_intent_still_runs(tmp_path):
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    turn_queue.set_paused(agent, True)
    _send(service, "parked automation", queue=True, origin="automation")
    service.begin_admission_fence()

    _send(service, "user carries on")                       # normal send
    _send(service, "user queue send", queue=True)           # fresh intent

    assert provider.spawned == []
    new_service, new_provider = _restart(tmp_path)
    _finish_recovered(new_service, new_provider, ("user carries on",))
    assert new_provider.texts() == ["user carries on", "user queue send"]
    assert turn_queue.is_paused(agent) is True
    assert turn_queue.status("parked automation") == "queued"


def test_a_held_item_the_user_cancels_never_starts(tmp_path):
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    service.begin_admission_fence()
    _send(service, "keep", queue=True)
    _send(service, "cancel me", queue=True)

    assert turn_queue.cancel("cancel me") is True
    service.lower_admission_fence()
    service.start_held_work()
    provider.finish("keep")

    assert provider.texts() == ["keep"]
    assert td._SLOTS.queue_depth(agent) == 0


def test_stop_during_the_drain_keeps_its_meaning(tmp_path):
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    _send(service, "running")
    service.begin_admission_fence()
    _send(service, "held", queue=True)

    td.clear_for_agent(agent, preserve_queue=True, pause_queue=True)
    td.finish_stop(service.ctx, agent, backend_registry=provider)

    assert turn_queue.is_paused(agent) is True
    assert agent not in td._INFLIGHT
    assert provider.texts() == ["running"]
    assert turn_queue.status("held") == "queued"


# --- what the runtime owns blocks the seal ------------------------------------------

def test_the_seal_waits_for_every_owner_and_ignores_durable_held_work(tmp_path, monkeypatch):
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    runtime, _ = _runtime(tmp_path, service)
    try:
        assert runtime.seal_if_drained() == {"fence": ["not raised"]}
        _send(service, "running")
        service.begin_admission_fence()
        _send(service, "held", queue=True)
        assert runtime.seal_if_drained() == {"active": [agent]}

        provider.finish("running")
        status = dict(td.runtime_status())
        for key, value in (("terminals", [agent]), ("compactions", ["mike"]),
                           ("provider_turns", [agent]),
                           ("claude_account_recovery",
                            {"recovering": True, "waiting": [agent]})):
            monkeypatch.setattr(runtime, "status_provider",
                                lambda key=key, value=value: {**status, key: value})
            assert key in runtime.seal_if_drained()
            assert runtime._draining is False
        monkeypatch.setattr(runtime, "status_provider", lambda: status)
        runtime._stop_leases["lease"] = (agent, {})
        assert runtime.seal_if_drained() == {"stop_leases": [agent]}
        runtime._stop_leases.clear()

        assert runtime.seal_if_drained() == {}
        assert runtime._draining is True
        refused = runtime.dispatch_request("dispatch", {"text": "late"})
        assert refused["status"] == 503      # the brief hard fence, as today
    finally:
        runtime.server_close()


def test_account_recovery_park_holds_the_seal(tmp_path, monkeypatch):
    agent = _agent(tmp_path, "mike")
    service, _provider = _service(tmp_path)
    runtime, _ = _runtime(tmp_path, service)
    coordinator = td._FAILOVERS["claude"]
    monkeypatch.setattr(coordinator, "recovering", True)
    try:
        service.begin_admission_fence()
        assert runtime.seal_if_drained() == {
            "claude_account_recovery": ["<recovering>"]}
        assert runtime._draining is False
    finally:
        runtime.server_close()


def test_unpersisted_held_work_holds_the_seal(tmp_path, monkeypatch):
    agent = _agent(tmp_path, "mike")
    service, _provider = _service(tmp_path)
    runtime, _ = _runtime(tmp_path, service)
    monkeypatch.setattr(turn_queue, "park", lambda **_kw: (_ for _ in ()).throw(
        OSError("disk full")))
    try:
        service.begin_admission_fence()
        _send(service, "cannot persist")
        assert runtime.seal_if_drained() == {"unpersisted": [agent]}
    finally:
        runtime.server_close()


# --- the monitor drives it ---------------------------------------------------------

def test_drain_hands_over_once_when_in_flight_work_finishes(tmp_path):
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    runtime, shutdowns = _runtime(tmp_path, service)
    clock, order, desired = Clock(), [], ["new"]
    monitor = _monitor(runtime, desired, clock, order)
    try:
        _send(service, "running")
        assert monitor.check_once() is False
        assert monitor.status()["phase"] == "draining"
        _send(service, "arrived during drain")
        clock.now += 60
        assert monitor.check_once() is False
        assert monitor.status()["blockers"] == {"active": [agent]}

        provider.finish("running")
        assert monitor.check_once() is True
        assert order == ["handoff"] and shutdowns == ["shutdown"]
        assert provider.texts() == ["running"]
        assert provider.interrupted == []
    finally:
        runtime.server_close()
    new_service, new_provider = _restart(tmp_path)
    assert new_provider.texts() == ["arrived during drain"]


def test_a_hung_turn_hits_the_budget_and_the_fence_reverts_without_killing(tmp_path):
    agent = _agent(tmp_path, "mike")
    other = _agent(tmp_path, "nina")
    service, provider = _service(tmp_path)
    runtime, shutdowns = _runtime(tmp_path, service)
    clock, desired = Clock(), ["new"]
    monitor = _monitor(runtime, desired, clock)
    try:
        _send(service, "hung")
        monitor.check_once()
        _send(service, "please hold", session="nina")
        assert provider.texts() == ["hung"]

        clock.now += 899
        monitor.check_once()
        assert monitor.status()["phase"] == "draining"
        clock.now += 1
        monitor.check_once()

        assert monitor.status()["phase"] == "backoff"
        assert monitor.status()["last_outcome"] == "budget expired"
        assert td._SLOTS.fenced is False
        assert provider.interrupted == []                  # never killed
        assert provider.texts() == ["hung", "please hold"]  # held work started
        assert td._INFLIGHT[agent] == "t-hung"             # still running
        assert shutdowns == []

        # Back off 5 minutes, then 10, then 20... capped at 60.
        clock.now += 299
        monitor.check_once()
        assert monitor.status()["phase"] == "backoff"
        clock.now += 1
        monitor.check_once()
        assert monitor.status()["phase"] == "draining"
        assert monitor.status()["attempt"] == 2
        clock.now += 900
        monitor.check_once()
        wall_ms = int((1_700_000_000 + clock.now - 1000) * 1000)
        assert monitor.status()["next_attempt_at"] == wall_ms + 600_000
        assert other in td._INFLIGHT
    finally:
        runtime.server_close()


def test_a_newer_release_retargets_the_drain_without_a_second_handoff(tmp_path):
    _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    runtime, shutdowns = _runtime(tmp_path, service)
    clock, order, desired = Clock(), [], ["r1"]
    monitor = _monitor(runtime, desired, clock, order)
    try:
        _send(service, "running")
        monitor.check_once()
        deadline = monitor.status()["deadline"]
        desired[0] = "r2"
        clock.now += 30
        monitor.check_once()
        assert monitor.status()["target_release"] == "r2"
        assert monitor.status()["deadline"] == deadline   # budget not extended

        provider.finish("running")
        assert monitor.check_once() is True
        assert order == ["handoff"] and shutdowns == ["shutdown"]
        assert monitor.status()["target_release"] == "r2"
    finally:
        runtime.server_close()


@pytest.mark.parametrize("withdrawn", ["old", ""], ids=["rollback", "not-ready"])
def test_a_rolled_back_or_unready_release_aborts_the_drain_cleanly(tmp_path, withdrawn):
    _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    runtime, shutdowns = _runtime(tmp_path, service)
    clock, desired = Clock(), ["new"]
    monitor = _monitor(runtime, desired, clock)
    try:
        _send(service, "running")
        monitor.check_once()
        _send(service, "held")
        desired[0] = withdrawn
        monitor.check_once()

        assert monitor.status()["phase"] == "idle"
        assert monitor.status()["attempt"] == 0
        assert td._SLOTS.fenced is False
        provider.finish("running")
        assert provider.texts() == ["running", "held"]
        assert shutdowns == []
    finally:
        runtime.server_close()


def test_a_rollback_between_seal_and_shutdown_unseals(tmp_path):
    _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    runtime, shutdowns = _runtime(tmp_path, service)
    reads = iter(["new", "new", "old"])
    monitor = RuntimeReleaseMonitor(
        runtime, running_release_id="old", desired_release_id=lambda: next(reads),
        clock=Clock())
    try:
        _send(service, "running")
        monitor.check_once()                  # fence
        provider.finish("running")
        assert monitor.check_once() is False  # sealed, then the re-read says old
        assert runtime._draining is False
        assert td._SLOTS.fenced is False
        assert shutdowns == []
    finally:
        runtime.server_close()


def test_runtime_status_shows_the_drain_and_who_waits_for_it(tmp_path):
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    runtime, _ = _runtime(tmp_path, service)
    clock, desired = Clock(), ["new"]
    runtime.release_monitor = _monitor(runtime, desired, clock)
    hub = live_hub.LiveHub(sink=lambda _event: None)
    live_hub.install(hub)
    hub.begin_turn(agent_id=agent, session="mike", conv="c", turn_id="t0")
    hub.end_turn(agent)
    try:
        _send(service, "running")
        runtime.release_monitor.check_once()
        _send(service, "next", queue=True)
        provider.finish("running")

        status = runtime.dispatch_request("status", {})["result"]
        assert status["drain"]["phase"] == "draining"
        assert status["drain"]["target_release"] == "new"
        assert status["drain"]["deadline"] == 1_700_000_000_000 + 900_000
        assert status["held"] == {agent: 1}
        assert status["draining"] is False
    finally:
        runtime.server_close()


def test_an_idle_agent_whose_next_turn_is_held_says_so(tmp_path):
    agent = _agent(tmp_path, "mike")
    service, provider = _service(tmp_path)
    hub = live_hub.LiveHub(sink=lambda _event: None)
    live_hub.install(hub)
    hub.begin_turn(agent_id=agent, session="mike", conv="c", turn_id="t0")
    hub.end_turn(agent)
    _send(service, "running")
    service.begin_admission_fence()
    _send(service, "next", queue=True)
    provider.finish("running")
    hub.end_turn(agent)

    td.refresh_hold_headlines()
    activity = hub.activities()[agent]
    assert (activity["state"], activity["headline"]) == (
        "idle", td.UPDATE_HOLD_HEADLINE)

    service.lower_admission_fence()
    assert hub.activities()[agent]["headline"] is None
