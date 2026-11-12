"""TP-N3 (ii) round 2: reviewer A's implementation guardrails (notes on the
approved design, reviews/tpn3-design-4a0d9565-notes.md).

1. An unknown launch outcome is not 'nothing ran': a lost turn/start reply
   keeps a hold, and a learned id is interrupted; a Stop before the launch
   sends nothing; a Stop whose 10 s detach fires before the start returns
   still interrupts the exact turn; the Stop never waits on the goal pause.
2. The agent's own follow-up during a pending start is durable (a park row)
   before it is acknowledged, and a failed park refuses it.
3. A retiring client is published before it leaves the pool.
4. A late launch failure cleans up only its own claim."""
import threading
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from lib import agent_goals, agents as agents_db, backends, codex_app_server as cas, turn_dispatch as td
import sqlite3

from lib import launch_receipts, turn_queue
from lib.backend.registry import by_id
from tests.patching import patch_shared
from tests.unit.test_codex_start_head_of_line import _Stream, _agent, _timed, _wait
from tests.unit.test_codex_generations import fake as gen_fake, _agent as _gen_agent, _turn as _gen_turn, _wait as _gen_wait  # noqa: F401

FAKE = Path(__file__).resolve().parents[2] / "tests/qa/fake_codex.py"


@pytest.fixture
def pool(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CLARP_QA_PROVIDER_ROOT", str(home))
    monkeypatch.setattr(by_id("codex"), "required_binary", str(FAKE))
    cas._CLIENTS.clear()
    td.reset_for_tests()
    service = td.TurnDispatchService(SimpleNamespace(default_session="rachel", stream=_Stream(),
                                                     agents_path=tmp_path / "unused.json"),
                                     backend_registry=backends, home=tmp_path)
    world = SimpleNamespace(service=service, sent=[], rules={}, rachel=_agent(tmp_path, "rachel"),
                            mike=_agent(tmp_path, "mike"))
    real = cas._Client.request

    def request(self, method, params, timeout=30, **kwargs):
        world.sent.append((method, dict(params)))
        rule = world.rules.get(method)
        if rule is not None and rule.get("match", lambda p: True)(params):
            return rule["do"](lambda: real(self, method, params, timeout=timeout, **kwargs), timeout)
        return real(self, method, params, timeout=timeout, **kwargs)

    monkeypatch.setattr(cas._Client, "request", request)
    yield world
    for rule in world.rules.values():
        rule.get("release", threading.Event()).set()
    cas.recycle_clients()


def _hold(world, method, match=lambda p: True):
    held, release = threading.Event(), threading.Event()

    def do(call, timeout):
        held.set()
        release.wait(20)
        return call()
    world.rules[method] = {"match": match, "do": do, "release": release}
    return held, release


def _first_turn(world, agent, name):
    done = world.service.dispatch(text="hello", requested_session=name, trace_id=f"t-{name}-0",
                                  synthesize_audio=False)
    assert _wait(lambda: not cas.active_handles(agent), 10)
    return done


def _stop(world, agent):
    td.clear_for_agent(agent, preserve_queue=True, pause_queue=True)
    return backends.interrupt("codex", agent)


def _interrupts(world):
    return [p for m, p in world.sent if m == "turn/interrupt"]


def test_the_own_follow_up_is_durable_before_it_is_acknowledged_and_runs_once(pool):
    held, release = _hold(pool, "turn/start", lambda p: "HOLD" in str(p.get("input")))
    _timed(lambda: pool.service.dispatch(text="HOLD", requested_session="rachel", trace_id="t-r1",
                                         synthesize_audio=False), 0.0)
    assert held.wait(15)
    ok, result, _, _ = _timed(lambda: pool.service.dispatch(
        text="and also", requested_session="rachel", trace_id="t-r2", synthesize_audio=False), 3.0)
    assert ok and getattr(result, "queued", False), result
    rows = turn_queue.parked(pool.rachel) + turn_queue.pending(pool.rachel)
    assert any(row["trace_id"] == "t-r2" for row in rows), "queued:true without a durable row"
    release.set()
    assert _wait(lambda: sum("and also" in str(p.get("input")) for m, p in pool.sent if m == "turn/start") == 1, 20)
    time.sleep(1.0)
    assert sum("and also" in str(p.get("input")) for m, p in pool.sent if m == "turn/start") == 1


def test_a_failed_park_refuses_the_follow_up(pool, monkeypatch):
    held, release = _hold(pool, "turn/start", lambda p: "HOLD" in str(p.get("input")))
    _ok, _, _, starter = _timed(lambda: pool.service.dispatch(
        text="HOLD", requested_session="rachel", trace_id="t-r1", synthesize_audio=False), 0.0)
    assert held.wait(15)
    patch_shared(monkeypatch, td, "_park", lambda spec, prefix="park": "")
    with pytest.raises(td.DispatchError) as error:
        pool.service.dispatch(text="and also", requested_session="rachel", trace_id="t-r2",
                              synthesize_audio=False)
    assert error.value.status == 503
    release.set()
    starter.join(15)
    assert _wait(lambda: not cas.active_handles(pool.rachel), 15)


def test_a_stop_before_the_launch_sends_no_turn_start(pool):
    held, release = _hold(pool, "thread/start")
    ok, _, _, starter = _timed(lambda: pool.service.dispatch(
        text="never launched", requested_session="rachel", trace_id="t-r1", synthesize_audio=False), 0.0)
    assert held.wait(15)
    ok, _, _, _ = _timed(lambda: _stop(pool, pool.rachel), 3.0)
    assert ok
    release.set()
    starter.join(15)
    assert not any(m == "turn/start" and "never launched" in str(p.get("input")) for m, p in pool.sent), \
        "a turn stopped before its launch was still sent"


def test_a_lost_turn_start_reply_keeps_a_hold_and_interrupts_the_learned_turn(pool, monkeypatch):
    def lose_reply(call, timeout):
        call()                       # codex accepted it and runs the turn
        time.sleep(0.3)              # its turn/started reaches the reader
        raise TimeoutError("Codex app-server timed out: turn/start")
    pool.rules["turn/start"] = {"match": lambda p: "LOST" in str(p.get("input")), "do": lose_reply}
    allow_end = threading.Event()
    real_notification = cas._Client._notification

    def notification(self, method, params):
        if method == "turn/completed":
            allow_end.wait(10)       # the execution is still running until released
        return real_notification(self, method, params)
    monkeypatch.setattr(cas._Client, "_notification", notification)
    try:
        with pytest.raises(td.DispatchError):
            pool.service.dispatch(text="LOST [qa-slow]", requested_session="rachel", trace_id="t-r1",
                                  synthesize_audio=False)
        assert cas.turn_holders(pool.rachel), "an uncertain launch released the thread"
        assert not cas.active_handles(pool.rachel), "the uncertain execution was adopted as a live turn"
        assert _wait(lambda: _interrupts(pool)), "the execution learned through the reader was not interrupted"
        assert cas.turn_holders(pool.rachel), "the hold ended before the execution did"
    finally:
        allow_end.set()
    assert _wait(lambda: not cas.turn_holders(pool.rachel), 10), "the hold outlived the ended execution"


def test_a_stop_whose_detach_fires_before_the_start_returns_still_interrupts_that_turn(pool, monkeypatch):
    monkeypatch.setattr(cas, "STOP_DETACH_SEC", 0.3)
    # Another agent keeps the process busy, so stage 1 does not recycle it
    # when the stopped pending turn is detached (alone, the recycle would end
    # the process and no late reply would exist to interrupt).
    pool.service.dispatch(text="[qa-slow] keeps it busy", requested_session="mike", trace_id="t-m1",
                          synthesize_audio=False)
    held, release = _hold(pool, "turn/start", lambda p: "HOLD" in str(p.get("input")))
    _, _, _, starter = _timed(lambda: pool.service.dispatch(
        text="HOLD [qa-slow]", requested_session="rachel", trace_id="t-r1", synthesize_audio=False), 0.0)
    assert held.wait(15)
    ok, _, _, _ = _timed(lambda: _stop(pool, pool.rachel), 3.0)
    assert ok
    time.sleep(0.8)                  # the detach deadline passes while the start is held
    release.set()
    starter.join(15)
    assert _wait(lambda: _interrupts(pool)), "the detached pending turn was never interrupted by its id"


def test_a_stop_during_a_start_never_waits_on_the_goal_pause(pool):
    _first_turn(pool, pool.rachel, "rachel")
    thread = agents_db.live_backend_session(pool.rachel)
    agent_goals.upsert(pool.rachel, session="rachel", backend="codex",
                       goal=agent_goals.from_codex({"objective": "synthetic", "status": "active"}))
    held, release = _hold(pool, "turn/start", lambda p: "HOLD" in str(p.get("input")))
    paused, unpause = _hold(pool, "thread/goal/set", lambda p: p.get("status") == "paused")
    _, _, _, starter = _timed(lambda: pool.service.dispatch(
        text="HOLD", requested_session="rachel", trace_id="t-r1", synthesize_audio=False), 0.0)
    assert held.wait(15)
    ok, _, seconds, _ = _timed(lambda: _stop(pool, pool.rachel), 3.0)
    assert ok, "the Stop waited on the goal pause"
    assert cas._stop_pause_get(thread) is not None, "the durable stopped-goal fence was not written"
    release.set()
    assert paused.wait(10), "the goal pause never followed the learned id"
    unpause.set()
    starter.join(15)
    assert _wait(lambda: _interrupts(pool))
    assert _wait(lambda: cas._stop_pause_get(thread) is None), "the fence outlived the landed pause"


def test_a_retiring_client_is_published_before_it_leaves_the_pool(monkeypatch):
    from tests.unit.test_codex_stuck_and_switch import _client
    client = _client()
    client.process_group = False
    client.detached_agents = {"stopped-agent"}
    seen = []

    class Pool(dict):
        def clear(self):
            seen.append(id(client) in cas._RETIRING)
            super().clear()

    monkeypatch.setattr(cas, "_CLIENTS", Pool({cas._SHARED_KEY: client}))
    monkeypatch.setattr(cas, "_RETIRING", {})
    client.recycle_if_quiet()
    assert seen == [True], "a lock-free reader could see the stuck client in neither place"


def test_a_late_launch_failure_cleans_up_only_its_own_claim(tmp_path):
    from tests.unit.test_codex_stuck_and_switch import _client
    client = _client()
    successor = {}

    def request(method, params, timeout=30):
        if method == "thread/start":
            return {"thread": {"id": "t-1"}}
        if method == "turn/start":
            # A later turn of the same agent took over while this RPC ran.
            handle = cas.AppTurnHandle(client, agent_id="agent")
            successor["active"] = cas._ActiveTurn(
                "turn-2", "t-1", "agent", "s", "trace-2", cas.TurnState(live_backend_session_id="t-1"),
                handle, None, None, None, lambda **_k: 0)
            client._actives["agent"] = successor["active"]
            raise RuntimeError("turn/start: {'code': -32000, 'message': 'old launch failed'}")
        return {}

    client.request = request
    first = cas.AppTurnHandle(client, agent_id="agent")
    with pytest.raises(RuntimeError):
        client.start(text="x", cwd=tmp_path, backend_session_id="", is_new_session=True, session="s",
                     trace_id="trace-1", model="", effort="", voice=False, persona="", handle=first,
                     on_session_init=None, on_result=None, on_error=None, stream=None,
                     enqueue=lambda **_k: 0, agent_id="agent")
    assert client._actives.get("agent") is successor["active"], "the old launch removed its successor"
    assert successor["active"].handle.is_alive()


def _runtime_stop(world, agent):
    # The Host's Stop against the runtime (runtime_bridge begin_stop and
    # finish_stop, the lease held across the interrupt).
    snapshot, _, _ = td.begin_stop(agent, user_stop=True)
    td.fence_user_stop(agent)
    terminated = backends.interrupt("codex", agent)
    td.complete_stop(world.service.ctx, agent, snapshot, set(), backend_registry=backends)
    return terminated


def _host_stop(world, agent):
    # The in-process Host's Stop (server.py _stop_agent_turns).
    _stop(world, agent)
    td.finish_stop(world.service.ctx, agent, backend_registry=backends)


@pytest.mark.parametrize("stop", [_host_stop, _runtime_stop], ids=["in-process", "runtime"])
def test_a_follow_up_queued_during_a_start_survives_stop_and_runs_once_after_resume(pool, stop):
    # TP-N3 (iii): queued:true was the answer, so Stop pauses it like any
    # queued message; it neither runs before the resume nor vanishes.
    def follow_ups():
        return sum("and also" in str(p.get("input")) for m, p in pool.sent if m == "turn/start")
    held, release = _hold(pool, "turn/start", lambda p: "HOLD" in str(p.get("input")))
    _, _, _, starter = _timed(lambda: pool.service.dispatch(
        text="HOLD [qa-slow]", requested_session="rachel", trace_id="t-r1", synthesize_audio=False), 0.0)
    assert held.wait(15)
    result = pool.service.dispatch(text="and also", requested_session="rachel", trace_id="t-r2",
                                   client_msg_id="cm-r2", synthesize_audio=False)
    assert getattr(result, "queued", False), result
    ok, _, _, _ = _timed(lambda: stop(pool, pool.rachel), 3.0)
    assert ok
    release.set()
    starter.join(15)
    assert _wait(lambda: _interrupts(pool))
    assert _wait(lambda: not cas.active_handles(pool.rachel), 10)
    assert [row["trace_id"] for row in turn_queue.pending(pool.rachel)] == ["t-r2"], \
        "the follow-up acknowledged as queued did not survive the Stop in the paused queue"
    time.sleep(0.5)
    assert follow_ups() == 0, "the follow-up ran before the resume"
    turn_queue.set_paused(pool.rachel, False)
    pool.service.recover_queued()
    assert _wait(lambda: follow_ups() == 1, 20), "the follow-up did not run after the resume"
    pool.service.recover_queued()
    time.sleep(1.0)
    assert follow_ups() == 1
    assert not turn_queue.pending(pool.rachel) and not turn_queue.parked(pool.rachel)
    assert (launch_receipts.get("t-r2") or {}).get("state") == launch_receipts.ATTEMPTED, \
        "the follow-up did not launch through its own receipt exactly once"


def test_a_rolled_back_stop_leaves_the_follow_up_to_run_once(pool):
    # A strict Stop whose interrupt fails restores the memory queue; the kept
    # row must not become a second copy of the follow-up.
    def follow_ups():
        return sum("and also" in str(p.get("input")) for m, p in pool.sent if m == "turn/start")
    held, release = _hold(pool, "turn/start", lambda p: "HOLD" in str(p.get("input")))
    _, _, _, starter = _timed(lambda: pool.service.dispatch(
        text="HOLD", requested_session="rachel", trace_id="t-r1", synthesize_audio=False), 0.0)
    assert held.wait(15)
    pool.service.dispatch(text="and also", requested_session="rachel", trace_id="t-r2",
                          synthesize_audio=False)
    snapshot = td.snapshot_stop_state(pool.rachel)
    td.clear_for_agent(pool.rachel, preserve_queue=True, pause_queue=True)
    td.restore_stop_state(pool.rachel, snapshot)
    turn_queue.set_paused(pool.rachel, False)
    release.set()
    starter.join(15)
    assert _wait(lambda: follow_ups() == 1, 20), "the follow-up did not run after the rollback"
    # Its launch unparks the row once the spawn returns (after turn/start).
    assert _wait(lambda: not turn_queue.parked(pool.rachel), 10)
    pool.service.recover_queued()
    time.sleep(1.0)
    assert follow_ups() == 1, "the rolled-back Stop left a second copy of the follow-up"
    assert not turn_queue.pending(pool.rachel) and not turn_queue.parked(pool.rachel), \
        "a second copy of the follow-up stays queued"


# Reviewer A's probes on 582dc80b (f88f49de), unchanged but for names.
def test_old_cleanup_cannot_remove_a_successor_between_identity_check_and_pop(tmp_path, monkeypatch):
    from tests.unit.test_codex_stuck_and_switch import _client
    client = _client()
    agent, other = _gen_agent(tmp_path, 'claim-owner'), _gen_agent(tmp_path, 'claim-other')
    old_handle = cas.AppTurnHandle(client, agent_id=agent)
    old = cas._ActiveTurn('old-turn', 'old-thread', agent, 'claim-owner', 'old-trace',
        cas.TurnState(live_backend_session_id='old-thread'), old_handle,
        None, None, None, lambda **_k: 0)
    read, registered = threading.Event(), threading.Event()
    fired, handles, errors = [], [], []
    class Claims(dict):
        def get(self, key, default=None):
            observed = super().get(key, default)
            if key == agent and observed is old and not fired:
                fired.append(True)
                read.set()
                registered.wait(.5) # synchronized cleanup may correctly block the successor
            return observed
    client.active = old
    client._actives = Claims({agent: old})
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_DRAINING', {})
    monkeypatch.setattr(cas, '_client', lambda *_a, **_k: client)
    sequence = []
    def request(method, params, timeout=30):
        if method == 'thread/start':
            sequence.append(True)
            return {'thread': {'id': f'thread-{len(sequence)}'}}
        if method == 'turn/start':
            return {'turn': {'id': f'turn-{len(sequence)}'}}
        return {}
    client.request = request
    def spawn(owner, session):
        return cas.spawn_turn(text='next', cwd=tmp_path, agent_id=owner, session=session,
                              is_new_session=True, trace_id='next-'+session, enqueue=lambda **_k: 0)
    def successor():
        try:
            assert read.wait(5)
            # Actual terminal handler ends the old turn before the legitimate next admission.
            client._notification('turn/completed', {'threadId': 'old-thread',
                'turn': {'id': 'old-turn', 'threadId': 'old-thread', 'status': 'completed'}})
            handles.append(spawn(agent, 'claim-owner'))
        except Exception as exc:
            errors.append(exc)
        finally:
            registered.set()
    worker = threading.Thread(target=successor)
    worker.start()
    client._forget_claim(agent, old)
    worker.join(5)
    assert fired and not worker.is_alive() and not errors, errors
    assert handles[0].is_alive()
    spawn(other, 'claim-other') # the compatibility pointer now belongs to this other agent
    assert cas.active_handles(agent) == handles, 'old cleanup removed the successor from live backend ownership'


def test_an_ended_uncertain_start_does_not_hold_its_agent_forever(gen_fake, tmp_path, monkeypatch):
    agent = _gen_agent(tmp_path, 'ended-uncertain-start')
    started, allow_end, ended = threading.Event(), threading.Event(), threading.Event()
    real_request, real_notification = cas._Client.request, cas._Client._notification
    def request(self, method, params, timeout=30, **kwargs):
        result = real_request(self, method, params, timeout=timeout, **kwargs)
        if method == 'turn/start':
            assert started.wait(5)
            raise TimeoutError('synthetic lost reply after actual native start')
        return result
    def notification(self, method, params):
        if method == 'turn/completed':
            assert allow_end.wait(5)  # consume the terminal event after launch cleanup
        result = real_notification(self, method, params)
        if method == 'turn/started':
            started.set()
        if method == 'turn/completed':
            ended.set()
        return result
    monkeypatch.setattr(cas._Client, 'request', request)
    monkeypatch.setattr(cas._Client, '_notification', notification)
    try:
        with pytest.raises(TimeoutError):
            _gen_turn(tmp_path, agent, 'ended-uncertain-start', '[qa-slow] uncertain request')
        allow_end.set()
        assert ended.wait(5), 'control: the interrupted native execution really ended'
        assert _gen_wait(lambda: not cas.turn_holders(agent), 3), \
            'confirmed terminal event left the sole agent held behind its otherwise quiet shared process'
    finally:
        allow_end.set()


# Reviewer A's probe on 19b7587d (refactor/review-core 31f70c3d), unchanged but for imports.
def test_a_retry_after_failed_queue_promotion_preserves_the_follow_up(pool, monkeypatch):
    held, release = _hold(pool, 'turn/start', lambda p: 'HOLD' in str(p.get('input')))
    _, _, _, starter = _timed(lambda: pool.service.dispatch(
        text='HOLD [qa-slow]', requested_session='rachel', trace_id='t-r1',
        synthesize_audio=False), 0)
    try:
        assert held.wait(5)
        result = pool.service.dispatch(text='keep this follow-up', requested_session='rachel',
            trace_id='t-r2', client_msg_id='follow-2', synthesize_audio=False)
        assert result.queued
        def locked(*_args, **_kwargs):
            raise sqlite3.OperationalError('database is locked')
        with monkeypatch.context() as patch:
            for name in ('keep_queued', 'keep_start_parked'):
                if hasattr(turn_queue, name):
                    patch.setattr(turn_queue, name, locked)
            with pytest.raises(sqlite3.OperationalError, match='database is locked'):
                _host_stop(pool, pool.rachel)
        assert any(row['trace_id'] == 't-r2' for row in turn_queue.parked(pool.rachel)), \
            'control: the acknowledged row survived the failed write'
        _host_stop(pool, pool.rachel)  # actual retry; database is healthy again
        rows = turn_queue.pending(pool.rachel) + turn_queue.parked(pool.rachel)
        assert any(row['trace_id'] == 't-r2' for row in rows), \
            'retry discarded the acknowledged start-park row after the failed Stop cleared its memory spec'
    finally:
        release.set()
        starter.join(5)
