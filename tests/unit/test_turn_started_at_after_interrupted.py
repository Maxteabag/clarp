"""When the running work began, for the apps' "working for 4m 12s" timer (issue 354).

``turn_started_at`` was the first busy state after the last done, idle or
stopped. A turn that ends as interrupted (a Stop, a usage limit, a runner
exit, a server restart) is none of those, so the turn after it was counted
from the interrupted turn's start. Measured read-only on one Host over three
days: 356 of 1,873 turn starts, often by hours. Found by the conformance run
of the Host in Rust (server-rs/docs/reference-defects.md, R11).
"""
from __future__ import annotations

import json

import pytest

from lib import agents, backends, db
from lib.snapshot import build_agent_snapshot
from tests.unit.test_snapshot import _model_avatar_ctx

T = 1_794_700_000_000   # some morning in November 2026, epoch ms


def _agent(tmp_path, name="Felix", backend="codex"):
    return agents.create_agent(persona=name, voice_id="V", cwd=str(tmp_path),
                               session=name.lower(), backend=backend, model="")


def _states(agent_id, *states):
    for kind, ts in states:
        db.conn().execute("INSERT INTO state_log(agent_id, kind, ts, detail) VALUES(?,?,?,?)",
                          (agent_id, kind, ts, json.dumps({})))


def _both_clocks(agent_id):
    """The per-agent query and the batched one the snapshot reads."""
    return agents.turn_started_at(agent_id), int(agents.dashboard_states()[agent_id]["turn_started_at"] or 0)


def test_the_turn_after_an_interrupted_one_starts_at_its_own_first_state(tmp_path):
    agent_id = _agent(tmp_path)
    _states(agent_id, ("thinking", T), ("tool", T + 60_000), ("interrupted", T + 120_000),
            ("thinking", T + 6 * 3_600_000), ("tool", T + 6 * 3_600_000 + 5_000))

    assert _both_clocks(agent_id) == (T + 6 * 3_600_000, T + 6 * 3_600_000)


@pytest.mark.parametrize("end", ["done", "idle", "stopped", "interrupted"])
def test_every_way_a_turn_ends_starts_the_next_one_anew(tmp_path, end):
    agent_id = _agent(tmp_path)
    _states(agent_id, ("thinking", T), (end, T + 1_000), ("thinking", T + 9_000), ("compacting", T + 9_500))

    assert _both_clocks(agent_id) == (T + 9_000, T + 9_000)


@pytest.mark.parametrize("inside", ["waiting", "background", "spawned"])
def test_a_state_that_ends_no_turn_does_not_restart_the_clock(tmp_path, inside):
    agent_id = _agent(tmp_path)
    _states(agent_id, ("done", T - 1_000), ("thinking", T), (inside, T + 1_000), ("tool", T + 9_000))

    assert _both_clocks(agent_id) == (T, T)


def test_an_interrupted_turn_with_no_turn_after_it_has_no_running_start(tmp_path):
    agent_id = _agent(tmp_path)
    _states(agent_id, ("thinking", T), ("interrupted", T + 1_000))

    assert _both_clocks(agent_id) == (0, 0)


def _snapshot_row(tmp_path, persona="Felix"):
    snap = build_agent_snapshot(_model_avatar_ctx(tmp_path, tmp_path / "static"))
    return next(row for row in snap["agents"] if row["persona"] == persona)


def test_more_sent_to_a_working_agent_does_not_restart_its_clock(tmp_path, monkeypatch):
    """A message to a working agent opens a new turn row and writes no end
    state: 397 of 2,899 working stretches on one Host in four days, the
    newest row a median 19 minutes after the work began. The timer says how
    long the agent has been working, so it stays (review of PR 357)."""
    agent_id = _agent(tmp_path)
    monkeypatch.setattr(backends, "active_handles",
                        lambda backend, aid: [object()] if aid == agent_id else [])
    _states(agent_id, ("done", T - 60_000), ("thinking", T), ("tool", T + 5_000),
            ("thinking", T + 19 * 60_000), ("tool", T + 75 * 60_000))
    for trace, at in (("first", T - 400), ("more", T + 19 * 60_000), ("and more", T + 75 * 60_000)):
        db.conn().execute("INSERT INTO turns(agent_id,source,trace_id,started_at) VALUES(?,'user',?,?)",
                          (agent_id, trace, at))

    assert _both_clocks(agent_id) == (T, T)
    assert _snapshot_row(tmp_path)["turn_started_at"] == T


def test_work_that_has_no_row_yet_is_not_given_the_turn_before(tmp_path, monkeypatch):
    agent_id = _agent(tmp_path)
    monkeypatch.setattr(backends, "active_handles",
                        lambda backend, aid: [object()] if aid == agent_id else [])
    db.conn().execute("INSERT INTO turns(agent_id,source,trace_id,started_at) VALUES(?,'user','before',?)",
                      (agent_id, T - 12 * 60_000))
    _states(agent_id, ("thinking", T - 12 * 60_000), ("done", T - 11 * 60_000), ("thinking", T))

    assert _snapshot_row(tmp_path)["turn_started_at"] == T


def test_a_running_turn_with_no_row_of_its_own_is_counted_from_its_states(tmp_path, monkeypatch):
    agent_id = _agent(tmp_path)
    monkeypatch.setattr(backends, "active_handles",
                        lambda backend, aid: [object()] if aid == agent_id else [])
    _states(agent_id, ("thinking", T), ("interrupted", T + 1_000), ("thinking", T + 7_000))

    assert _snapshot_row(tmp_path)["turn_started_at"] == T + 7_000


def test_an_agent_at_rest_is_not_given_an_open_turn_s_start(tmp_path, monkeypatch):
    """A turn row left open by a crash must not make an idle agent look as if
    it had been working since then."""
    agent_id = _agent(tmp_path)
    monkeypatch.setattr(backends, "active_handles", lambda backend, aid: [])
    _states(agent_id, ("thinking", T), ("done", T + 1_000))
    db.conn().execute("INSERT INTO turns(agent_id,source,trace_id,started_at) VALUES(?,'user','stale',?)",
                      (agent_id, T - 86_400_000))

    assert _snapshot_row(tmp_path)["turn_started_at"] == 0
