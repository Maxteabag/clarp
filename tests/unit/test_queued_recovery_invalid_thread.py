"""Queued recovery against an offline Codex whose thread cannot be read.

The real dispatcher and Codex app-server client drive tests/qa/fake_codex.py.
The agent is bound to a thread whose rollout has no session_meta, so every
thread/resume fails the way Miso's did on 2026-10-10 ("rollout ... is
empty"). A resume error is not proof that native adoption did not happen.
Under the signed 933 fallback, keep unknown work visible without automatic
retry; leave user pause and account-recovery wait as they were.
"""
import itertools
import json
import time
from types import SimpleNamespace

import pytest

from lib import activity, codex_app_server, db, recovery_backoff, turn_lifecycle, turn_queue
from lib import agents as agents_db
from lib.account_failover import Attempt
from lib.protocol import AgentState
from lib.turn_dispatch import DispatchError, TurnDispatchService
import lib.turn_dispatch as _td
from tests.unit.test_codex_app_server import _install_fake_codex, _spawn_fake_turn

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

    def requests(method):
        log = home / "turn-requests.jsonl"
        if not log.exists():
            return []
        return [line for line in log.read_text().splitlines()
                if json.loads(line)["method"] == method]

    return SimpleNamespace(service=service, agent_id=agent_id, run_for=run_for,
                           resumes=resumes, send=send, timers=timers, home=home,
                           requests=requests, tmp_path=tmp_path)


def test_an_unreadable_thread_is_protected_visible_and_never_automatically_retried(broken):
    broken.send("t1")
    queued = turn_queue.pending(broken.agent_id)
    assert [row["trace_id"] for row in queued] == ["t1"]
    assert broken.resumes() == 1
    before = [tuple(row) for row in db.conn().execute(
        "SELECT * FROM turns WHERE agent_id=? ORDER BY turn_id", (broken.agent_id,))]
    assert len(before) == 1

    broken.run_for(3600)
    for _ in range(2):
        assert broken.service.recover_queued() == 0
        turn_queue.reconcile_receipts()

    # A correlated native ERROR may follow adoption. No NOTSTARTED proof is
    # manufactured from its text or from not having seen a reader event yet.
    assert broken.resumes() == 1
    assert broken.requests("turn/start") == []
    assert not broken.timers
    assert not db.conn().execute(
        """SELECT 1 FROM state_log WHERE agent_id=?
             AND json_extract(detail,'$.trace_id')='t1'
             AND json_extract(detail,'$.dispatch_not_started')=1""",
        (broken.agent_id,)).fetchone()
    assert agents_db.trace_launch_status(broken.agent_id, "t1") == "unknown"
    assert turn_queue.pending_for_dispatch() == []
    assert not turn_queue.claim_for_dispatch(queued[0]["queue_id"])
    assert turn_queue.claim(queued[0]["queue_id"]) is None
    assert turn_queue.pending(broken.agent_id) == queued
    assert turn_queue.pending_count(broken.agent_id) == 1
    assert not turn_queue.is_paused(broken.agent_id)
    assert [tuple(row) for row in db.conn().execute(
        "SELECT * FROM turns WHERE agent_id=? ORDER BY turn_id", (broken.agent_id,))] == before


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


def test_the_codex_unreadable_thread_error_is_one_failure_whatever_the_thread(broken):
    other = "0a1b2c3d-ab3f-4e5f-9a8b-112233445566"
    (broken.home / "sessions" / f"rollout-{other}.jsonl").write_text("")
    errors = []
    for thread in (THREAD, other):
        with pytest.raises(RuntimeError) as caught:
            _spawn_fake_turn(broken.tmp_path, agent_id=broken.agent_id, session="miso",
                             text="x", backend_session_id=thread, is_new_session=False)
        errors.append(caught.value)

    assert "is empty" in str(errors[0]) and other in str(errors[1])
    assert recovery_backoff.signature(errors[0]) == recovery_backoff.signature(errors[1])


def test_a_queue_paused_after_an_unknown_failure_is_neither_retried_nor_unpaused(broken):
    broken.send("t1")
    broken.run_for(10)
    tried = broken.resumes()
    assert tried == 1
    turn_queue.set_paused(broken.agent_id, True)
    queued = turn_queue.pending(broken.agent_id)

    broken.run_for(3600)

    assert turn_queue.is_paused(broken.agent_id)
    assert broken.resumes() == tried
    assert turn_queue.pending(broken.agent_id) == queued


def test_slot_repair_does_not_retry_unknown_native_failure_or_unpause_queue(broken):
    broken.send("t1")
    broken.run_for(3600)
    assert _td._RECOVERY_BACKOFF.snapshot() == {}
    turn_queue.set_paused(broken.agent_id, True)
    queued = turn_queue.pending(broken.agent_id)
    tried = broken.resumes()

    broken.service.repair_slot(broken.agent_id, actor="test", reason="retry")
    broken.run_for(3600)

    assert broken.agent_id not in _td._RECOVERY_BACKOFF.snapshot()
    assert turn_queue.is_paused(broken.agent_id)
    assert broken.resumes() == tried
    assert turn_queue.pending(broken.agent_id) == queued


def _account_wait(agent_id, trace):
    failover = _td.account_failover("codex")
    _td._SLOTS.claim(agent_id, trace)
    failover.attempts[agent_id] = Attempt(
        agent_id=agent_id, trace_id=trace, model="", state={"account_recovery": True},
        owned=lambda: True, pause=lambda: None, resume=lambda: None)
    turn_lifecycle.transition(agent_id, turn_lifecycle.TurnEvent.ACCOUNT_RECOVERY_WAIT, {
        "trace_id": trace, "account_recovery": "waiting",
        "message": "Waiting for a codex account with available usage"}, force=True)
    return failover


def test_an_account_recovery_release_launches_the_queued_message_once(broken):
    agent_id = broken.agent_id
    # The thread is readable here: the account wait is the only thing holding.
    (broken.home / "sessions" / f"rollout-{THREAD}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": THREAD}}) + "\n")
    failover = _account_wait(agent_id, "tA")
    try:
        broken.send("t1")
        broken.run_for(600)
        assert broken.requests("turn/start") == []

        # The account frees up: the held turn settles and hands over, while
        # a recovery pass runs at the same moment.
        failover.attempts.pop(agent_id, None)
        broken.service._finish_slot(agent_id, "tA", "codex")
        broken.service.recover_queued()
        deadline = time.time() + 8
        while time.time() < deadline and turn_queue.pending(agent_id):
            time.sleep(0.05)
        broken.run_for(600)
        deadline = time.time() + 2
        while time.time() < deadline:
            time.sleep(0.05)
            broken.run_for(1)

        starts = [line for line in broken.requests("turn/start") if "message t1" in line]
        assert len(starts) == 1
        assert turn_queue.pending(agent_id) == []
        assert _td._RECOVERY_BACKOFF.snapshot() == {}
    finally:
        failover.attempts.pop(agent_id, None)
