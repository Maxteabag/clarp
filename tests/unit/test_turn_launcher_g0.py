"""Fail-safe launch ownership and backend-session binding failures."""
from types import SimpleNamespace

import pytest

from lib import agents, backends, turn_dispatch as td
from tests.patching import patch_shared
from tests.unit.test_turn_handoff_g0 import handoff, _fail


def test_unknown_busy_state_is_not_permission_to_launch(handoff, monkeypatch):
    service, _, spec, _ = handoff
    td._SLOTS.release(spec.agent_id, spec.trace_id)
    monkeypatch.setattr(agents, 'is_busy', _fail)
    assert service._turn_in_flight(spec.agent_id) is True


def test_backend_session_binding_failure_refuses_dispatch(handoff, monkeypatch):
    service, backend, spec, _ = handoff
    adapter = backends.by_id(spec.backend)
    monkeypatch.setattr(adapter, 'bind_new_session', _fail)
    with pytest.raises(td.DispatchError, match='could not bind backend session') as caught:
        service.dispatch(text='synthetic request', requested_session=spec.session,
                         trace_id='new-trace', synthesize_audio=False)
    assert caught.value.status == 500
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert backend.spawned == []


def test_superseded_spawn_attempt_does_not_invoke_backend(handoff):
    service, backend, spec, _ = handoff
    td._SLOTS.claim(spec.agent_id, 'new-owner')
    assert service._spawn_attempt(spec, attempt=1) is False
    assert backend.spawned == []
    assert td._SLOTS.get(spec.agent_id) == 'new-owner'


def test_unowned_handle_termination_failure_is_reported(handoff, monkeypatch):
    service, _, spec, _ = handoff
    reports = []
    patch_shared(monkeypatch, td, 'log_exception',
                 lambda event, error, **kw: reports.append((event, error, kw)))
    failure = RuntimeError('synthetic termination failure')
    calls = []

    def terminate():
        calls.append('terminate')
        raise failure

    service._interrupt_unowned(spec, SimpleNamespace(terminate=terminate))
    assert calls == ['terminate']
    assert reports == [('spawnUnownedInterruptFail', failure, {'detail': spec.agent_id})]


def test_init_storage_failure_interrupts_backend_and_result_releases_slot(handoff, monkeypatch):
    service, backend, spec, scheduled = handoff
    service._resume_and_spawn(spec.agent_id, spec)
    call = backend.spawned[0][1]
    monkeypatch.setattr(agents, 'bind_backend_session', _fail)
    assert call['on_session_init']('synthetic-backend-id') is False
    assert backend.interrupted == [('claude', spec.agent_id)]
    call['on_result']({'duration_ms': 1})
    assert not td._SLOTS.get(spec.agent_id)
    assert scheduled == []
