"""Upgrade real legacy approval rows without reinterpreting their permission."""
from __future__ import annotations

import json
import sqlite3

import pytest

from lib import db


# The pre-question table definitions are independent of the current schema.
_LEGACY_TABLES = """
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ready',
    reference_id TEXT NOT NULL DEFAULT '',
    payload_version INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL DEFAULT '{}',
    source_message_id TEXT NOT NULL DEFAULT '',
    source_trace_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER,
    deleted_at INTEGER
);
CREATE TABLE artifact_decisions (
    decision_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(artifact_id),
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    yes_label TEXT NOT NULL DEFAULT 'Yes',
    no_label TEXT NOT NULL DEFAULT 'No',
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_choice TEXT NOT NULL DEFAULT '',
    resolved_at INTEGER,
    resolved_by TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    expires_at INTEGER
);
CREATE TABLE decision_deliveries (
    decision_id TEXT PRIMARY KEY REFERENCES artifact_decisions(decision_id),
    artifact_id TEXT NOT NULL,
    session TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    reference_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    choice TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    delivered_at INTEGER
);
PRAGMA user_version = 66;
"""


def test_v66_approval_migration_preserves_rows_and_matches_fresh_tables(tmp_path):
    con = sqlite3.connect(tmp_path / "old.sqlite", isolation_level=None)
    con.row_factory = sqlite3.Row
    # Keep the other Host tables present: later migrations may upgrade them.
    # Only the approval tables are intentionally downgraded by this fixture.
    db._migrate(con)
    for table in ("decision_deliveries", "artifact_decisions", "artifacts"):
        con.execute(f"DROP TABLE {table}")
    con.executescript(_LEGACY_TABLES)
    for status in ("pending", "accepted", "rejected", "expired"):
        con.execute("""INSERT INTO artifacts(artifact_id,agent_id,session,type,title,status,created_at,updated_at)
                       VALUES(?,'agent','mike','decision','Deploy?',?,1,2)""", (status, status))
        con.execute("""INSERT INTO artifact_decisions(decision_id,artifact_id,question,status,resolved_choice,revision)
                       VALUES(?,?,'Deploy?',?,?,?)""",
                    (status, status, status, status if status != "pending" else "", 1 if status == "pending" else 2))
        if status != "pending":
            con.execute("""INSERT INTO decision_deliveries(decision_id,artifact_id,session,question,choice,created_at)
                           VALUES(?,?,'mike','Deploy?',?,2)""", (status, status, status))
    db._migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    for row in con.execute("SELECT * FROM artifact_decisions"):
        assert row["response_type"] == "approval"
        assert row["status"] == row["decision_id"]
        assert json.loads(row["options_json"]) == []
        assert not row["allow_custom_text"] and not row["blocks_progress"]
        assert row["urgency"] == "normal" and row["response_effort"] == "review"
        assert row["deadline_at"] is None and row["recommended_option_id"] is None
        expected_answer = {"choice": row["status"]} if row["status"] in {"accepted", "rejected"} else None
        assert json.loads(row["answer_json"] or "null") == expected_answer
    for row in con.execute("SELECT * FROM decision_deliveries"):
        assert row["status"] == "pending" and row["response_type"] == "approval"
        expected_answer = {"choice": row["choice"]} if row["choice"] in {"accepted", "rejected"} else None
        assert json.loads(row["answer_json"] or "null") == expected_answer
    assert all(row[0] is None for row in con.execute("SELECT archived_at FROM artifacts"))
    fresh = db.conn()
    for table in ("artifacts", "artifact_decisions", "decision_deliveries"):
        assert [tuple(row) for row in con.execute(f"PRAGMA table_info({table})")] == [
            tuple(row) for row in fresh.execute(f"PRAGMA table_info({table})")]
    before = [tuple(row) for row in con.execute("SELECT * FROM artifact_decisions")]
    db._migrate(con)
    assert [tuple(row) for row in con.execute("SELECT * FROM artifact_decisions")] == before
    con.close()


