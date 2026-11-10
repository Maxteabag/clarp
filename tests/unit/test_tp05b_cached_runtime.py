"""TP-05b round 2 (reviewer A on 1d27dbc4): the Host's reconciler, observing
an external runtime through its cached status, must not unbind the session of
a send the runtime is admitting. A would-unbind decision is revalidated with a
fresh status read, and an unreadable status keeps the binding (fail closed).
The first scenario is reviewer A's probe (WIP 3ba123d8)."""
from types import SimpleNamespace

from lib import agents, backends, reconcile, turn_dispatch as td
from tests.unit.test_ghost_session_guard import dispatch_case  # noqa: F401


def test_cached_runtime_status_cannot_hide_an_in_progress_admission(dispatch_case, monkeypatch, tmp_path):
    service, backend, agent = dispatch_case
    calls = []

    def status():
        calls.append(True)
        return td.runtime_status()

    monkeypatch.setattr(backends, '_RUNTIME_CLIENT', SimpleNamespace(status=status))
    monkeypatch.setattr(backends, '_clock', lambda: 0.0)  # remain within the legitimate cache window
    backends.invalidate_runtime_status()
    assert agent not in backends.runtime_status().get('admitting', ())
    original = td.team_store.pending_digest
    repairs = []

    def snapshot_before_claim(agent_id):
        if not repairs:
            assert td._SLOTS.is_admitting(agent_id), 'control: runtime has the new admission marker'
            assert len(calls) == 1, 'control: only the pre-admission idle status is cached'
            repairs.append(reconcile.reconcile_agent(agent_id, backend='claude', home=tmp_path))
            assert len(calls) == 2, 'the would-unbind path reads the runtime fresh'
        return original(agent_id)

    monkeypatch.setattr(td.team_store, 'pending_digest', snapshot_before_claim)
    try:
        service.dispatch(text='first', requested_session='synthetic', trace_id='t-one', synthesize_audio=False)
        native = backend.spawned[0]['backend_session_id']
        queued = service.dispatch(text='second', requested_session='synthetic', trace_id='t-two',
                                  synthesize_audio=False, queue_if_busy=True)
        assert queued.queued and len(backend.spawned) == 1
        assert agents.live_backend_session(agent) == native, 'cached idle status still allowed INV2 to unbind the new session'
    finally:
        backends.invalidate_runtime_status()


def test_an_unreadable_runtime_keeps_the_binding(monkeypatch, tmp_path):
    agent = agents.create_agent(persona='Ghost', voice_id='v', cwd=str(tmp_path),
                                session='ghost', backend='claude')
    agents.start_runtime(agent, 'ghost')
    agents.bind_backend_session(agent, 'no-transcript-session')
    monkeypatch.setattr(backends, '_RUNTIME_CLIENT', SimpleNamespace(status=lambda: {}))

    def runtime_status(*, max_age=None):
        if max_age == 0:
            raise ConnectionError('runtime unavailable')
        return {}

    monkeypatch.setattr(backends, 'runtime_status', runtime_status)
    monkeypatch.setattr(reconcile, '_runtime_slot_may_have_leaked', lambda agent_id: False)
    repaired = reconcile.reconcile_agent(agent, backend='claude', home=tmp_path)
    assert 'ghost_session' not in repaired
    assert agents.live_backend_session(agent) == 'no-transcript-session'


def test_a_ghost_decision_never_closes_a_binding_made_after_it(monkeypatch, tmp_path):
    """Reviewer A on 6c7ea274: a send can end the ghost and bind a new
    session after the fresh idle read; the old decision closes only the
    binding it inspected."""
    agent = agents.create_agent(persona='Rebind', voice_id='v', cwd=str(tmp_path),
                                session='rebind', backend='claude')
    agents.start_runtime(agent, 'rebind')
    agents.bind_backend_session(agent, 'old-ghost')
    monkeypatch.setattr(backends, '_RUNTIME_CLIENT', SimpleNamespace(status=lambda: {}))
    monkeypatch.setattr(backends, 'runtime_status', lambda *, max_age=None: {})
    monkeypatch.setattr(reconcile, '_runtime_slot_may_have_leaked', lambda agent_id: False)
    owns = reconcile._runtime_owns_now

    def a_send_rebinds_after_the_fresh_read(agent_id):
        result = owns(agent_id)
        agents.end_current_runtime(agent_id)          # the send's own ghost reset
        agents.bind_backend_session(agent_id, 'native-one')
        return result

    monkeypatch.setattr(reconcile, '_runtime_owns_now', a_send_rebinds_after_the_fresh_read)
    repaired = reconcile.reconcile_agent(agent, backend='claude', home=tmp_path)
    assert agents.live_backend_session(agent) == 'native-one'
    assert 'ghost_session' not in repaired
