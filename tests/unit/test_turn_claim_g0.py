"""Durable manual-send refusal and recovery at the dispatch boundary."""
from types import SimpleNamespace

import pytest

from lib import turn_dispatch as td, turn_queue
from lib.task_goal_recovery import WakeRejected
from tests.patching import patch_shared
from tests.unit.test_turn_dispatch import _make_service


@pytest.fixture
def queued(tmp_path, monkeypatch):
    patch_shared(monkeypatch, td, '_RUNTIME_CLIENT', None)
    service, backend, agent_id = _make_service(tmp_path)
    backend.live = False
    turn_queue.enqueue(queue_id='manual', agent_id=agent_id, session='mike',
                       text='synthetic queued request', trace_id='queued-trace',
                       client_msg_id='manual', synthesize_audio=False,
                       origin='user', sender_agent_id='')
    return service, backend, agent_id


def test_manual_send_busy_backend_preserves_retryable_queue(queued):
    service, backend, _ = queued
    backend.live = True
    with pytest.raises(td.DispatchError, match='agent is still working'):
        service.dispatch_queued('manual')
    assert turn_queue.status('manual') == 'queued'
    assert backend.spawned == []


@pytest.mark.parametrize('failure', [RuntimeError('submit failed'), KeyboardInterrupt()])
def test_manual_send_releases_claim_when_submit_aborts(queued, monkeypatch, failure):
    service, backend, _ = queued

    def abort(command):
        assert turn_queue.status('manual') == 'claimed'
        raise failure

    monkeypatch.setattr(service, 'submit', abort)
    with pytest.raises(type(failure)) as caught:
        service.dispatch_queued('manual')
    assert caught.value is failure
    assert turn_queue.status('manual') == 'queued'
    assert backend.spawned == []


@pytest.mark.parametrize('at_submit', [False, True])
def test_manual_send_transient_goal_refusal_releases_claim(queued, monkeypatch, at_submit):
    service, backend, _ = queued
    rejection = td.GoalWakeDispatchError(
        WakeRejected('capacity', 'synthetic capacity refusal'))

    def refuse(*args, **kwargs):
        raise rejection

    if at_submit:
        monkeypatch.setattr(service, 'submit', refuse)
    else:
        patch_shared(monkeypatch, td, '_validate_goal_dispatch', refuse)
    with pytest.raises(td.GoalWakeDispatchError) as caught:
        service.dispatch_queued('manual')
    assert caught.value is rejection
    assert turn_queue.status('manual') == 'queued'
    assert backend.spawned == []


def test_manual_send_propagates_non_availability_runtime_error(queued):
    service, backend, _ = queued
    failure = ValueError('invalid runtime request')

    def refuse(queue_id):
        raise failure

    service.ctx.runtime_client = SimpleNamespace(dispatch_queued=refuse)
    with pytest.raises(ValueError) as caught:
        service.dispatch_queued('manual')
    assert caught.value is failure
    assert turn_queue.status('manual') == 'queued'
    assert backend.spawned == []