# These are the actual unrelated additions from the historical dreaming
# migrations (v67) and two v68 variants, reconciled by that branch's v69.
# They are fixtures only: the production migration must neither add nor remove
# them, and must preserve their data regardless of the shared version stamp.
_DREAM_V67 = """
ALTER TABLE dream_runs ADD COLUMN seed_strategy TEXT NOT NULL DEFAULT 'control';
ALTER TABLE dream_runs ADD COLUMN context_dose TEXT NOT NULL DEFAULT 'full';
ALTER TABLE dream_runs ADD COLUMN seed_material TEXT NOT NULL DEFAULT '';
ALTER TABLE dream_threads ADD COLUMN killed_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE dream_threads ADD COLUMN origin_note TEXT NOT NULL DEFAULT '';
"""
_VOICE_V68 = "ALTER TABLE agents ADD COLUMN voice_verbosity INTEGER NOT NULL DEFAULT 0;"
_BRANCH_V68 = "ALTER TABLE dream_runs ADD COLUMN artifact_branch TEXT NOT NULL DEFAULT '';"


def _legacy_host(tmp_path, version, attention_shape, *, v68_variant="both"):
    con = sqlite3.connect(tmp_path / "legacy-host.sqlite", isolation_level=None)
    con.row_factory = sqlite3.Row
    db._migrate(con)
    # Keep the full Host schema, but rebuild the three old tables from their
    # independent pre-attention definitions. No new production column list is
    # used to manufacture the legacy attention shape.
    con.executescript("""
        DROP TABLE decision_deliveries;
        DROP TABLE artifact_decisions;
        DROP TABLE artifacts;
    """ + _LEGACY_TABLES)
    if version >= 67:
        con.executescript(_DREAM_V67)
    if version >= 68:
        if v68_variant in {"both", "voice"}:
            con.executescript(_VOICE_V68)
        if v68_variant in {"both", "branch"}:
            con.executescript(_BRANCH_V68)
    con.execute("""INSERT INTO agents(agent_id,persona,voice_id,cwd,session,created_at)
                   VALUES('agent','Mike','v',?,'mike',1)""", (str(tmp_path),))
    con.execute("""INSERT INTO dream_runs(run_id,agent_id,session,local_date,timezone,timezone_source,
                   min_directions,planned_directions,planned_rounds,target_tokens,target_minutes,started_at,updated_at)
                   VALUES('dream','agent','mike','2026-09-05','Europe/Oslo','configured',1,2,3,4000,5,6,7)""")
    con.execute("""INSERT INTO dream_threads(thread_id,run_id,thread_index,title,created_at,updated_at)
                   VALUES('thread','dream',1,'Preserve this dream',8,9)""")
    if version >= 67:
        con.execute("""UPDATE dream_runs SET seed_strategy='sentinel-strategy',context_dose='sentinel-dose',
                       seed_material='Preserve exact seed text'""")
        con.execute("""UPDATE dream_threads SET killed_reason='Preserve rejection reason',
                       origin_note='Preserve origin note'""")
    if version >= 68:
        if v68_variant in {"both", "voice"}:
            con.execute("UPDATE agents SET voice_verbosity=2")
        if v68_variant in {"both", "branch"}:
            con.execute("UPDATE dream_runs SET artifact_branch='dream/preserve-this-branch'")
    # An unrelated table/index/trigger verifies that reconciliation does not
    # replace the database with the new fresh-install schema.
    con.executescript("""
        CREATE TABLE external_sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
        CREATE INDEX idx_external_sentinel_value ON external_sentinel(value);
        CREATE TRIGGER protect_external_sentinel BEFORE DELETE ON external_sentinel
        BEGIN SELECT RAISE(ABORT,'must preserve unrelated data'); END;
        INSERT INTO external_sentinel VALUES(1,'Keep unrelated data verbatim');
    """)
    for status in ("pending", "accepted", "rejected", "expired"):
        con.execute("""INSERT INTO artifacts(artifact_id,agent_id,session,type,title,status,created_at,updated_at)
                       VALUES(?,'agent','mike','decision','Deploy?',?,1,2)""", (status, status))
        con.execute("""INSERT INTO artifact_decisions(decision_id,artifact_id,question,status,resolved_choice,revision)
                       VALUES(?,?,'Deploy?',?,?,?)""",
                    (status, status, status, status if status != "pending" else "", 1 if status == "pending" else 2))
        if status != "pending":
            con.execute("""INSERT INTO decision_deliveries(decision_id,artifact_id,session,question,choice,created_at)
                           VALUES(?,?,'mike','Deploy?',?,2)""", (status, status, status))
    if attention_shape == "partial":
        # Deliberately mixed tables and column order, representative of a
        # partial schema overlap; existing values must survive every add.
        con.executescript("""
            ALTER TABLE artifacts ADD COLUMN archived_at INTEGER;
            ALTER TABLE artifact_decisions ADD COLUMN answer_json TEXT;
            ALTER TABLE artifact_decisions ADD COLUMN blocks_progress INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE artifact_decisions ADD COLUMN response_type TEXT NOT NULL DEFAULT 'approval';
            ALTER TABLE decision_deliveries ADD COLUMN answer_json TEXT;
        """)
    elif attention_shape == "complete":
        # The released attention-v67 shape can coexist with a legacy v68/v69
        # stamp. Apply the actual migration once, then verify the second pass.
        db._migrate(con)
        con.execute("""UPDATE artifact_decisions SET response_type='single_choice',
                       options_json='[{"id":"a","label":"A"},{"id":"b","label":"B"}]',
                       allow_custom_text=1,answer_json='{"text":"Keep my exact answer"}',
                       status='answered',resolved_choice='answered' WHERE decision_id='expired'""")
        con.execute("""UPDATE artifacts SET type='question',status='completed' WHERE artifact_id='expired'""")
        con.execute("""UPDATE decision_deliveries SET response_type='single_choice',choice='answered',
                       answer_json='{"text":"Keep my exact answer"}' WHERE decision_id='expired'""")
    if attention_shape in {"partial", "complete"}:
        con.execute("UPDATE artifacts SET archived_at=12345 WHERE artifact_id='pending'")
        con.execute("""UPDATE artifact_decisions SET blocks_progress=1 WHERE decision_id='pending'""")
        con.execute("""UPDATE artifact_decisions SET answer_json='{"choice":"accepted","sentinel":"preserve"}'
                       WHERE decision_id='accepted'""")
        con.execute("""UPDATE decision_deliveries SET answer_json='{"choice":"accepted","sentinel":"preserve"}'
                       WHERE decision_id='accepted'""")
        # Missing snapshots must be backfilled even when all columns exist.
        con.execute("UPDATE artifact_decisions SET answer_json=NULL WHERE decision_id='rejected'")
        con.execute("UPDATE decision_deliveries SET answer_json=NULL WHERE decision_id='rejected'")
    con.execute(f"PRAGMA user_version={version}")
    return con


