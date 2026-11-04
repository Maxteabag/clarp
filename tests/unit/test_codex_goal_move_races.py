# Reviewer A's probes on 5903e5c7 (WIP 8f3638fa, extends 89abae55), unchanged, as the stage-2 round-3 regression.
"""Moving a goal must preserve user pause intent and recover failed activation."""
import threading
import pytest
from lib import codex_app_server as cas
from tests.unit.test_codex_generations import fake, _leftover, _wait, _goal_turns


@pytest.mark.parametrize('user_action,expected', [('pause', 'paused'), ('resume', 'active')])
def test_a_user_pause_while_the_moves_pause_is_outstanding_stays_paused(fake, tmp_path, monkeypatch, user_action, expected):
    monkeypatch.setenv('CLARP_QA_GOAL_TURN_HOLD', '0')
    agent, thread, _ = _leftover(fake, tmp_path, monkeypatch, 'pause-race')
    cas.goal(agent, 'resume')
    client = cas._client(agent, 'pause-race')
    original = client.request
    waiting, release = threading.Event(), threading.Event()
    held, errors = [], []
    def request(method, params, timeout=30):
        if method == 'thread/goal/set' and params.get('status') == 'paused' and not held:
            held.append(True)
            waiting.set()
            assert release.wait(5)
        return original(method, params, timeout=timeout)
    monkeypatch.setattr(client, 'request', request)
    def moving_pause():
        try:
            client._pause_for_move(thread)
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=moving_pause)
    worker.start()
    try:
        assert waiting.wait(5)
        cas.goal(agent, user_action) # real public user action clears the move claim
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors, errors
    assert _wait(lambda: not cas.active_handles(agent))
    assert client._capture_move(thread)
    cas._continue_move(thread)
    assert cas.goal(agent, 'get')['status'] == expected, 'the old move pause overrode the user goal action'


def test_a_failed_restore_can_be_recovered_on_the_next_pass(fake, tmp_path, monkeypatch):
    monkeypatch.setenv('CLARP_QA_GOAL_TURN_HOLD', '0')
    agent, thread, _ = _leftover(fake, tmp_path, monkeypatch, 'retry-restore')
    assert _wait(lambda: not cas.active_handles(agent))
    client = cas._client(agent, 'retry-restore')
    goal = client.request('thread/goal/get', {'threadId': thread})['goal']
    cas._move_put(thread, {'agent_id': agent, 'thread': thread, 'phase': 'moving',
                          'generation': 'old', 'updated_at': goal['updatedAt']})
    original, refused = client.request, []
    def request(method, params, timeout=30):
        if method == 'thread/goal/set' and params.get('status') == 'active' and not refused:
            refused.append(True)
            raise RuntimeError('synthetic pre-handoff restore failure')
        return original(method, params, timeout=timeout)
    monkeypatch.setattr(client, 'request', request)
    cas.recover_goal_moves()
    assert refused, 'control: the first activation was actually refused'
    assert cas.goal(agent, 'get')['status'] == 'paused'
    cas.recover_goal_moves()          # provider is healthy; remaining intent must recover
    assert cas.goal(agent, 'get')['status'] == 'active', 'failed activation deleted durable recovery intent and left the goal paused'


def test_a_drain_backstop_does_not_kill_a_continuation_before_its_notification_is_delivered(fake, tmp_path, monkeypatch):
    monkeypatch.setenv('CLARP_QA_GOAL_TURN_HOLD', '3')
    agent, thread, _ = _leftover(fake, tmp_path, monkeypatch, 'late-native-start')
    assert _wait(lambda: not cas.active_handles(agent))
    client = cas._client(agent, 'late-native-start')
    client.draining = True
    monkeypatch.setattr(cas, '_DRAINING', {id(client): client})
    delivered, release = threading.Event(), threading.Event()
    original = client._notification
    def notification(method, params):
        if method == 'turn/started' and params.get('threadId') == thread:
            delivered.set()
            assert release.wait(8)
        return original(method, params)
    monkeypatch.setattr(client, '_notification', notification)
    before = len(_goal_turns(fake, thread))
    try:
        cas.goal(agent, 'resume')
        assert delivered.wait(5)
        assert _wait(lambda: len(_goal_turns(fake, thread)) == before + 1), 'control: native continuation really started'
        assert not cas.active_handles(agent), 'control: its start notification is still being delivered'
        client._exit_if_drained(backstop=True)
        assert client.proc.poll() is None, 'drain backstop killed a running native continuation before its start notification was adopted'
    finally:
        release.set()
