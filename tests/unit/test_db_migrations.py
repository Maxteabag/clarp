"""Schema creation and upgrade tests for the SQLite state store."""
from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import db  # noqa: E402


def _connect(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def _fresh(path: pathlib.Path) -> sqlite3.Connection:
    con = _connect(path)
    db._migrate(con)
    return con


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _names(con: sqlite3.Connection, kind: str) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def test_fresh_database_is_stamped_at_the_current_version(tmp_path):
    con = _fresh(tmp_path / "fresh.sqlite")
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    assert "agents" in _names(con, "table")
    assert con.execute(
        "SELECT revision FROM message_clock WHERE singleton = 0"
    ).fetchone()[0] == 0


def test_migrate_is_idempotent_on_a_current_database(tmp_path):
    path = tmp_path / "state.sqlite"
    con = _fresh(path)
    before = {r[0]: r[1] for r in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")}
    db._migrate(con)
    after = {r[0]: r[1] for r in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")}
    assert before == after


def test_pre_release_databases_are_refused(tmp_path):
    path = tmp_path / "old.sqlite"
    con = _connect(path)
    con.execute("CREATE TABLE agents (agent_id TEXT PRIMARY KEY)")
    con.execute(f"PRAGMA user_version = {db._MIN_UPGRADABLE_VERSION - 1}")
    con.commit()
    with pytest.raises(RuntimeError, match="schema version"):
        db._migrate(con)


def _shape_as_v61(con: sqlite3.Connection) -> None:
    """Rebuild the pre-v62 shape on top of a current database."""
    con.executescript("""
        DROP INDEX idx_messages_trace;
        ALTER TABLE messages DROP COLUMN trace_id;
        ALTER TABLE dream_runs DROP COLUMN seed_strategy;
        ALTER TABLE dream_runs DROP COLUMN context_dose;
        ALTER TABLE dream_runs DROP COLUMN seed_material;
        ALTER TABLE agents DROP COLUMN voice_verbosity;
        ALTER TABLE dream_threads DROP COLUMN killed_reason;
        ALTER TABLE dream_threads DROP COLUMN origin_note;
        DROP TABLE vocab_runs;
        DROP TABLE vocab_assignments;
        DROP TABLE vocab_profile_packs;
        DROP TABLE vocab_profiles;
        DROP TABLE vocab_terms;
        DROP TABLE vocab_packs;
        DROP TABLE oracle_delegations;
        ALTER TABLE tts_queue ADD COLUMN mode TEXT NOT NULL DEFAULT 'pwa';
        ALTER TABLE agents ADD COLUMN heartbeat_interval_sec INTEGER NOT NULL DEFAULT 1800;
        ALTER TABLE agents ADD COLUMN heartbeat_backoff_strategy TEXT NOT NULL DEFAULT 'exponential';
        ALTER TABLE agents ADD COLUMN heartbeat_backoff_cap_sec INTEGER NOT NULL DEFAULT 14400;
        ALTER TABLE agents ADD COLUMN heartbeat_dormant_after_noops INTEGER NOT NULL DEFAULT 5;
        CREATE TABLE diagnostic_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, event TEXT NOT NULL, trace_id TEXT
        );
        CREATE VIEW events AS SELECT * FROM diagnostic_events;
        CREATE VIEW untraced_events AS
            SELECT * FROM events WHERE trace_id IS NULL OR trace_id = '';
        INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at,
                            mcp_servers, avatar_path)
            VALUES ('a1', 'Ada', 'v', '/tmp', 'ada', 1,
                    '["teams-local"]', '/home/u/.local/share/claude-pwa/avatars/ada.png'),
                   ('a2', 'Lin', 'v', '/tmp', 'lin', 2,
                    '{"configured": true, "servers": []}', ''),
                   ('a3', 'Mo', 'v', '/tmp', 'mo', 3, '[]', '');
        INSERT INTO settings (key, value, updated_at) VALUES
            ('transcription.guidance.mode', 'agent_names', 1),
            ('telemetry.state_events_retired.v1', 'true', 1),
            ('transcription.guidance.glossary', 'SwiftUI', 1);
        PRAGMA user_version = 61;
    """)


def test_v62_drops_pre_release_shapes_and_normalizes_rows(tmp_path):
    path = tmp_path / "v61.sqlite"
    con = _fresh(path)
    _shape_as_v61(con)
    con.close()

    upgraded = _connect(path)
    db._migrate(upgraded)

    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    assert "diagnostic_events" not in _names(upgraded, "table")
    assert not {"events", "untraced_events"} & _names(upgraded, "view")
    assert "mode" not in _columns(upgraded, "tts_queue")
    assert not {"heartbeat_interval_sec", "heartbeat_backoff_strategy",
                "heartbeat_backoff_cap_sec",
                "heartbeat_dormant_after_noops"} & _columns(upgraded, "agents")

    rows = {r["agent_id"]: r for r in upgraded.execute(
        "SELECT agent_id, mcp_servers, avatar_path FROM agents")}
    assert rows["a1"]["mcp_servers"] == '{"configured":true,"servers":["teams-local"]}'
    assert rows["a1"]["avatar_path"] == "/home/u/.local/share/clarp/avatars/ada.png"
    assert rows["a2"]["mcp_servers"] == '{"configured": true, "servers": []}'
    assert rows["a3"]["mcp_servers"] == "[]"

    keys = {r[0] for r in upgraded.execute("SELECT key FROM settings")}
    assert keys == {"transcription.guidance.glossary"}


def test_v63_adds_durable_oracle_delegations(tmp_path):
    path = tmp_path / "v61.sqlite"
    con = _fresh(path)
    _shape_as_v61(con)
    con.close()

    upgraded = _connect(path)
    db._migrate(upgraded)

    assert "oracle_delegations" in _names(upgraded, "table")
    assert {
        "delegation_id", "owner_principal", "trace_id", "client_msg_id",
        "agent_id", "session", "backend_session_id", "request_text", "status",
        "result_message_id", "result_text", "error", "created_at", "updated_at",
        "delivered_at",
    } == _columns(upgraded, "oracle_delegations")
    assert {"idx_oracle_delegations_delivery",
            "idx_oracle_delegations_agent"} <= _names(upgraded, "index")


def test_upgraded_database_matches_fresh_schema(tmp_path):
    """The migration and _SCHEMA_SQL must describe the same shape."""
    fresh = _fresh(tmp_path / "fresh.sqlite")
    upgraded_path = tmp_path / "upgraded.sqlite"
    upgraded = _fresh(upgraded_path)
    _shape_as_v61(upgraded)
    upgraded.close()
    upgraded = _connect(upgraded_path)
    db._migrate(upgraded)

    for kind in ("table", "view", "index"):
        assert _names(upgraded, kind) == _names(fresh, kind), kind
    for table in _names(fresh, "table"):
        assert _columns(upgraded, table) == _columns(fresh, table), table