def _unrelated_snapshot(con):
    tables = ("agents", "dream_runs", "dream_threads", "external_sentinel")
    return {
        "schema": [tuple(row) for row in con.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN (?,?,?,?) ORDER BY type,name", tables)],
        "rows": {table: [tuple(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables},
    }


def _column_contract(con, table):
    # Partial additive migrations may preserve a different physical column
    # order. Names, types, defaults, nullability and primary keys must match.
    return {row[1]: tuple(row)[2:] for row in con.execute(f"PRAGMA table_info({table})")}


@pytest.mark.parametrize("version", [66, 67, 68, 69])
@pytest.mark.parametrize("attention_shape", ["absent", "partial", "complete"])
def test_overlap_versions_reconcile_attention_without_touching_unrelated_data(tmp_path, version, attention_shape):
    con = _legacy_host(tmp_path, version, attention_shape)
    before = _unrelated_snapshot(con)
    db._migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    assert _unrelated_snapshot(con) == before
    fresh = db.conn()
    for table in ("artifacts", "artifact_decisions", "decision_deliveries"):
        assert _column_contract(con, table) == _column_contract(fresh, table)
    decisions = {row["decision_id"]: dict(row) for row in con.execute("SELECT * FROM artifact_decisions")}
    deliveries = {row["decision_id"]: dict(row) for row in con.execute("SELECT * FROM decision_deliveries")}
    for table in (decisions, deliveries):
        assert json.loads(table["rejected"]["answer_json"]) == {"choice": "rejected"}
        expected_accepted = {"choice": "accepted"}
        if attention_shape != "absent":
            expected_accepted["sentinel"] = "preserve"
        assert json.loads(table["accepted"]["answer_json"]) == expected_accepted
        if attention_shape == "complete":
            assert table["expired"]["response_type"] == "single_choice"
            assert json.loads(table["expired"]["answer_json"]) == {"text": "Keep my exact answer"}
        else:
            assert table["expired"]["answer_json"] is None
    assert decisions["pending"]["answer_json"] is None
    archived = con.execute("SELECT archived_at FROM artifacts WHERE artifact_id='pending'").fetchone()[0]
    assert archived == (None if attention_shape == "absent" else 12345)
    assert decisions["pending"]["blocks_progress"] == (0 if attention_shape == "absent" else 1)
    # A historical build may have reused a lower version stamp. Exercise the
    # reconciliation itself again, not just the current-version early return.
    con.execute("PRAGMA user_version=69")
    before_rerun = con.total_changes
    db._migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    assert con.total_changes == before_rerun and _unrelated_snapshot(con) == before
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


@pytest.mark.parametrize("v68_variant", ["voice", "branch"])
def test_split_v68_variants_keep_their_existing_unrelated_columns(tmp_path, v68_variant):
    con = _legacy_host(tmp_path, 68, "absent", v68_variant=v68_variant)
    before = _unrelated_snapshot(con)
    db._migrate(con)
    assert _unrelated_snapshot(con) == before
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    assert "archived_at" in _column_contract(con, "artifacts")
    assert "response_type" in _column_contract(con, "artifact_decisions")
    con.close()


def test_future_database_version_is_never_downgraded_or_mutated(tmp_path):
    con = _legacy_host(tmp_path, 69, "complete")
    con.execute(f"PRAGMA user_version={db._SCHEMA_VERSION + 1}")
    before = con.total_changes
    db._migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION + 1
    assert con.total_changes == before
    con.close()


def test_migrated_legacy_v69_host_can_run_typed_question_lifecycle(tmp_path):
    from lib import artifacts

    con = _legacy_host(tmp_path, 69, "absent")
    db._migrate(con)
    con.close()
    db.reset_for_tests(tmp_path / "legacy-host.sqlite")
    question = artifacts.create_decision(
        session="mike", title="Layout", question="Which layout?", response_type="single_choice",
        options=[{"id": "a", "label": "Current"}, {"id": "b", "label": "Compact"}])
    pending = artifacts.attention(include_questions=True)
    assert question["artifact_id"] in {item["artifact_id"] for item in pending}
    resolved, changed = artifacts.resolve(question["decision"]["decision_id"],
                                          expected_revision=1, answer={"text": "Keep both for now"})
    assert changed and resolved["decision"]["answer"] == {"text": "Keep both for now"}
    delivery = next(row for row in artifacts.pending_deliveries() if row["decision_id"] == question["decision"]["decision_id"])
    assert "Keep both for now" in artifacts.format_delivery_prompt(delivery)


def test_attention_reconciliation_failure_rolls_back_all_additions_and_version(tmp_path):
    con = _legacy_host(tmp_path, 69, "absent")
    con.execute("DROP TABLE decision_deliveries")
    before_schema = [tuple(row) for row in con.execute(
        "SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
    before_data = _unrelated_snapshot(con)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db._migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 69
    assert [tuple(row) for row in con.execute(
        "SELECT type,name,sql FROM sqlite_master ORDER BY type,name")] == before_schema
    assert _unrelated_snapshot(con) == before_data
    con.close()
