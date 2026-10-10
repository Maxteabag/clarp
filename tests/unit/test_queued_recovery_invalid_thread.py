"""Queued recovery against an offline Codex whose thread cannot be read.

The real dispatcher and Codex app-server client drive tests/qa/fake_codex.py.
The agent is bound to a thread whose rollout has no session_meta, so every
thread/resume fails the way Miso's did on 2026-10-10 ("rollout ... is
empty"). Recovery must stay bounded, keep the queued message, say why it
stopped, and leave a user pause and an account-recovery wait as they were.
"""
import itertools
import json
from types import SimpleNamespace

import pytest

from lib import activity, codex_app_server, turn_lifecycle, turn_queue
from lib import agents as agents_db
from lib.account_failover import Attempt
from lib.protocol import AgentState
from lib.turn_dispatch import DispatchError, TurnDispatchService
import lib.turn_dispatch as _td
from tests.unit.test_codex_app_server import _install_fake_codex

THREAD = "c59fbeda-de2d-4989-a7a8-4683580325e9"


@pytest.fixture(autouse=True)
def _local_dispatch_runtime(monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    _td.reset_for_tests()
    yield
    _td.reset_for_tests()
    codex_app_server.recycle_clients()


@pytest.fixture
def broken(tmp_path, monkeypatch):
    home = _install_fake_codex(tmp_path, monkeypatch)
    (home / "sessions").mkdir()
    (home / "sessions" / f"rollout-{THREAD}.jsonl").write_text("")
    agent_id = agents_db.create_agent(
        persona="Miso", voice_id="v", cwd=str(tmp_path), session="miso",
        backend="codex")
    agents_db.start_runtime(agent_id, "miso")
    agents_db.bind_backend_session(agent_id, THREAD)
    clock = SimpleNamespace(t=1000.0)
    timers = []
    ids = itertools.count(1)
    service = TurnDispatchService(
        SimpleNamespace(default_session="miso", agents_path=tmp_path / "unused.json",
                        stream=SimpleNamespace(broadcast=lambda _e: None)),
        home=tmp_path, uuid_factory=lambda: f"fresh-{next(ids)}",
        now=lambda: clock.t,
        retry_scheduler=lambda delay, fn: timers.append((clock.t + delay, fn)))

    def run_for(seconds):
        until = clock.t + seconds
        while True:
            at = min((t for t, _ in timers if t <= until), default=None)
            if at is None:
                break
            index = next(i for i, (t, _) in enumerate(timers) if t == at)
            _, fn = timers.pop(index)
            clock.t = max(clock.t, at)
            fn()
        clock.t = until

    def resumes():
        log = home / "turn-requests.jsonl"
        if not log.exists():
            return 0
        return sum(json.loads(line)["method"] == "thread/resume"
                   for line in log.read_text().splitlines())

    def send(trace, origin="agent"):
        try:
            service.dispatch(
                text=f"message {trace}", requested_session="miso",
                forced_session="miso", trace_id=trace, client_msg_id=f"q-{trace}",
                synthesize_audio=False, queue_if_busy=True, origin=origin)
        except DispatchError:
            pass

    return SimpleNamespace(service=service, agent_id=agent_id, run_for=run_for,
                           resumes=resumes, send=send, timers=timers)


def test_an_unreadable_thread_is_retried_a_bounded_number_of_times(broken):
    broken.send("t1")
    queued = turn_queue.pending(broken.agent_id)
    assert [row["trace_id"] for row in queued] == ["t1"]
    assert broken.resumes() == 1

    broken.run_for(3600)

    # The released runtime made ~3600 attempts an hour; this is the first
    # send plus eight recovery attempts, then nothing more is scheduled.
    assert broken.resumes() == 9
    assert not broken.timers
    # The message is still queued, exactly as it was.
    assert turn_queue.pending(broken.agent_id) == queued
    assert not turn_queue.is_paused(broken.agent_id)
    # The agent says why it stopped: interrupted, naming the failure, not
    # thinking and not silent.
    state = agents_db.latest_state(broken.agent_id)
    assert state["kind"] == AgentState.INTERRUPTED
    assert not agents_db.is_busy(broken.agent_id)
    assert activity.live_state(state["kind"], state["detail"]) == "interrupted"
    event = activity.state_activity_event(
        agent_id=broken.agent_id, session="miso", persona="Miso",
        kind=state["kind"], ts=state["ts"], detail=state["detail"])
    assert "is empty" in event["summary"]
    assert "Queued messages are waiting" in event["summary"]


def test_a_user_paused_queue_stays_paused_and_untried(broken):
    turn_queue.set_paused(broken.agent_id, True)
    broken.send("t1", origin="user")
    queued = turn_queue.pending(broken.agent_id)
    tried = broken.resumes()

    broken.run_for(3600)

    assert turn_queue.is_paused(broken.agent_id)
    assert turn_queue.pending(broken.agent_id) == queued
    assert broken.resumes() == tried
    assert _td._RECOVERY_BACKOFF.snapshot() == {}


def test_an_account_recovery_wait_is_not_overridden(broken):
    agent_id = broken.agent_id
    failover = _td.account_failover("codex")
    _td._SLOTS.claim(agent_id, "tA")
    failover.attempts[agent_id] = Attempt(
        agent_id=agent_id, trace_id="tA", model="", state={"account_recovery": True},
        owned=lambda: True, pause=lambda: None, resume=lambda: None)
    turn_lifecycle.transition(agent_id, turn_lifecycle.TurnEvent.ACCOUNT_RECOVERY_WAIT, {
        "trace_id": "tA", "account_recovery": "waiting",
        "message": "Waiting for a codex account with available usage"}, force=True)
    try:
        broken.send("t1")
        broken.run_for(3600)

        assert broken.resumes() == 0
        assert [row["trace_id"] for row in turn_queue.pending(agent_id)] == ["t1"]
        state = agents_db.latest_state(agent_id)
        assert activity.live_state(state["kind"], state["detail"]) == "limited"
        assert _td._RECOVERY_BACKOFF.snapshot() == {}
    finally:
        failover.attempts.pop(agent_id, None)
