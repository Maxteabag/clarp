# Reviewer A's probes on 1d99a224 (WIP b28e0404), kept unchanged as the round-7 regression (lead's coarse rule).
"""Lead's coarse stage-1 rule: hold nonterminal goals and outstanding goal answers."""
import pytest

from lib import agent_goals, agents, codex_app_server as cas
from tests.unit.test_codex_recycle_boundaries import _codex_agent
from tests.unit.test_codex_stuck_and_switch import _client


def _loaded_goal(tmp_path, monkeypatch, status):
    client = _client()
    client.process_group = False
    client.detached_agents = {'stopped-agent'}
    monkeypatch.setattr(cas, '_CLIENTS', {cas._SHARED_KEY: client})
    monkeypatch.setattr(cas, '_RETIRING', {})
    other = _codex_agent(tmp_path, 'goal-owner', 'goal-thread')
    client._loaded_threads.add('goal-thread')
    agent_goals.upsert(other, session='goal-owner', backend='codex',
        goal=agent_goals.from_codex({'objective': 'synthetic', 'status': status}))
    return client, agents.get_by_agent_id(other)


@pytest.mark.parametrize('status', [agent_goals.ACTIVE, agent_goals.BLOCKED,
                                    agent_goals.USAGE_LIMITED, agent_goals.BUDGET_LIMITED])
def test_a_nonterminal_goal_not_paused_by_the_user_holds_the_process(tmp_path, monkeypatch, status):
    client, _agent = _loaded_goal(tmp_path, monkeypatch, status)
    client.recycle_if_quiet()
    assert not client.proc.kill.called, 'recycled a goal that is neither terminal nor user-paused'


def test_an_outstanding_goal_read_keeps_even_a_user_paused_process(tmp_path, monkeypatch):
    client, agent = _loaded_goal(tmp_path, monkeypatch, agent_goals.ACTIVE)
    requests = []

    def paused_reply(method, params, timeout=30):
        requests.append(method)
        if method == 'thread/goal/get':
            client.recycle_if_quiet()
        return {'goal': {'objective': 'synthetic', 'status': 'paused'}}

    client.request = paused_reply
    # Round 8 adaptation (disclosed): the user-paused state is set in the
    # mirror directly. A goal() pause would now end its own answer and
    # re-trigger the recycle, which may legitimately release a user-paused
    # process before this read starts.
    agent_goals.upsert(agent['agent_id'], session=agent['session'], backend='codex',
        goal=agent_goals.from_codex({'objective': 'synthetic', 'status': 'paused'}))
    assert not client.proc.kill.called
    client.goal(agent, 'get')
    assert requests == ['thread/goal/get']
    assert not client.proc.kill.called, 'recycled while another agents goal answer was outstanding'


def test_an_explicitly_user_paused_goal_can_release_the_process(tmp_path, monkeypatch):
    client, agent = _loaded_goal(tmp_path, monkeypatch, agent_goals.ACTIVE)
    client.request = lambda *_a, **_k: {'goal': {'objective': 'synthetic', 'status': 'paused'}}
    client.goal(agent, 'pause')
    client.recycle_if_quiet()
    assert client.proc.kill.called


def test_an_outstanding_stop_pause_holds_the_process(tmp_path, monkeypatch):
    """Round 7: the Stop path's own goal pause is an outstanding goal answer
    too, until it returns or times out."""
    client, agent = _loaded_goal(tmp_path, monkeypatch, agent_goals.PAUSED)
    seen = []

    def reply(method, params, timeout=30):
        client.recycle_if_quiet()
        seen.append(client.proc.kill.called)
        return {'goal': {'objective': 'synthetic', 'status': 'paused'}}

    client.request = reply
    active = type('A', (), {'agent_id': agent['agent_id'], 'thread_id': 'goal-thread'})()
    real_get, calls = agent_goals.get, []

    def get(agent_id):
        calls.append(agent_id)   # only the Stop path's own decision sees it active
        return {'status': agent_goals.ACTIVE} if len(calls) == 1 else real_get(agent_id)

    monkeypatch.setattr(agent_goals, 'get', get)
    client._pause_goal_for_stop(active)
    assert seen == [False]
