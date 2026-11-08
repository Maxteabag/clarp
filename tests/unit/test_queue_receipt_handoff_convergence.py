"""Receipt maintenance must not consume a proven never-launched handoff.

Real drain/account/queue owners supply the evidence; only their existing
provider and selector fixtures are synthetic. No fabricated positive state.
"""
from __future__ import annotations

import pytest

from lib import db, reconcile, turn_queue
from test_drain_recovery_transfer import (  # noqa: F401 - canonical fixture
    Pool, _bound_agent, _runtime, _send, selector_configured,
)
from test_graceful_release_drain import _fresh_runtime_memory, _service  # noqa: F401


def _transfer(tmp_path, monkeypatch, *, queued, origin):
    pool = Pool(monkeypatch)
    agent = _bound_agent(tmp_path)
    service, provider = _service(tmp_path)
    if queued:
        _send(service, "first")
        _send(service, "work", queue=True, origin=origin)
    pool.coordinator.recovering = True
    pool.coordinator.parked_since = 40.0
    pool.coordinator.command = ("selector",)
    if queued:
        provider.finish("first")
    else:
        _send(service, "work", origin=origin)
    assert pool.coordinator.status()["waiting"] == [agent]
    assert "work" not in provider.texts()
    service.begin_admission_fence()
    _send(service, "next")
    return agent, provider, _runtime(tmp_path, service)


@pytest.mark.parametrize("origin", ["user", "agent"])
@pytest.mark.parametrize("queued", [False, True], ids=["new-park", "taken-over-row"])
@pytest.mark.parametrize("stopped", [False, True], ids=["normal", "keep-stopped"])
def test_periodic_receipt_maintenance_keeps_never_launched_transfer(
        tmp_path, monkeypatch, selector_configured, origin, queued, stopped):
    agent, provider, runtime = _transfer(tmp_path, monkeypatch, queued=queued, origin=origin)
    try:
        assert runtime.seal_if_drained() == {}
        if stopped:
            assert runtime.dispatch_request("begin_stop", {
                "agent_id": agent, "backend": "claude", "strict": False,
                "hold": False})["ok"]
    finally:
        runtime.server_close()
    # The same real queue transition used during boot rehydrates the held row.
    turn_queue.requeue_parked(agent)
    work_id = "work" if queued else "recovery-park-t-work"
    before = dict(db.conn().execute(
        "SELECT * FROM queued_turns WHERE queue_id=?", (work_id,)).fetchone())
    assert before["status"] == "queued" and before["text"] == "work"
    paused = turn_queue.is_paused(agent)
    reconcile.end_idle_agent_receipts({"provider_turns": ["<unknown>"]})
    after = dict(db.conn().execute(
        "SELECT * FROM queued_turns WHERE queue_id=?", (work_id,)).fetchone())
    assert after == before, "administrative handoff was consumed as backend execution"
    assert [row["queue_id"] for row in turn_queue.pending_for_dispatch()] == [
        work_id, "drain-park-t-next"]
    assert turn_queue.is_paused(agent) is paused
    assert (not stopped) or paused
    assert "work" not in provider.texts() and "next" not in provider.texts()
