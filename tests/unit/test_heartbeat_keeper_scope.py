"""Peter's answer to AUT-02 (keep_fixed, 2026-11-03): the fixed heartbeat keeps
serving heartbeat-enabled agents outside an adopted keeper's scope. Inside that
scope, pausing or deleting the keeper keeps fixed heartbeats off, and the
schedule does not advertise a fixed heartbeat that will not come."""
from __future__ import annotations

import pytest

from lib import agents, backends, heartbeat, janitor_builtins, janitors


@pytest.fixture
def scoped_keeper(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *_: [])
    janitor_builtins.ensure_builtins(cwd=str(tmp_path))
    ids = {}
    for session in ("included", "outside", "keeper"):
        ids[session] = agents.create_agent(persona=session, voice_id="synthetic",
            cwd=str(tmp_path), session=session, backend="codex")
        agents.record_state(ids[session], "done", {"source": "user"})
    agents.update_agent(ids["included"], heartbeat_enabled=True)
    agents.update_agent(ids["outside"], heartbeat_enabled=True)
    cfg = janitors.create("keeper", template_id="heartbeat-decider",
        scope={"agent_ids": [ids["included"]]})
    cfg = janitors.set_enabled("keeper", cfg["revision"], True)
    return ids, cfg


def _fixed_heartbeats():
    sent = []
    heartbeat.HeartbeatScheduler(send_heartbeat=lambda session, _text: sent.append(session)).run_once()
    return sent


def _next_fixed(ids, session):
    return heartbeat.agent_schedule(agents.get_by_agent_id(ids[session]), now=1000.0)["next_heartbeat_at"]


def test_agents_outside_the_keepers_scope_keep_the_fixed_heartbeat(scoped_keeper):
    ids, _cfg = scoped_keeper
    assert _fixed_heartbeats() == ["outside"]
    assert _next_fixed(ids, "outside") is not None
    assert _next_fixed(ids, "included") is None


@pytest.mark.parametrize("how", ["pause", "delete"])
def test_pausing_or_deleting_the_keeper_keeps_its_scope_off_the_fixed_heartbeat(scoped_keeper, how):
    ids, cfg = scoped_keeper
    cfg = janitors.set_enabled("keeper", cfg["revision"], False)
    if how == "delete":
        assert janitors.remove("keeper", cfg["revision"])
    assert _fixed_heartbeats() == ["outside"]
    assert _next_fixed(ids, "included") is None
