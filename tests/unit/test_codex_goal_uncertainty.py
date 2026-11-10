# Reviewer A's probe on cd1119d4 (WIP 1349e0c5), kept unchanged as the round-6 regression.
"""A mirror write predating a timed-out activation cannot settle its uncertainty."""

from tests.codex_request import result_reply
import pytest

from lib import agent_goals, agents, codex_app_server as cas
from tests.unit.test_codex_recycle_boundaries import _codex_agent
from tests.unit.test_codex_stuck_and_switch import _client


def test_old_paused_write_finishing_after_a_resume_timeout_keeps_uncertainty(tmp_path, monkeypatch):
    client = _client()
    client.process_group = False
    client.detached_agents = {'stopped-agent'}
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_RETIRING', {})
    other = _codex_agent(tmp_path, 'goal-owner', 'goal-thread')
    client._loaded_threads.add('goal-thread')
    agent_goals.upsert(other, session='goal-owner', backend='codex',
        goal=agent_goals.from_codex({'objective': 'synthetic', 'status': 'paused'}))
    requested = []

    @result_reply
    def lost_reply(method, params, timeout=30):
        assert method == 'thread/goal/set' and params['status'] == 'active'
        requested.append(True)
        raise TimeoutError('activation may have reached codex; response was not delivered')

    client.request = lost_reply
    upsert = agent_goals.upsert
    delayed = []

    def older_write_finishes_later(*args, **kwargs):
        # The reader is handling the old paused event. While its DB write
        # waits, another caller's newer resume loses its response.
        if not delayed:
            delayed.append(True)
            with pytest.raises(TimeoutError):
                client.goal(agents.get_by_agent_id(other), 'resume')
        return upsert(*args, **kwargs)

    monkeypatch.setattr(agent_goals, 'upsert', older_write_finishes_later)
    client._notification('thread/goal/updated', {
        'threadId': 'goal-thread', 'goal': {'objective': 'synthetic', 'status': 'paused'}})
    assert delayed == [True] and requested == [True]
    assert agent_goals.get(other)['status'] == 'paused'
    client.recycle_if_quiet()
    assert not client.proc.kill.called, 'a pre-activation mirror write erased the timed-out activation uncertainty'


def test_a_later_notification_never_resolves_an_unanswered_activation(tmp_path, monkeypatch):
    """Round 6: nothing proves which state codex settled on after an
    activation whose answer was lost, not even a later paused update (it may
    predate the activation). The process is kept for its lifetime."""
    client = _client()
    client.process_group = False
    client.detached_agents = {'stopped-agent'}
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_RETIRING', {})
    other = _codex_agent(tmp_path, 'lost-answer', 'lost-thread')
    client._loaded_threads.add('lost-thread')

    @result_reply
    def lost_reply(method, params, timeout=30):
        raise TimeoutError('no answer')

    client.request = lost_reply
    with pytest.raises(TimeoutError):
        client.goal(agents.get_by_agent_id(other), 'resume')
    client._notification('thread/goal/updated', {
        'threadId': 'lost-thread', 'goal': {'objective': 'synthetic', 'status': 'paused'}})
    client.recycle_if_quiet()
    assert not client.proc.kill.called
