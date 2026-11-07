# Reviewer A's probes on be8baff4 (WIP c711135b, extends a0e6a4b0), unchanged, as the stage-2 round-6 regression.
"""Moving a goal must preserve user pause intent and recover failed activation."""
import threading
import pytest
from lib import codex_app_server as cas
from tests.unit.test_codex_generations import fake, _leftover, _wait, _goal_turns
from tests.unit.test_codex_paused_goal_setup import native_goal_gate


@pytest.mark.parametrize('user_action,expected', [('pause', 'paused'), ('resume', 'active')])
def test_a_user_pause_while_the_moves_pause_is_outstanding_stays_paused(fake, tmp_path, monkeypatch, user_action, expected):
    monkeypatch.setenv('CLARP_QA_GOAL_TURN_HOLD', '0')
    agent, thread, _ = _leftover(fake, tmp_path, monkeypatch, 'pause-race')
    cas.goal(agent, 'resume')
    client = cas._client(agent, 'pause-race')
    original = client.request
    waiting, release = threading.Event(), threading.Event()
    held, errors = [], []
    pause_answered, user_entered, user_done = threading.Event(), threading.Event(), threading.Event()
    def request(method, params, timeout=30):
        if method == 'thread/goal/set' and params.get('status') == 'paused' and not held:
            held.append(True)
            waiting.set()
            assert release.wait(5)
        result = original(method, params, timeout=timeout)
        if method == 'thread/goal/set' and params.get('status') == 'paused':
            pause_answered.set()
        return result
    monkeypatch.setattr(client, 'request', request)
    def moving_pause():
        try:
            client._pause_for_move(thread)
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=moving_pause)
    worker.start()
    def user_action_call():
        user_entered.set()
        try:
            cas.goal(agent, user_action)
        except Exception as exc:
            errors.append(exc)
        finally:
            user_done.set()
    user = threading.Thread(target=user_action_call)
    try:
        assert waiting.wait(5)
        user.start()
        assert user_entered.wait(5)
        user_done.wait(.5)  # old code lets it overtake; ordered code may block it
    finally:
        release.set()
        worker.join(5)
        if user.ident is not None:
            user.join(5)
    assert not worker.is_alive() and not user.is_alive() and not errors, errors
    assert pause_answered.is_set(), 'control: the move pause must complete, not escape by timing out'
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
    assert cas._move_get(thread) is not None, 'failed activation deleted durable recovery intent'
    turns_before = len(_goal_turns(fake, thread))
    cas.recover_goal_moves()          # provider is healthy; remaining intent must recover
    status = cas.goal(agent, 'get')['status']
    assert cas._move_get(thread) is None, 'the record outlived a successful restore'
    # The fake completes an active goal with exactly one continuation turn ~50 ms later
    # (CLARP_QA_GOAL_TURN_HOLD=0); a later read sees that, never 'paused'.
    assert status == 'active' or (status == 'complete' and len(_goal_turns(fake, thread)) == turns_before + 1), \
        'failed activation deleted durable recovery intent and left the goal paused'


def test_a_drain_backstop_does_not_kill_a_continuation_before_its_notification_is_delivered(fake, tmp_path, monkeypatch, native_goal_gate):
    monkeypatch.setenv('CLARP_QA_GOAL_TURN_HOLD', '3')
    agent, thread, _ = _leftover(fake, tmp_path, monkeypatch, 'late-native-start')
    assert _wait(lambda: not cas.active_handles(agent))
    client = cas._client(agent, 'late-native-start')
    client.draining = True
    monkeypatch.setattr(cas, '_DRAINING', {id(client): client})
    delivered, release, unblocked = (threading.Event() for _ in range(3))
    original = client._notification
    native_turn = []
    def notification(method, params):
        if method == 'turn/started' and params.get('threadId') == thread:
            native_turn.append(params["turn"]["id"])
            delivered.set()
            try:
                assert release.wait(native_goal_gate.remaining()), "owned notification gate deadline"
                return original(method, params)
            finally:
                unblocked.set()
        return original(method, params)
    monkeypatch.setattr(client, '_notification', notification)
    before = len(_goal_turns(fake, thread))
    try:
        cas.goal(agent, 'resume')
        assert delivered.wait(5)
        assert _wait(lambda: len(_goal_turns(fake, thread)) == before + 1), 'control: native continuation really started'
        assert not cas.active_handles(agent), 'control: its start notification is still being delivered'
        assert len(native_turn) == 1
        native_goal_gate.assert_live(client, thread, native_turn[0])
        client._exit_if_drained(backstop=True)
        assert client.proc.poll() is None, 'drain backstop killed a running native continuation before its start notification was adopted'
    finally:
        release.set()
        if delivered.is_set():
            assert unblocked.wait(native_goal_gate.remaining()), "notification callback did not finish"


