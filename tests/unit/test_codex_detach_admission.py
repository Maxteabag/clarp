"""BACKENDS-06/07b stage 1, round 2 (reviewer A's findings on 1ae76709).

F1: the quiet recycle of a stuck shared process must not kill a turn admitted
for another agent between its quiet check and the retirement.
F2: a late turn/started for the detached execution (or any turn on the stuck
thread) is not adopted again as a goal continuation.
Lead: a send to the stopped agent waits behind the stuck process instead of
failing, so the user's text is never lost; it runs once the process is gone.
The first two scenarios are reviewer A's probes (WIP 3697662b)."""

from tests.codex_request import result_reply
import threading

from lib import agents, codex_app_server as cas, turn_lifecycle as tl
from tests.unit.test_codex_stuck_and_switch import _client, _turn


def test_quiet_recycle_cannot_kill_a_newly_admitted_turn(tmp_path, monkeypatch):
    client = _client()
    client.detached_agents = {'stopped-agent'}
    client.process_group = False
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_auth_fingerprint', lambda: b'old')
    requests = []

    @result_reply
    def reply(method, params, timeout=30):
        requests.append(method)
        return {'thread': {'id': 'fresh-thread'}} if method.startswith('thread/') else {'turn': {'id': 'fresh-turn'}}

    client.request = reply
    checked = threading.Event()
    busy = cas._busy

    def observed_busy(owner):
        result = busy(owner)
        if threading.current_thread().name == 'recycle-worker':
            checked.set()
        return result

    monkeypatch.setattr(cas, '_busy', observed_busy)
    errors = []

    def recycle():
        try:
            client.recycle_if_quiet()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    # A timer starts the recycle while a send holds the admission lock; the
    # send admits another agent's turn before the recycle can retire.
    with cas._LOCK:
        worker = threading.Thread(target=recycle, name='recycle-worker')
        worker.start()
        checked.wait(0.5)        # a quiet read outside the lock happens here
        fresh = cas.spawn_turn(text='synthetic', cwd=tmp_path, session='fresh',
                               agent_id='fresh-agent', trace_id='fresh-trace')
        assert 'turn/start' in requests and fresh.is_alive()
    worker.join(2)
    assert not worker.is_alive() and not errors
    assert fresh.is_alive()
    assert not client.proc.kill.called, 'recycle killed the shared process after admitting another agent'
    assert cas._CLIENTS.get(cas._SHARED_KEY) is client


def _stopped_agent(tmp_path, client):
    stopped = agents.create_agent(persona='Stopped', voice_id='v1', cwd=str(tmp_path),
                                  session='stopped', backend='codex')
    agents.start_runtime(stopped, 'stopped')
    agents.bind_backend_session(stopped, 'stopped-thread')
    stuck = _turn(client, stopped, 'stuck-turn')
    client._actives[stopped].thread_id = 'stopped-thread'
    other = _turn(client, 'other-agent', 'other-turn')
    client._actives['other-agent'].thread_id = 'other-thread'
    tl.transition(stopped, tl.TurnEvent.STOP_REQUESTED, {'source': 'user_stop'})
    stuck._detach_if_running()   # the real deadline callback
    return stopped, stuck, other


def test_late_started_for_a_detached_turn_does_not_readopt_it(tmp_path, monkeypatch):
    client = _client()
    client.process_group = False
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    stopped, stuck, other = _stopped_agent(tmp_path, client)
    assert stuck._done.is_set() and not cas.active_handles(stopped)
    assert other.is_alive() and not client.proc.kill.called
    client._notification('turn/started', {'threadId': 'stopped-thread', 'turn': {'id': 'stuck-turn'}})
    assert not cas.active_handles(stopped), 'late start re-adopted the detached execution as a goal continuation'
    # Nor a continuation codex starts by itself on the stuck thread.
    client._notification('turn/started', {'threadId': 'stopped-thread', 'turn': {'id': 'next-turn'}})
    assert not cas.active_handles(stopped)
    assert stopped not in cas.busy_agents()


def test_the_stopped_agent_is_held_until_the_stuck_process_is_recycled(tmp_path, monkeypatch):
    client = _client()
    client.process_group = False
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    stopped, _stuck, other = _stopped_agent(tmp_path, client)
    holders = cas.turn_holders(stopped)
    assert holders and all(h.is_alive() for h in holders), \
        'a send must wait behind the stuck process, not fail'
    assert not cas.turn_holders('other-agent')
    # The other agent's turn ends: the quiet process is recycled.
    client._actives.pop('other-agent')
    other._done.set()
    client.recycle_if_quiet()
    assert client.proc.kill.called
    client.proc.poll.return_value = -9
    assert not any(h.is_alive() for h in holders)
    assert not cas.turn_holders(stopped)
