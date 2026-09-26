"""Helper agents: creation with a parent, cycles, lifecycle, archive, snapshot."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agent_lifecycle, agents as agents_db, helper_agents, maintenance
from lib.agent_lifecycle import AgentLifecycleError, AgentLifecycleService
from lib.audio_stream import AudioStream
from lib.context import ServerContext, StubSTT
from lib.snapshot import build_agent_snapshot
from lib.tts_engine import FakeTTSEngine


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


def _ctx(tmp_path):
    return SimpleNamespace(agents_path=tmp_path / "unused.json", stream=_Stream(),
                           speak_announcement=lambda *a, **k: None)


def _create(tmp_path, name, **extra):
    data = {"name": name, "cwd": str(tmp_path), "voice_id": "{}",
            "synthesize_audio": False, **extra}
    result = AgentLifecycleService(_ctx(tmp_path)).create(data)
    return agents_db.get_by_session(result.session)


def _agent(tmp_path, session):
    return agents_db.get_by_agent_id(agents_db.create_agent(
        persona=session.title(), voice_id="", cwd=str(tmp_path), session=session))


# ---- creation ------------------------------------------------------------

def test_create_helper_with_parent_session(tmp_path):
    parent = _create(tmp_path, "Boss", session="boss")
    helper = _create(tmp_path, "stream-a", parent="boss", role="helper")
    assert helper["parent_agent_id"] == parent["agent_id"]
    assert helper["role"] == "helper"
    assert helper["helper_state"] == "running"
    assert helper["helper_completed_at"] is None
    assert parent["role"] == "agent" and parent["parent_agent_id"] is None


def test_create_accepts_parent_agent_id_and_plain_child_has_no_helper_state(tmp_path):
    parent = _create(tmp_path, "Boss", session="boss")
    child = _create(tmp_path, "Kid", parent=parent["agent_id"])
    assert child["parent_agent_id"] == parent["agent_id"]
    assert child["role"] == "agent"
    assert child["helper_state"] is None


@pytest.mark.parametrize("extra,status,code", [
    ({"role": "helper"}, 400, "helper_requires_parent"),
    ({"parent": "nobody"}, 404, "parent_not_found"),
    ({"role": "janitor", "parent": "boss"}, 400, "invalid_role"),
    ({"role": "boss"}, 400, "invalid_role"),
])
def test_create_refuses_bad_lineage(tmp_path, extra, status, code):
    _create(tmp_path, "Boss", session="boss")
    with pytest.raises(AgentLifecycleError) as error:
        _create(tmp_path, "Kid", session="kid", **extra)
    assert (error.value.status, error.value.code) == (status, code)
    assert agents_db.get_by_session("kid") is None


def test_fork_records_its_source_as_parent(tmp_path, monkeypatch):
    source = _create(tmp_path, "Source", session="source")
    agents_db.bind_backend_session(source["agent_id"], "conv-source")
    monkeypatch.setattr(agent_lifecycle, "fork_session", lambda sid, cwd: "conv-forked")
    fork = _create(tmp_path, "Forky", session="forky", fork_session_id="conv-source")
    assert fork["parent_agent_id"] == source["agent_id"]
    assert fork["role"] == "agent"


def test_relaunch_refuses_self_parent_and_cycles(tmp_path):
    boss = _create(tmp_path, "Boss", session="boss")
    helper = _create(tmp_path, "Help", session="help", parent="boss", role="helper")
    with pytest.raises(AgentLifecycleError) as error:
        _create(tmp_path, "Boss", replace_sid="boss", parent="boss")
    assert (error.value.status, error.value.code) == (409, "self_parent")
    with pytest.raises(AgentLifecycleError) as error:
        _create(tmp_path, "Boss", replace_sid="boss", parent="help")
    assert (error.value.status, error.value.code) == (409, "parent_cycle")
    assert agents_db.get_by_agent_id(boss["agent_id"])["parent_agent_id"] is None
    assert agents_db.get_by_agent_id(helper["agent_id"])["parent_agent_id"] == boss["agent_id"]


def test_relaunch_keeps_lineage_when_not_named(tmp_path):
    boss = _create(tmp_path, "Boss", session="boss")
    _create(tmp_path, "Help", session="help", parent="boss", role="helper")
    again = _create(tmp_path, "Help", replace_sid="help")
    assert again["parent_agent_id"] == boss["agent_id"]
    assert again["role"] == "helper"


def test_store_refuses_cycles_directly(tmp_path):
    a, b, c = (_agent(tmp_path, s) for s in ("a", "b", "c"))
    agents_db.set_lineage(b["agent_id"], parent_agent_id=a["agent_id"])
    agents_db.set_lineage(c["agent_id"], parent_agent_id=b["agent_id"])
    assert agents_db.ancestors(c["agent_id"]) == [b["agent_id"], a["agent_id"]]
    with pytest.raises(agents_db.ParentRefused, match="parent_cycle"):
        agents_db.set_lineage(a["agent_id"], parent_agent_id=c["agent_id"])
    with pytest.raises(agents_db.ParentRefused, match="self_parent"):
        agents_db.set_lineage(a["agent_id"], parent_agent_id=a["agent_id"])


def test_janitor_rows_carry_the_janitor_role(tmp_path):
    from lib.db import conn
    row = _agent(tmp_path, "hugo")
    agents_db.mark_janitor(conn(), row["agent_id"])
    assert agents_db.get_by_agent_id(row["agent_id"])["role"] == "janitor"
    agents_db.release_from_janitor(conn(), row["agent_id"])
    assert agents_db.get_by_agent_id(row["agent_id"])["role"] == "agent"


def test_resurrected_session_forgets_its_old_lineage(tmp_path):
    boss = _agent(tmp_path, "boss")
    helper = _agent(tmp_path, "help")
    agents_db.set_lineage(helper["agent_id"], parent_agent_id=boss["agent_id"], role="helper")
    agents_db.soft_delete(helper["agent_id"])
    again = agents_db.create_agent(persona="Help", voice_id="", cwd=str(tmp_path), session="help")
    row = agents_db.get_by_agent_id(again)
    assert (row["parent_agent_id"], row["role"], row["helper_state"]) == (None, "agent", None)


# ---- lifecycle -----------------------------------------------------------

def _pair(tmp_path):
    boss = _agent(tmp_path, "boss")
    helper = _agent(tmp_path, "help")
    agents_db.set_lineage(helper["agent_id"], parent_agent_id=boss["agent_id"], role="helper")
    return boss["agent_id"], helper["agent_id"]


def _state(agent_id):
    return agents_db.get_by_agent_id(agent_id)["helper_state"]


def test_report_to_parent_then_retask(tmp_path):
    boss, helper = _pair(tmp_path)
    stream = _Stream()
    assert helper_agents.note_agent_message(stream, sender_agent_id=helper,
                                            target_agent_id=boss) == "reported"
    assert stream.events[-1]["kind"] == "helper-state"
    assert stream.events[-1]["session"] == "help"
    # A second report changes nothing and announces nothing.
    assert helper_agents.note_agent_message(stream, sender_agent_id=helper,
                                            target_agent_id=boss) is None
    assert len(stream.events) == 1
    assert helper_agents.note_agent_message(stream, sender_agent_id=boss,
                                            target_agent_id=helper) == "running"
    assert _state(helper) == "running"


def test_message_to_someone_else_is_not_a_report(tmp_path):
    _boss, helper = _pair(tmp_path)
    other = _agent(tmp_path, "other")["agent_id"]
    assert helper_agents.note_agent_message(None, sender_agent_id=helper,
                                            target_agent_id=other) is None
    assert _state(helper) == "running"


def test_mark_done_by_parent_or_user_and_refusals(tmp_path):
    boss, helper = _pair(tmp_path)
    other = _agent(tmp_path, "other")
    with pytest.raises(helper_agents.HelperStateError) as error:
        helper_agents.mark(None, "help", "done", by="other")
    assert (error.value.status, error.value.code) == (403, "not_parent")
    with pytest.raises(helper_agents.HelperStateError) as error:
        helper_agents.mark(None, "other", "done")
    assert error.value.code == "not_a_helper"
    with pytest.raises(helper_agents.HelperStateError) as error:
        helper_agents.mark(None, "help", "reported")
    assert error.value.code == "invalid_state"
    assert helper_agents.mark(None, "help", "done", by="boss") == "done"
    assert agents_db.get_by_agent_id(helper)["helper_completed_at"]
    assert helper_agents.mark(None, "help", "done") == "done"  # idempotent
    assert helper_agents.mark(None, helper, "running") == "running"
    assert agents_db.get_by_agent_id(helper)["helper_completed_at"] is None
    assert other["role"] == "agent"


def test_deleting_the_parent_abandons_unfinished_helpers_without_cascading(tmp_path):
    boss, helper = _pair(tmp_path)
    finished = _agent(tmp_path, "finished")["agent_id"]
    agents_db.set_lineage(finished, parent_agent_id=boss, role="helper")
    agents_db.apply_helper_event(finished, "marked_done")
    agents_db.soft_delete(boss)
    assert _state(helper) == "abandoned"
    assert agents_db.get_by_agent_id(helper) is not None  # not deleted
    assert _state(finished) == "done"


def test_archiving_the_parent_abandons_its_helpers(tmp_path):
    boss, helper = _pair(tmp_path)
    agents_db.apply_helper_event(helper, "reported")
    agents_db.set_archived(boss, True)
    assert _state(helper) == "abandoned"


def test_maintenance_archives_done_helpers_after_the_grace(tmp_path):
    boss, helper = _pair(tmp_path)
    running = _agent(tmp_path, "running")["agent_id"]
    agents_db.set_lineage(running, parent_agent_id=boss, role="helper")
    agents_db.apply_helper_event(helper, "marked_done", now=1_000)
    policy = maintenance.Policy(helper_archive_grace_ms=500)
    assert maintenance.archive_done_helpers(now_ms=1_400, policy=policy) == 0
    assert maintenance.archive_done_helpers(now_ms=1_500, policy=policy) == 1
    assert agents_db.get_by_agent_id(helper)["archived_at"] is not None
    assert agents_db.get_by_agent_id(running)["archived_at"] is None
    assert agents_db.get_by_agent_id(boss)["archived_at"] is None
    assert maintenance.archive_done_helpers(now_ms=9_000, policy=policy) == 0


def test_maintenance_reads_the_configured_grace(tmp_path, monkeypatch):
    from lib import config
    _boss, helper = _pair(tmp_path)
    agents_db.apply_helper_event(helper, "marked_done", now=0)
    monkeypatch.setattr(config, "_CACHED", config.Config(helper_archive_grace_hours=1))
    hour = 60 * 60 * 1000
    assert maintenance.archive_done_helpers(now_ms=hour - 1) == 0
    assert maintenance.archive_done_helpers(now_ms=hour) == 1


def test_worker_run_once_reports_archived_helpers(tmp_path):
    _boss, helper = _pair(tmp_path)
    agents_db.apply_helper_event(helper, "marked_done", now=0)
    worker = maintenance.MaintenanceWorker(
        audio_dir=tmp_path, policy=maintenance.Policy(helper_archive_grace_ms=0),
        stream=_Stream())
    counts = worker.run_once(checkpoint=False)
    assert counts["helpers_archived"] == 1
    assert worker.stream.events[-1]["kind"] == "helper-state"


# ---- snapshot ------------------------------------------------------------

def test_snapshot_carries_lineage_and_child_counts(tmp_path):
    boss, helper = _pair(tmp_path)
    reported = _agent(tmp_path, "reported")["agent_id"]
    agents_db.set_lineage(reported, parent_agent_id=boss, role="helper")
    agents_db.apply_helper_event(reported, "reported")
    ctx = ServerContext(
        root=tmp_path, static=tmp_path, audio_dir=tmp_path / "audio",
        agents_path=tmp_path / "agents.json", default_session="boss",
        tts=FakeTTSEngine(tmp_path / "audio"), stream=AudioStream(tmp_path / "audio"),
        stt=StubSTT(), roster_names=())
    rows = {row["agent_id"]: row for row in build_agent_snapshot(ctx)["agents"]}
    assert {k: rows[boss][k] for k in (
        "parent_agent_id", "role", "helper_state", "child_count", "running_children")} == {
        "parent_agent_id": None, "role": "agent", "helper_state": None,
        "child_count": 2, "running_children": 1}
    assert {k: rows[helper][k] for k in (
        "parent_agent_id", "role", "helper_state", "child_count", "running_children")} == {
        "parent_agent_id": boss, "role": "helper", "helper_state": "running",
        "child_count": 0, "running_children": 0}
    assert rows[reported]["helper_state"] == "reported"
