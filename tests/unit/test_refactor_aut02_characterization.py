"""Current AUT-02 product questions, not assertions of intended future design.

Lead ruled adoption permanent on pause per docs/janitor-autonomy.md. These
passing cases retain observations for the three separate open product questions;
they must change if Peter chooses a different contract.
"""
from __future__ import annotations

import pytest

from lib import agents, backends, heartbeat, janitor_builtins, janitors, settings_store


@pytest.fixture
def adopted(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *_: [])
    janitor_builtins.ensure_builtins(cwd=str(tmp_path))
    ids = {}
    for session in ("included", "outside", "keeper"):
        ids[session] = agents.create_agent(persona=session, voice_id="synthetic",
            cwd=str(tmp_path), session=session, backend="codex")
        agents.record_state(ids[session], "done", {"source": "user"})
    agents.update_agent(ids["outside"], heartbeat_enabled=True)
    cfg = janitors.create("keeper", template_id="heartbeat-decider",
        scope={"agent_ids": [ids["included"]]})
    cfg = janitors.set_enabled("keeper", cfg["revision"], True)
    assert settings_store.get_bool("heartbeat.janitor_adopted")
    return ids, cfg


def test_scoped_adoption_also_disables_rigid_heartbeat_outside_scope(adopted):
    ids, _cfg = adopted
    assert janitor_builtins.resolve("heartbeat-decider", target_agent_id=ids["outside"]) is None
    sent = []
    scheduler = heartbeat.HeartbeatScheduler(send_heartbeat=lambda *args: sent.append(args))
    assert scheduler.run_once() == 0
    assert sent == []


def test_removing_non_builtin_decider_retains_global_adoption_latch(adopted):
    _ids, cfg = adopted
    cfg = janitors.set_enabled("keeper", cfg["revision"], False)
    assert janitors.remove("keeper", cfg["revision"])
    assert settings_store.get_bool("heartbeat.janitor_adopted")
    assert janitor_builtins.resolve("heartbeat-decider") is None
    assert heartbeat.HeartbeatScheduler(send_heartbeat=lambda *_: pytest.fail("rigid heartbeat fired")).run_once() == 0


def test_schedule_still_advertises_next_heartbeat_while_rigid_loop_disabled(adopted):
    ids, _cfg = adopted
    agent = agents.get_by_agent_id(ids["outside"])
    schedule = heartbeat.agent_schedule(agent, now=1000.0)
    assert schedule["enabled"] is True
    assert schedule["next_heartbeat_at"] is not None
    assert heartbeat.HeartbeatScheduler(send_heartbeat=lambda *_: pytest.fail("rigid heartbeat fired")).run_once() == 0
