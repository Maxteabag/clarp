"""Upgrade real legacy approval rows without reinterpreting their permission."""
from __future__ import annotations

import json
import sqlite3

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
