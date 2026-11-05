"""Queue handoff failures must preserve ownership and acknowledged work."""
import pytest

from lib import eventlog, turn_dispatch as td
from lib.task_goal_recovery import WakeRejected
from tests.patching import patch_shared
from tests.unit.test_turn_dispatch import _make_service


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    patch_shared(monkeypatch, td, '_RUNTIME_CLIENT', None)
    scheduled = []
    service, backend, agent_id = _make_service(
        tmp_path, retry_scheduler=lambda delay, fn: scheduled.append((delay, fn)))
    backend.live = False
    spec = td._TurnSpec(
        backend='claude', text='synthetic handoff', cwd=tmp_path,
        backend_session_id='', is_new_session=True, session='mike',
        agent_id=agent_id, trace_id='handoff-trace',
        context=eventlog.EventContext(agent_id=agent_id, session='mike'),
        synthesize_audio=False)
    td._SLOTS.claim(agent_id, spec.trace_id)
    try:
        yield service, backend, spec, scheduled
    finally:
        td.reset_for_tests()


def _fail(*args, **kwargs):
    raise RuntimeError('synthetic storage failure')


def test_post_launch_bookkeeping_failure_keeps_backend_owned_slot(handoff, monkeypatch):
    service, backend, spec, scheduled = handoff
    monkeypatch.setattr(service, '_mark_spawned', _fail)
    service._resume_and_spawn(spec.agent_id, spec)
    assert len(backend.spawned) == 1
    assert td._SLOTS.owns(spec.agent_id, spec.trace_id)
    assert td._SLOTS.queue_depth(spec.agent_id) == 0
    assert scheduled == []


def test_spawn_failure_with_live_backend_does_not_retry_acknowledged_work(handoff):
    service, backend, spec, scheduled = handoff
    backend.live = True
    service._requeue_failed_handoff(spec.agent_id, spec, RuntimeError('spawn uncertain'),
                                   phase='spawning')
    assert td._SLOTS.owns(spec.agent_id, spec.trace_id)
    assert td._SLOTS.queue_depth(spec.agent_id) == 0
    assert scheduled == []


def test_failed_handoff_cannot_take_slot_back_from_newer_owner(handoff):
    service, _, spec, scheduled = handoff
    td._SLOTS.claim(spec.agent_id, 'new-owner')
    service._requeue_failed_handoff(spec.agent_id, spec, RuntimeError('prepare failed'),
                                   phase='prepare')
    assert td._SLOTS.get(spec.agent_id) == 'new-owner'
    assert td._SLOTS.queue_depth(spec.agent_id) == 0
    assert scheduled == []


def test_retry_survives_failure_to_close_old_turn_rows(handoff, monkeypatch):
    service, backend, spec, scheduled = handoff
    monkeypatch.setattr(td.turn_lifecycle, 'close_turns_for_trace', _fail)
    service._requeue_failed_handoff(spec.agent_id, spec, RuntimeError('prepare failed'),
                                   phase='prepare')
    assert not td._SLOTS.get(spec.agent_id)
    assert td._SLOTS.queue_depth(spec.agent_id) == 1
    assert scheduled[0][0] == 1.0
    # The retry runs the preserved request, despite cleanup of the failed
    # attempt being unavailable. No real scheduler or backend process starts.
    scheduled[0][1]()
    assert [call[1]['text'] for call in backend.spawned] == [spec.text]


@pytest.mark.parametrize('during_spawn', [False, True])
def test_transient_goal_fence_releases_handoff_for_recovery(handoff, monkeypatch, during_spawn):
    service, backend, spec, scheduled = handoff
    refusal = td.GoalWakeDispatchError(WakeRejected('capacity', 'synthetic capacity'))

    def refuse(*args, **kwargs):
        raise refusal

    if during_spawn:
        monkeypatch.setattr(service, '_spawn_attempt', refuse)
    else:
        patch_shared(monkeypatch, td, '_validate_goal_dispatch', refuse)
    service._resume_and_spawn(spec.agent_id, spec)
    assert not td._SLOTS.get(spec.agent_id)
    assert len(scheduled) == 1
    assert scheduled[0][0] == 1.0
    assert scheduled[0][1] == service.recover_queued
    assert backend.spawned == []


def test_legacy_memory_only_handoff_failure_releases_slot(handoff, monkeypatch):
    service, backend, spec, scheduled = handoff

    def refuse(*args, **kwargs):
        raise td.DispatchError(503, 'synthetic unavailable backend')

    monkeypatch.setattr(service, '_spawn_attempt', refuse)
    service._resume_and_spawn(spec.agent_id, spec)
    assert not td._SLOTS.get(spec.agent_id)
    assert td._SLOTS.queue_depth(spec.agent_id) == 0
    assert backend.spawned == []
    assert scheduled == []
