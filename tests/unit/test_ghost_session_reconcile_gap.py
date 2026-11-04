"""TP-05b (reviewer A's probe, registered by the lead): a fleet snapshot runs
the reconciler for every agent. One that ran while a send was being admitted,
after it bound a new session and before it claimed the slot, saw a bound
session with no transcript and no live work and unbound it, so the next
queued send bound yet another session. An admission in progress is live work."""
from __future__ import annotations

from lib import agents, reconcile, turn_dispatch as td

from test_ghost_session_guard import dispatch_case  # noqa: F401


def test_a_snapshot_during_admission_keeps_the_session_being_launched(dispatch_case, monkeypatch, tmp_path):
    service, backend, agent = dispatch_case
    original = td.team_store.pending_digest
    repairs = []

    def snapshot_before_claim(agent_id):
        if not repairs:
            repairs.append(reconcile.reconcile_agent(agent_id, backend="claude", home=tmp_path))
        return original(agent_id)

    monkeypatch.setattr(td.team_store, "pending_digest", snapshot_before_claim)
    service.dispatch(text="first", requested_session="synthetic", trace_id="t-one",
                     synthesize_audio=False)
    native = backend.spawned[0]["backend_session_id"]
    assert repairs and "ghost_session" not in repairs[0], "the snapshot unbound the session being launched"
    queued = service.dispatch(text="second", requested_session="synthetic", trace_id="t-two",
                              synthesize_audio=False, queue_if_busy=True)
    assert queued.queued and len(backend.spawned) == 1
    assert agents.live_backend_session(agent) == native
