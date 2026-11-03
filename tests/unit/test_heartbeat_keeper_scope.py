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


def test_a_new_scoped_adoption_keeps_an_adoption_made_before_scopes_were_recorded(scoped_keeper, tmp_path):
    """AUT-02s review: a Host that adopted a keeper before its scope was
    recorded (the latch alone) keeps that adoption when a second, scoped
    keeper is enabled; the outside agent stays off the fixed heartbeat."""
    from lib import settings_store
    ids, cfg = scoped_keeper
    cfg = janitors.configure("keeper", cfg["revision"], scope={})
    janitors.set_enabled("keeper", cfg["revision"], False)
    settings_store.set_text("heartbeat.janitor_adopted_scopes", "[]")   # adopted before records
    outside = agents.get_by_agent_id(ids["outside"])
    assert heartbeat.keeper_owns_heartbeat(outside)

    agents.create_agent(persona="second", voice_id="synthetic", cwd=str(tmp_path),
                        session="second", backend="codex")
    second = janitors.create("second", template_id="heartbeat-decider",
                             scope={"agent_ids": [ids["included"]]})
    janitors.set_enabled("second", second["revision"], True)

    assert heartbeat.keeper_owns_heartbeat(outside)
    assert _fixed_heartbeats() == []


def test_a_first_adoption_on_a_fresh_host_records_only_its_scope(tmp_path, monkeypatch):
    from lib import settings_store
    monkeypatch.setattr(backends, "active_handles", lambda *_: [])
    janitor_builtins.ensure_builtins(cwd=str(tmp_path))
    ids = {}
    for session in ("included", "outside", "keeper"):
        ids[session] = agents.create_agent(persona=session, voice_id="synthetic",
            cwd=str(tmp_path), session=session, backend="codex")
        agents.record_state(ids[session], "done", {"source": "user"})
    agents.update_agent(ids["outside"], heartbeat_enabled=True)
    assert not settings_store.get_bool("heartbeat.janitor_adopted")
    cfg = janitors.create("keeper", template_id="heartbeat-decider",
        scope={"agent_ids": [ids["included"]]})
    janitors.set_enabled("keeper", cfg["revision"], True)
    assert _fixed_heartbeats() == ["outside"]


def test_the_first_record_keeps_a_legacy_adoption_after_its_keeper_is_deleted(scoped_keeper, tmp_path):
    """AUT-02s review 2: the first record must store the adoption made before
    records, even when the new keeper's scope is already among it; deleting
    the old unscoped keeper afterwards must not hand the outside agent back to
    the fixed heartbeat. The built-in keeper is scoped so its default cannot
    stand in for the lost record."""
    from lib import settings_store
    ids, cfg = scoped_keeper
    builtin = next(j for j in janitors.list_janitors(include_runtime=False)
                   if j["template_id"] == "heartbeat-decider" and j["session"] != "keeper")
    janitors.configure(builtin["session"], builtin["revision"], scope={"agent_ids": [ids["included"]]})
    cfg = janitors.configure("keeper", cfg["revision"], scope={})
    cfg = janitors.set_enabled("keeper", cfg["revision"], False)
    settings_store.set_text("heartbeat.janitor_adopted_scopes", "[]")   # adopted before records

    agents.create_agent(persona="second", voice_id="synthetic", cwd=str(tmp_path),
                        session="second", backend="codex")
    second = janitors.create("second", template_id="heartbeat-decider",
                             scope={"agent_ids": [ids["included"]]})
    janitors.set_enabled("second", second["revision"], True)
    assert janitors.remove("keeper", cfg["revision"])

    assert heartbeat.keeper_owns_heartbeat(agents.get_by_agent_id(ids["outside"]))
    assert _fixed_heartbeats() == []


def test_a_heartbeat_pass_reads_the_keepers_once_however_many_agents(scoped_keeper, tmp_path, monkeypatch):
    """AUT-02s bench (lead): the pass resolved the keepers again for every due
    agent, re-reading every Janitor config each time. It reads them once, and
    an agent a keeper serves is dropped before its state is read."""
    ids, _cfg = scoped_keeper
    for n in range(12):
        agent_id = agents.create_agent(persona=f"extra{n}", voice_id="synthetic",
                                       cwd=str(tmp_path), session=f"extra{n}", backend="codex")
        agents.record_state(agent_id, "done", {"source": "user"})
        agents.update_agent(agent_id, heartbeat_enabled=True)
    listed = []
    real = janitors.list_janitors
    monkeypatch.setattr(janitors, "list_janitors", lambda **kw: listed.append(1) or real(**kw))
    snapshots = []
    real_snapshot = heartbeat._gather_snapshot
    monkeypatch.setattr(heartbeat, "_gather_snapshot",
                        lambda agent, now: snapshots.append(agent["agent_id"]) or real_snapshot(agent, now))

    _fixed_heartbeats()

    assert len(listed) == 1
    assert ids["included"] not in snapshots
