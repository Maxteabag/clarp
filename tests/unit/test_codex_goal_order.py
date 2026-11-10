# Reviewer A's probe on a70a5aa2 (WIP 8dedc6af), kept unchanged as the round-5 regression.
"""An old goal snapshot must not erase protection for a newer activation."""

from tests.codex_request import result_reply
from lib import agent_goals, agents, codex_app_server as cas
from tests.unit.test_codex_recycle_boundaries import _codex_agent
from tests.unit.test_codex_stuck_and_switch import _client


def test_older_paused_notification_cannot_clear_an_inflight_resume(tmp_path, monkeypatch):
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
    def reply(method, params, timeout=30):
        assert method == 'thread/goal/set' and params['status'] == 'active'
        requested.append(True)
        # The prior pause response let its caller issue resume, while its
        # older notification was still queued on the protocol reader.
        client._notification('thread/goal/updated', {
            'threadId': 'goal-thread', 'goal': {'objective': 'synthetic', 'status': 'paused'}})
        client.recycle_if_quiet()  # the new resume has not replied yet
        return {'goal': {'objective': 'synthetic', 'status': 'active'}}

    client.request = reply
    client.goal(agents.get_by_agent_id(other), 'resume')
    assert requested == [True]
    assert not client.proc.kill.called, 'an older paused snapshot cleared protection for the inflight active request'


def test_an_activation_whose_request_failed_keeps_the_process(tmp_path, monkeypatch):
    """Round 5: a start/resume whose request raised may still have reached
    codex, so its goal stays possibly active."""
    import pytest
    client = _client()
    client.process_group = False
    client.detached_agents = {'stopped-agent'}
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_RETIRING', {})
    other = _codex_agent(tmp_path, 'lost-reply', 'lost-thread')
    client._loaded_threads.add('lost-thread')
    agent_goals.upsert(other, session='lost-reply', backend='codex',
        goal=agent_goals.from_codex({'objective': 'synthetic', 'status': 'paused'}))

    @result_reply
    def reply(method, params, timeout=30):
        raise TimeoutError('codex app-server did not answer')

    client.request = reply
    with pytest.raises(TimeoutError):
        client.goal(agents.get_by_agent_id(other), 'resume')
    client.recycle_if_quiet()
    assert not client.proc.kill.called, 'an unanswered activation may have started the goal'
