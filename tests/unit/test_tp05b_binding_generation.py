# Reviewer A's probe on 6c7ea274 (WIP 326b8f2d), kept unchanged as the regression.
"""A fresh idle observation cannot authorize closing a subsequently replaced runtime."""
import threading
from types import SimpleNamespace

from lib import agents, backends, reconcile, turn_dispatch as td
from tests.unit.test_ghost_session_guard import dispatch_case  # noqa: F401


def test_new_binding_after_fresh_idle_snapshot_is_not_closed(dispatch_case, monkeypatch, tmp_path):
    service, backend, agent = dispatch_case
    agents.bind_backend_session(agent, 'old-ghost')
    at_bind = threading.Event()
    release = threading.Event()
    errors, native, reads = [], [], []
    original = td.team_store.pending_digest

    def pause_after_new_bind(agent_id):
        native.append(agents.live_backend_session(agent_id))
        assert td._SLOTS.is_admitting(agent_id)
        at_bind.set()
        assert release.wait(5)
        return original(agent_id)

    monkeypatch.setattr(td.team_store, 'pending_digest', pause_after_new_bind)

    def send():
        try:
            service.dispatch(text='new work', requested_session='synthetic', trace_id='new-work', synthesize_audio=False)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=send)

    def runtime_status_reply():
        # The runtime can truthfully report idle, then begin a new admission
        # before the Host receives that point-in-time reply and does its write.
        snapshot = td.runtime_status()
        reads.append(snapshot)
        if len(reads) == 2:
            assert agent not in snapshot.get('admitting', ())
            worker.start()
            assert at_bind.wait(5)
        return snapshot

    monkeypatch.setattr(backends, '_RUNTIME_CLIENT', SimpleNamespace(status=runtime_status_reply))
    monkeypatch.setattr(backends, '_clock', lambda: 0.0)
    backends.invalidate_runtime_status()
    try:
        backends.runtime_status()  # legitimate cached idle result
        repaired = reconcile.reconcile_agent(agent, backend='claude', home=tmp_path)
        assert len(reads) == 2 and at_bind.is_set(), 'control: a fresh read raced the real new admission'
        assert native == ['native-one'] and agents.live_backend_session(agent) == native[0], \
            'repair of old-ghost closed the newer runtime binding after the fresh idle snapshot'
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(5)
        backends.invalidate_runtime_status()
    assert not worker.is_alive() and not errors
