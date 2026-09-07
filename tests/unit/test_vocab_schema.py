"""Schema for transcription context packs.

Covers both arrival routes - a fresh database built from _SCHEMA_SQL and an
existing one upgraded by _migrate_to_v64 - because a table that only exists
on one of those paths breaks in exactly one direction and is easy to miss.
"""
from __future__ import annotations

import sqlite3

import pytest

from lib import db as dbmod

VOCAB_TABLES = {
    "vocab_packs",
    "vocab_terms",
    "vocab_profiles",
    "vocab_profile_packs",
    "vocab_assignments",
    "vocab_runs",
}


def _tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _fresh(tmp_path) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "fresh.sqlite")
    dbmod._create_schema(con)
    return con


def test_fresh_schema_contains_every_vocab_table(tmp_path):
    assert VOCAB_TABLES <= _tables(_fresh(tmp_path))


def test_migration_creates_the_same_tables(tmp_path):
    """An upgraded database must match a freshly created one."""
    con = sqlite3.connect(tmp_path / "upgrade.sqlite")
    con.execute("CREATE TABLE agents (agent_id TEXT PRIMARY KEY)")
    dbmod._migrate_to_v64(con)
    assert VOCAB_TABLES <= _tables(con)


def test_schema_version_was_bumped():
    assert dbmod._SCHEMA_VERSION >= 64


def test_assignment_binds_to_exactly_one_owner(tmp_path):
    """A profile belongs to an agent or a team, never both, never neither."""
    con = _fresh(tmp_path)
    con.execute(
        "INSERT INTO vocab_profiles (profile_id, name, created_at, updated_at)"
        " VALUES ('p1', 'Default', 0, 0)")

    con.execute(
        "INSERT INTO vocab_assignments"
        " (assignment_id, profile_id, agent_id, team_id, created_at)"
        " VALUES ('a1', 'p1', 'agent-1', NULL, 0)")

    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO vocab_assignments"
            " (assignment_id, profile_id, agent_id, team_id, created_at)"
            " VALUES ('a2', 'p1', 'agent-2', 'team-2', 0)")

    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO vocab_assignments"
            " (assignment_id, profile_id, agent_id, team_id, created_at)"
            " VALUES ('a3', 'p1', NULL, NULL, 0)")


def test_one_profile_per_agent(tmp_path):
    con = _fresh(tmp_path)
    con.execute(
        "INSERT INTO vocab_profiles (profile_id, name, created_at, updated_at)"
        " VALUES ('p1', 'Default', 0, 0)")
    con.execute(
        "INSERT INTO vocab_assignments"
        " (assignment_id, profile_id, agent_id, created_at)"
        " VALUES ('a1', 'p1', 'agent-1', 0)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO vocab_assignments"
            " (assignment_id, profile_id, agent_id, created_at)"
            " VALUES ('a2', 'p1', 'agent-1', 0)")


def test_terms_are_unique_per_pack_case_insensitively(tmp_path):
    """'Clarp' and 'clarp' are the same biasing term; storing both wastes budget."""
    con = _fresh(tmp_path)
    con.execute(
        "INSERT INTO vocab_packs (pack_id, name, created_at, updated_at)"
        " VALUES ('pk1', 'People', 0, 0)")
    con.execute(
        "INSERT INTO vocab_terms (pack_id, text, created_at)"
        " VALUES ('pk1', 'Clarp', 0)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO vocab_terms (pack_id, text, created_at)"
            " VALUES ('pk1', 'clarp', 0)")


def test_pack_kind_is_constrained(tmp_path):
    con = _fresh(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO vocab_packs (pack_id, name, kind, created_at, updated_at)"
            " VALUES ('pk1', 'Bad', 'sometimes', 0, 0)")


def test_runs_table_stores_the_full_audit(tmp_path):
    con = _fresh(tmp_path)
    con.execute(
        "INSERT INTO vocab_runs"
        " (provider, model, unit, capacity, used, form, payload,"
        "  included_json, dropped_json, created_at)"
        " VALUES ('faster-whisper', 'small.en', 'tokens', 223, 40, 'terms',"
        "         'Clarp.', '[{\"text\":\"Clarp\"}]',"
        "         '[{\"text\":\"the\",\"reason\":\"below rarity floor\"}]', 0)")
    row = con.execute(
        "SELECT provider, capacity, used, dropped_json FROM vocab_runs").fetchone()
    assert row[0] == "faster-whisper"
    assert row[1] == 223 and row[2] == 40
    assert "below rarity floor" in row[3]
