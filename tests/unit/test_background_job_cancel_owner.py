"""AUT-04: cancelling a computer-owned job (a model install, a portrait, a
server update) must not send its cleanup prompt to the default or sticky
agent. Computer-owned jobs have no owning session; the durable cancellation
gate already stops their worker."""
from __future__ import annotations

from types import SimpleNamespace

import server as host
from lib import background_jobs, turn_dispatch as td

from test_drain_pause_parity import World


def test_cancelling_a_computer_job_prompts_no_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    try:
        world = World(tmp_path)
        job = background_jobs.upsert_computer(
            computer_id="synthetic-computer", job_id="synthetic-install",
            title="Synthetic model install")
        handler = host.Handler.__new__(host.Handler)
        handler.server = SimpleNamespace(ctx=world.service.ctx)
        handler._expected_generation = lambda: (True, job["generation"])
        handler._json_ok = lambda payload: payload
        monkeypatch.setattr(host, "TurnDispatchService", lambda _ctx: world.service)

        receipt = handler._handle_background_job_cancel(job["job_id"])

        assert receipt["job"]["status"] == "cancelled"
        world._collect()
        assert world.started == []
    finally:
        td.reset_for_tests()
