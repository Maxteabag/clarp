"""Schema 118: an agent's Codex provider profile lives on its row.

``agents.provider_profile`` is written by the creating INSERT, a trigger makes
it immutable, every reader takes it from the row, and a profile Clarp does not
know never runs (it never falls back to the CLI's OpenAI account).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import codex_app_server, codex_profiles, db, db_migrations, db_schema
from lib import turn_dispatch as _td

pytestmark = pytest.mark.usefixtures("fake_codex_profile")


def _eu(session="eu", profile="example-eu"):
    agent_id = agents_db.create_agent(persona=session.title(), voice_id="V", cwd="/tmp",
                                      session=session, backend="codex",
                                      provider_profile=profile)
    agents_db.start_runtime(agent_id, session)
    return agent_id


def test_the_profile_is_a_column_written_by_the_insert_and_immutable():
    eu = _eu()
    plain = agents_db.create_agent(persona="Plain", voice_id="V", cwd="/tmp",
                                   session="plain", backend="codex")

    assert agents_db.get_by_agent_id(eu)["provider_profile"] == "example-eu"
    assert agents_db.get_by_agent_id(plain)["provider_profile"] == ""
    assert db_schema._SCHEMA_VERSION >= 118
    for value in ("", "other-eu"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.conn().execute("UPDATE agents SET provider_profile = ? WHERE agent_id = ?",
                              (value, eu))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.conn().execute("UPDATE agents SET provider_profile = 'example-eu' "
                          "WHERE agent_id = ?", (plain,))
    # No settings row carries it any more.
    assert not db.conn().execute(
        "SELECT 1 FROM settings WHERE key LIKE 'agent.provider_profile.%'").fetchall()


def test_resurrecting_an_eu_session_without_its_profile_fails_and_stays_deleted():
    eu = _eu()
    agents_db.soft_delete(eu)

    with pytest.raises(sqlite3.IntegrityError):
        agents_db.create_agent(persona="Eu", voice_id="V", cwd="/tmp", session="eu",
                               backend="codex")
    assert agents_db.get_by_session("eu") is None
    assert agents_db.create_agent(persona="Eu", voice_id="V", cwd="/tmp", session="eu",
                                  backend="codex", provider_profile="example-eu") == eu


def test_schema_118_backfills_from_the_settings_rows_additively(tmp_path):
    path = tmp_path / "v117.sqlite"
    con = sqlite3.connect(path, isolation_level=None)
    con.row_factory = sqlite3.Row
    db_migrations._create_schema(con)
    con.execute("DROP TRIGGER agents_provider_profile_immutable")
    con.execute("DROP INDEX idx_agents_provider_profile")
    con.execute("ALTER TABLE agents DROP COLUMN provider_profile")
    for agent_id, session in (("a-eu", "eu"), ("a-plain", "plain")):
        con.execute("INSERT INTO agents (agent_id, persona, voice_id, cwd, session, "
                    "backend, created_at) VALUES (?, ?, 'V', '/tmp', ?, 'codex', 1)",
                    (agent_id, session.title(), session))
    con.execute("INSERT INTO settings (key, value, updated_at) VALUES "
                "('agent.provider_profile.a-eu', 'example-eu', 1)")
    con.execute("PRAGMA user_version = 117")

    db_migrations._migrate(con)

    rows = {row["agent_id"]: row["provider_profile"]
            for row in con.execute("SELECT agent_id, provider_profile FROM agents")}
    assert rows == {"a-eu": "example-eu", "a-plain": ""}
    # Additive: the old settings row is left alone (and no longer read).
    assert con.execute("SELECT value FROM settings WHERE key = "
                       "'agent.provider_profile.a-eu'").fetchone()[0] == "example-eu"
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE agents SET provider_profile = '' WHERE agent_id = 'a-eu'")
    assert con.execute("PRAGMA user_version").fetchone()[0] == db_schema._SCHEMA_VERSION


def test_an_unknown_profile_fails_closed_and_never_runs_on_openai(tmp_path, monkeypatch):
    """A row whose profile is not (or no longer) configured: every turn is
    refused, no Codex process starts, and side channels still treat it as EU."""
    db.conn().execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, backend, "
        "created_at, provider_profile) VALUES ('a-ghost', 'Ghost', 'V', '/tmp', 'ghost', "
        "'codex', 1, 'gone-eu')")
    agents_db.start_runtime("a-ghost", "ghost")
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    spawned = []
    registry = SimpleNamespace(
        CODEX="codex", normalize=lambda b: b or "claude", interrupt=lambda *a: 0,
        active_handles=lambda *a: [], spawn_turn=lambda backend, **kw: spawned.append(kw))
    service = _td.TurnDispatchService(
        SimpleNamespace(default_session="ghost", agents_path=tmp_path / "x.json",
                        stream=SimpleNamespace(broadcast=lambda _e: None)),
        backend_registry=registry, home=tmp_path, retry_scheduler=lambda _d, fn: fn())
    popen = []
    monkeypatch.setattr(codex_app_server.subprocess, "Popen",
                        lambda *a, **k: popen.append(a) or pytest.fail("a process started"))

    with pytest.raises(_td.DispatchError) as refused:
        service.dispatch(text="hi", requested_session="ghost", trace_id="t",
                         synthesize_audio=False)
    assert refused.value.status == 409
    with pytest.raises(codex_profiles.ProfileUnavailable):
        codex_app_server.spawn_turn(text="hi", cwd=Path("/tmp"), session="ghost",
                                    agent_id="a-ghost", is_new_session=True,
                                    provider_profile="gone-eu")
    assert spawned == [] and popen == []
    assert codex_profiles.is_profiled(agents_db.get_by_agent_id("a-ghost"))


def test_the_snapshot_reads_each_profile_from_the_rows_it_loaded(tmp_path, monkeypatch):
    """(5) No per-agent query, by agent id or by its bound session: every
    agent's profile (and its transcript home) comes from the row the snapshot
    already loaded."""
    from lib.snapshot import build_agent_snapshot
    monkeypatch.setenv("HOME", str(tmp_path))
    ids, sids = [], []
    for i in range(6):
        agent_id = agents_db.create_agent(
            persona=f"P{i}", voice_id="", cwd="/tmp", session=f"p{i}", backend="codex",
            **({"provider_profile": "example-eu"} if i % 2 else {}))
        agents_db.start_runtime(agent_id, f"p{i}")
        agents_db.bind_backend_session(agent_id, f"thread-{i}-0b5e0a8c")
        ids.append(agent_id)
        sids.append(f"thread-{i}-0b5e0a8c")
    build_agent_snapshot(None)
    statements = []
    db.conn().set_trace_callback(statements.append)
    try:
        rows = build_agent_snapshot(None)["agents"]
    finally:
        db.conn().set_trace_callback(None)

    assert {row["provider_profile"] for row in rows} == {"", "example-eu"}
    per_agent = [s for s in statements if any(key in s for key in ids + sids)]
    assert per_agent == []
