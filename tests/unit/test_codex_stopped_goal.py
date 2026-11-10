"""BACKENDS-06/07b stage 1, round 8 (lead's questions on 0dabadfb).

Q1: the coarse hold counts OTHER agents' threads only. A stuck agent with an
active goal whose Stop-pause never answers is still recycled once the other
threads are quiet, and the end of an outstanding goal answer re-triggers it.
Q2: after the recycle that goal may still be active in codex, and a resume
restarts an active goal by itself. Stop's semantics hold: the goal is paused
right after the resume, a continuation that starts anyway is interrupted at
turn/started and never taken for the user's send, and the send then runs."""

from tests.codex_request import result_reply
import threading
import time

from lib import agent_goals, agents, codex_app_server as cas
from tests.unit.test_codex_recycle_boundaries import _codex_agent
from tests.unit.test_codex_stuck_and_switch import _client


def _wait(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        time.sleep(0.02)
    return predicate()


def test_a_stuck_agents_own_unanswered_stop_pause_does_not_hold_the_recycle(tmp_path, monkeypatch):
    client = _client()
    client.process_group = False
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_RETIRING', {})
    stopped = _codex_agent(tmp_path, 'stopped', 'stuck-thread')
    agent_goals.upsert(stopped, session='stopped', backend='codex',
                       goal=agent_goals.from_codex({'objective': 'synthetic', 'status': 'active'}))
    client._loaded_threads.add('stuck-thread')
    client.detached_agents = {stopped}
    client.detached_executions = {'stuck-thread', 'stuck-turn'}
    client._goal_answer_begin('stuck-thread')      # its Stop-pause never answers
    client.recycle_if_quiet()
    assert client.proc.kill.called, 'the stuck agent itself kept its own process alive'


def test_the_end_of_another_agents_goal_answer_retriggers_the_recycle(tmp_path, monkeypatch):
    client = _client()
    client.process_group = False
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_RETIRING', {})
    client.detached_agents = {'stopped-agent'}
    client._goal_answer_begin('other-thread')
    client.recycle_if_quiet()
    assert not client.proc.kill.called            # held while that answer is outstanding
    client._goal_answer_end('other-thread')
    assert _wait(lambda: client.proc.kill.called), 'nothing recycled once the answer arrived'


def test_a_resume_after_an_unapplied_stop_pause_keeps_the_goal_paused(tmp_path, monkeypatch):
    client = _client()
    client.process_group = False
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    stopped = _codex_agent(tmp_path, 'stopped', 'stopped-thread')
    agent_goals.upsert(stopped, session='stopped', backend='codex',
                       goal=agent_goals.from_codex({'objective': 'synthetic', 'status': 'active'}))
    cas._stop_pause_put('stopped-thread')         # the stuck process never answered Stop's pause
    sent, interrupted = [], threading.Event()

    @result_reply
    def reply(method, params, timeout=30):
        sent.append((method, params.get('status') or params.get('turnId') or ''))
        if method == 'thread/resume':
            # Codex restarts the still-active goal right after the resume.
            client._notification('turn/started', {'threadId': 'stopped-thread',
                                                  'turn': {'id': 'continuation', 'threadId': 'stopped-thread'}})
            return {'thread': {'id': 'stopped-thread'}}
        if method == 'turn/interrupt':
            interrupted.set()
            return {}
        if method == 'thread/goal/set':
            return {'goal': {'objective': 'synthetic', 'status': 'paused'}}
        if method == 'turn/start':
            return {'turn': {'id': 'user-turn'}}
        return {}

    client.request = reply
    handle = cas.AppTurnHandle(client, agent_id=stopped)
    client.start(text='after the stop', cwd=tmp_path, backend_session_id='stopped-thread',
                 is_new_session=False, session='stopped', trace_id='after-stop', model='', effort='',
                 voice=False, persona='', handle=handle, on_session_init=None, on_result=None,
                 on_error=None, stream=None, enqueue=lambda **_k: 0, agent_id=stopped)
    assert interrupted.wait(3), 'the continuation the user stopped was not interrupted'
    methods = [m for m, _ in sent]
    assert ('turn/interrupt', 'continuation') in sent
    assert methods.index('thread/goal/set') < methods.index('turn/start'), 'the goal was not paused before the send ran'
    assert client._actives[stopped].turn_id == 'user-turn', 'the continuation was taken for the user\'s send'
    assert agent_goals.get(stopped)['status'] == 'paused'
    assert cas._stop_pause_get('stopped-thread') is None, 'the pending Stop-pause was not cleared'


def test_an_unanswered_stop_pause_leaves_a_durable_marker(tmp_path, monkeypatch):
    client = _client()
    client.process_group = False
    stopped = _codex_agent(tmp_path, 'stopped', 'marker-thread')
    agent_goals.upsert(stopped, session='stopped', backend='codex',
                       goal=agent_goals.from_codex({'objective': 'synthetic', 'status': 'active'}))

    @result_reply
    def no_answer(method, params, timeout=30):
        raise TimeoutError('the stuck process never answered')

    client.request = no_answer
    active = type('A', (), {'agent_id': stopped, 'thread_id': 'marker-thread'})()
    client._pause_goal_for_stop(active)
    assert cas._stop_pause_get('marker-thread') is not None
