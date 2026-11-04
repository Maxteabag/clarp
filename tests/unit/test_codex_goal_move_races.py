# Reviewer A's probes on 85645f29 (WIP 89abae55), unchanged, as the stage-2 round-2 regression.
"""Moving a goal must preserve user pause intent and recover failed activation."""
import threading
from lib import codex_app_server as cas
from tests.unit.test_codex_generations import fake, _leftover, _wait


def test_a_user_pause_while_the_moves_pause_is_outstanding_stays_paused(fake, tmp_path, monkeypatch):
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
        cas.goal(agent, 'pause')      # real public user action clears the move claim
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors, errors
    assert _wait(lambda: not cas.active_handles(agent))
    assert client._capture_move(thread)
    cas._continue_move(thread)
    assert cas.goal(agent, 'get')['status'] == 'paused', 'the old move pause answer recreated a claim and resumed the user-paused goal'


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