def test_a_goal_admitted_after_the_last_drain_snapshot_is_not_killed(fake, tmp_path, monkeypatch):
    monkeypatch.setenv('CLARP_QA_GOAL_TURN_HOLD', '3')
    agent, thread, _ = _leftover(fake, tmp_path, monkeypatch, 'drain-admission')
    assert _wait(lambda: not cas.active_handles(agent))
    client = cas._client(agent, 'drain-admission')
    client.draining = True
    monkeypatch.setattr(cas, '_DRAINING', {id(client): client})
    monkeypatch.setattr(cas, '_CLIENTS', {})
    looked_up, begin, native_running, answer_release = (threading.Event() for _ in range(4))
    errors = []
    real_goal, real_request, real_quiet = client.goal, client.request, client._goal_may_continue
    before = len(_goal_turns(fake, thread))
    def after_pool_lookup(*args, **kwargs):
        looked_up.set()
        assert begin.wait(5)
        return real_goal(*args, **kwargs)
    monkeypatch.setattr(client, 'goal', after_pool_lookup)
    def request(method, params, timeout=30):
        result = real_request(method, params, timeout=timeout)
        if method == 'thread/goal/set' and params.get('status') == 'active':
            assert _wait(lambda: len(_goal_turns(fake, thread)) == before + 1)
            native_running.set()
            assert answer_release.wait(5)
        return result
    monkeypatch.setattr(client, 'request', request)
    def quiet_snapshot():
        result = real_quiet()
        begin.set()
        native_running.wait(.5) # a correct fence may block/refuse the caller
        return result
    monkeypatch.setattr(client, '_goal_may_continue', quiet_snapshot)
    def user_resume():
        try:
            cas.goal(agent, 'resume') # actual public lookup of the draining owner
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=user_resume)
    worker.start()
    try:
        assert looked_up.wait(5)
        client._exit_if_drained(backstop=True)
        cut_running = native_running.is_set() and client.proc.poll() is not None
    finally:
        begin.set()
        answer_release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert all(isinstance(error, RuntimeError) for error in errors), errors
    assert not cut_running, 'a goal admitted after the quiet snapshot was interrupted by drain shutdown'


def test_a_credential_switch_does_not_kill_an_unadopted_native_continuation(fake, tmp_path, monkeypatch, native_goal_gate):
    from tests.unit.test_codex_generations import _agent, _turn
    monkeypatch.setenv('CLARP_QA_GOAL_TURN_HOLD', '3')
    agent, thread, _ = _leftover(fake, tmp_path, monkeypatch, 'refresh-native-start')
    assert _wait(lambda: not cas.active_handles(agent))
    client = cas._client(agent, 'refresh-native-start')
    delivered, release, unblocked = (threading.Event() for _ in range(3))
    original = client._notification
    native_turn = []
    def notification(method, params):
        if method == 'turn/started' and params.get('threadId') == thread:
            native_turn.append(params["turn"]["id"])
            delivered.set()
            try:
                assert release.wait(native_goal_gate.remaining()), "owned notification gate deadline"
                return original(method, params)
            finally:
                unblocked.set()
        return original(method, params)
    monkeypatch.setattr(client, '_notification', notification)
    before = len(_goal_turns(fake, thread))
    try:
        cas.goal(agent, 'resume')
        assert delivered.wait(5)
        assert _wait(lambda: len(_goal_turns(fake, thread)) == before + 1)
        assert not cas.active_handles(agent), 'control: native start is still in delivery'
        assert len(native_turn) == 1
        native_goal_gate.assert_live(client, thread, native_turn[0])
        (fake / 'auth.json').write_text('account-two')
        nxt = _agent(tmp_path, 'switch-next')
        done, outcome, _ = _turn(tmp_path, nxt, 'switch-next', 'new credential turn')
        assert done.wait(5) and outcome == ['completed']
        assert client.proc.poll() is None, 'credential refresh called an executing native goal idle and killed its process'
    finally:
        release.set()
        if delivered.is_set():
            assert unblocked.wait(native_goal_gate.remaining()), "notification callback did not finish"


@pytest.mark.parametrize('shutdown_path', ['credential', 'recycle', 'usage'])
def test_a_current_client_goal_admitted_after_the_quiet_snapshot_is_not_killed(fake, tmp_path, monkeypatch, shutdown_path):
    monkeypatch.setenv('CLARP_QA_GOAL_TURN_HOLD', '3')
    agent, thread, _ = _leftover(fake, tmp_path, monkeypatch, 'current-admission')
    assert _wait(lambda: not cas.active_handles(agent))
    client = cas._client(agent, 'current-admission')
    looked_up, begin, native_running, answer_release = (threading.Event() for _ in range(4))
    errors = []
    real_goal, real_request, real_quiet = client.goal, client.request, client._goal_may_continue
    before = len(_goal_turns(fake, thread))
    def after_pool_lookup(*args, **kwargs):
        looked_up.set()
        assert begin.wait(5)
        return real_goal(*args, **kwargs)
    monkeypatch.setattr(client, 'goal', after_pool_lookup)
    def request(method, params, timeout=30):
        result = real_request(method, params, timeout=timeout)
        if method == 'thread/goal/set' and params.get('status') == 'active':
            assert _wait(lambda: len(_goal_turns(fake, thread)) == before + 1)
            native_running.set()
            assert answer_release.wait(5)
        return result
    monkeypatch.setattr(client, 'request', request)
    def quiet_snapshot():
        result = real_quiet()
        begin.set()
        native_running.wait(.5) # a correct exit fence may block or refuse admission
        return result
    monkeypatch.setattr(client, '_goal_may_continue', quiet_snapshot)
    def user_resume():
        try:
            cas.goal(agent, 'resume')
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=user_resume)
    worker.start()
    try:
        assert looked_up.wait(5)
        if shutdown_path == 'credential':
            (fake / 'auth.json').write_text('account-two')
            cas._client(agent, 'current-admission')
        elif shutdown_path == 'recycle':
            cas.recycle_clients()
        else:
            cas.recover_usage_failure(cas.CodexTurnFailure('synthetic usage limit', client, False))
        cut_running = native_running.is_set() and client.proc.poll() is not None
    finally:
        begin.set()
        answer_release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert all(isinstance(error, RuntimeError) for error in errors), errors
    assert not cut_running, f'{shutdown_path} shutdown killed a goal admitted after its quiet snapshot'
