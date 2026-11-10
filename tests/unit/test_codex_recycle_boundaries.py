"""BACKENDS-06/07b stage 1, round 3 (reviewer A on 4dd1c5e7).

F1: the recycle never kills a process on which another agent's turn was
adopted before the kill, nor one where a goal could start a continuation by
itself (an active goal on a thread loaded there, or a goal being activated).
F3: a kill without process-exit evidence keeps the stopped agent's thread
held, so its queued send keeps waiting.
The first and the F3 scenario follow reviewer A's probes (WIP 34cad05d)."""

from tests.codex_request import result_reply
import subprocess

import pytest

from lib import agent_goals, agents, codex_app_server as cas
from tests.unit.test_codex_stuck_and_switch import _client


@pytest.fixture
def pool(monkeypatch):
    client = _client()
    client.process_group = False
    client.detached_agents = {'stopped-agent'}
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_RETIRING', {}, raising=False)
    return client


def _codex_agent(tmp_path, name, thread):
    agent = agents.create_agent(persona=name, voice_id='v', cwd=str(tmp_path), session=name, backend='codex')
    agents.start_runtime(agent, name)
    agents.bind_backend_session(agent, thread)
    return agent


def test_a_continuation_adopted_after_the_quiet_read_is_not_cut(pool, tmp_path, monkeypatch):
    other = _codex_agent(tmp_path, 'other', 'other-thread')
    busy = cas._busy
    announced = []

    def provider_starts_after_the_read(owner):
        result = busy(owner)
        if not result and not announced:
            announced.append(True)
            owner._notification('turn/started', {'threadId': 'other-thread', 'turn': {'id': 'goal-next'}})
            assert cas.active_handles(other), 'control: the continuation was adopted'
        return result

    monkeypatch.setattr(cas, '_busy', provider_starts_after_the_read)
    pool.recycle_if_quiet()
    assert announced
    assert not pool.proc.kill.called, 'recycle killed another agent\'s continuation'
    assert cas._CLIENTS.get(cas._SHARED_KEY) is pool


def test_an_active_goal_on_another_loaded_thread_keeps_the_process(pool, tmp_path):
    other = _codex_agent(tmp_path, 'goal-owner', 'goal-thread')
    pool._loaded_threads.add('goal-thread')
    agent_goals.upsert(other, session='goal-owner', backend='codex',
                       goal=agent_goals.from_codex({'objective': 'synthetic', 'status': 'active'}))
    pool.recycle_if_quiet()
    assert not pool.proc.kill.called, 'codex could start that goal\'s continuation at any moment'
    agent_goals.upsert(other, session='goal-owner', backend='codex',
                       goal=agent_goals.from_codex({'objective': 'synthetic', 'status': 'paused'}))
    pool.recycle_if_quiet()
    assert pool.proc.kill.called


def test_a_goal_being_activated_keeps_the_process_and_none_starts_on_a_retiring_one(pool, tmp_path):
    agent = _codex_agent(tmp_path, 'activating', 'activating-thread')
    pool._loaded_threads.add('activating-thread')
    seen = []

    @result_reply
    def request(method, params, timeout=30):
        if method == 'thread/goal/set':
            pool.recycle_if_quiet()          # a recycle while the activation is in flight
            seen.append(pool.proc.kill.called)
            return {'goal': {'objective': 'synthetic', 'status': 'active'}}
        return {}

    pool.request = request
    cas_agent = agents.get_by_agent_id(agent)
    pool.goal(cas_agent, 'start', 'synthetic')
    assert seen == [False], 'recycled while a goal activation could start a continuation'
    pool.recycled = True                      # a recycle that already won
    with pytest.raises(RuntimeError):
        pool.goal(cas_agent, 'resume')


def test_an_unconfirmed_kill_keeps_the_thread_held(pool):
    holders = cas.turn_holders('stopped-agent')
    assert holders and holders[0].is_alive()
    pool.proc.wait.side_effect = subprocess.TimeoutExpired('synthetic app-server', 5)
    pool.recycle_if_quiet()
    assert pool.proc.kill.called and pool.proc.poll() is None
    assert holders[0].is_alive(), 'thread hold released without confirmed process exit'
    assert cas.turn_holders('stopped-agent'), 'a new send could no longer find the held thread'
    pool.proc.poll.return_value = -9          # now it is gone
    assert not holders[0].is_alive() and not cas.turn_holders('stopped-agent')
