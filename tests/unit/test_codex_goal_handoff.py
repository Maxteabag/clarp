# Reviewer A's probes on 66f67140 (WIP 2b5712de), kept unchanged as the round-4 regression.
"""Goal activation publication and follow-up sends retain their retirement fences."""

from tests.codex_request import result_reply
import sqlite3

import threading

import pytest

from lib import agent_goals, agents, backends, codex_app_server as cas, turn_dispatch as td
from tests.unit.test_codex_recycle_boundaries import _codex_agent
from tests.unit.test_codex_stuck_and_switch import _client, _turn
from tests.unit.test_drain_pause_parity import World


@pytest.mark.parametrize('publication', ['before_mirror', 'failed_mirror'])
def test_successful_activation_is_protected_until_its_active_state_is_known(tmp_path, monkeypatch, publication):
    client = _client()
    client.process_group = False
    client.detached_agents = {'stopped-agent'}
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_RETIRING', {})
    other = _codex_agent(tmp_path, 'goal-owner', 'goal-thread')
    client._loaded_threads.add('goal-thread')
    agent_goals.upsert(other, session='goal-owner', backend='codex',
        goal=agent_goals.from_codex({'objective': 'synthetic', 'status': 'paused'}))
    accepted = []

    @result_reply
    def reply(method, params, timeout=30):
        assert method == 'thread/goal/set'
        assert params['status'] == 'active'
        accepted.append(True)
        return {'goal': {'objective': 'synthetic', 'status': 'active'}}

    client.request = reply
    if publication == 'before_mirror':
        mirror = client._mirror_goal

        def recycle_before_publication(thread_id, goal):
            client.recycle_if_quiet()
            return mirror(thread_id, goal)

        monkeypatch.setattr(client, '_mirror_goal', recycle_before_publication)
    else:
        def locked(*_args, **_kwargs):
            raise sqlite3.OperationalError('database is locked')
        monkeypatch.setattr(agent_goals, 'upsert', locked)
    client.goal(agents.get_by_agent_id(other), 'resume')
    assert accepted == [True], 'control: the provider accepted the active goal'
    if publication == 'failed_mirror':
        assert agent_goals.get(other)['status'] == 'paused'
        client.recycle_if_quiet()
    assert not client.proc.kill.called, 'a successful provider activation lost protection before active-state publication'


def test_a_second_send_to_a_held_stopped_session_queues_instead_of_spawning(tmp_path, monkeypatch):
    td.reset_for_tests()
    monkeypatch.setattr(td, '_RUNTIME_CLIENT', None)
    monkeypatch.setattr(backends, '_RUNTIME_CLIENT', None)
    world = World(tmp_path)
    agents.update_agent(world.agent, backend='codex')
    agents.bind_backend_session(world.agent, 'stopped-thread')
    world.service.backends = backends
    client = _client()
    client.detached_agents = {world.agent}
    client.process_group = False
    _turn(client, 'other-agent', 'other-turn')
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_RETIRING', {})
    monkeypatch.setattr(cas, '_auth_fingerprint', lambda: b'old')
    try:
        first = world.service.dispatch(text='first waiting send', requested_session='mike',
            client_msg_id='first-wait', trace_id='first-wait', synthesize_audio=False)
        assert first.queued and cas.turn_holders(world.agent)
        assert not cas.active_handles(world.agent)
        second = world.service.dispatch(text='second waiting send', requested_session='mike',
            client_msg_id='second-wait', trace_id='second-wait', synthesize_audio=False)
        assert second.queued, 'a second send took the held-session owner for a stale turn and attempted a spawn'
    finally:
        td.reset_for_tests()
        # The waiting send left a thread that polls for the held process
        # (turn_claim._wait_for_process_exit). With the slots reset it ends
        # within one poll and then finishes its turn; that belongs to this
        # test, not to whichever one is running a quarter of a second later.
        for waiter in threading.enumerate():
            if waiter.name == f'turn-wait-{world.agent}':
                waiter.join(5)
                assert not waiter.is_alive()


def test_a_goal_update_the_mirror_failed_to_record_keeps_the_process(tmp_path, monkeypatch):
    """Round 4: a thread/goal/updated whose mirror write failed leaves the
    goal's state unknown, and an unknown goal may continue (fail closed)."""
    client = _client()
    client.process_group = False
    client.detached_agents = {'stopped-agent'}
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_RETIRING', {})
    other = _codex_agent(tmp_path, 'notified', 'notified-thread')
    client._loaded_threads.add('notified-thread')

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(agent_goals, 'upsert', locked)
    client._notification('thread/goal/updated', {'threadId': 'notified-thread',
                                                 'goal': {'objective': 'synthetic', 'status': 'active'}})
    client.recycle_if_quiet()
    assert not client.proc.kill.called
    monkeypatch.undo()
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_RETIRING', {})
    client._notification('thread/goal/updated', {'threadId': 'notified-thread',
                                                 'goal': {'objective': 'synthetic', 'status': 'paused'}})
    client.recycle_if_quiet()
    assert client.proc.kill.called, 'once the mirror records a paused goal the process may go'
