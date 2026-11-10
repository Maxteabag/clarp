# Reviewer A's probe (WIP 5812031b, extends 61b3f873 with late_continuation), unchanged.
"""A stopped native goal must stay fenced when its resume-time pause fails."""

from tests.codex_request import result_reply
import threading
import pytest
from lib import agent_goals, codex_app_server as cas
from tests.unit.test_codex_recycle_boundaries import _codex_agent
from tests.unit.test_codex_stuck_and_switch import _client


@pytest.mark.parametrize('late_continuation', [False, True])
def test_an_unanswered_resume_pause_does_not_admit_the_user_turn(tmp_path, monkeypatch, late_continuation):
    client = _client()
    stopped = _codex_agent(tmp_path, 'stopped', 'stopped-thread')
    agent_goals.upsert(stopped, session='stopped', backend='codex',
                      goal=agent_goals.from_codex({'objective': 'synthetic', 'status': 'active'}))
    cas._stop_pause_put('stopped-thread')
    sent = []
    interrupted = threading.Event()
    @result_reply
    def reply(method, params, timeout=30):
        sent.append(method)
        if method == 'thread/resume':
            return {'thread': {'id': 'stopped-thread'}}
        if method == 'thread/goal/set':
            raise TimeoutError('pause answer lost; native goal may still be active')
        if method == 'turn/interrupt':
            interrupted.set()
            return {}
        if method == 'turn/start':
            return {'turn': {'id': 'user-turn'}}
        return {}
    client.request = reply
    handle = cas.AppTurnHandle(client, agent_id=stopped)
    try:
        client.start(text='after stop', cwd=tmp_path, backend_session_id='stopped-thread',
                     is_new_session=False, session='stopped', trace_id='after-stop', model='',
                     effort='', voice=False, persona='', handle=handle, on_session_init=None,
                     on_result=None, on_error=None, stream=None, enqueue=lambda **_k: 0, agent_id=stopped)
    except (RuntimeError, TimeoutError):
        pass
    assert cas._stop_pause_get('stopped-thread') is not None
    assert 'turn/start' not in sent, 'user turn admitted while the stopped native goal is still unpaused'
    if late_continuation:
        client._notification('turn/started', {'threadId': 'stopped-thread',
            'turn': {'id': 'late-continuation', 'threadId': 'stopped-thread'}})
        assert interrupted.wait(.5), 'the stopped native goal continued after the failed pause without an interrupt'
        assert stopped not in client._actives, 'the stopped goal continuation was adopted as a fresh live turn'
