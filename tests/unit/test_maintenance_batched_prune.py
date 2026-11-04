"""RM-N8: the hourly retention sweep never holds the write lock for long.

One DELETE over a column no index covers scans its whole table under the
write lock; the state_log sweep held it for 10-47 s. The sweep now finds the
rows by reads outside the lock and deletes them by rowid in small batches.
"""
from __future__ import annotations

import json
import random
import sqlite3

from lib import batched_delete, db, maintenance, tool_explanation_learning
from lib.protocol import ClipProducerStatus

NOW = 10_000_000
POLICY = maintenance.Policy(
    sse_max_age_ms=1000, tts_max_age_ms=1000, state_max_age_ms=1000,
    clip_row_max_age_ms=1000, turn_usage_max_age_ms=1000,
    background_job_events_max_age_ms=1000, judgment_decisions_max_age_ms=1000)
CUTOFF = NOW - 1000

# The statements the sweep ran before RM-N8, verbatim: the oracle.
OLD = [
    ("DELETE FROM sse_events WHERE ts < ?", (CUTOFF,)),
    ("DELETE FROM tts_queue WHERE status IN ('done', 'failed') AND completed_at < ?", (CUTOFF,)),
    ("""DELETE FROM state_log WHERE ts < ? AND state_id NOT IN (
            SELECT MAX(state_id) FROM state_log GROUP BY agent_id)""", (CUTOFF,)),
    ("""DELETE FROM background_job_events WHERE observed_at < ? AND event_id NOT IN (
            SELECT MAX(event_id) FROM background_job_events GROUP BY job_id)""", (CUTOFF,)),
    ("DELETE FROM clips WHERE created_at < ? AND producer_status IN (?, ?)",
     (CUTOFF, ClipProducerStatus.COMPLETE, ClipProducerStatus.FAILED)),
    ("DELETE FROM turn_usage WHERE at < ?", (CUTOFF,)),
    ("DELETE FROM judgment_decisions WHERE created_at < ?", (CUTOFF,)),
    ("DELETE FROM tool_explanation_decisions WHERE at < ?", (NOW - 50,)),
    ("""DELETE FROM tool_explanation_decisions WHERE id <= (SELECT id FROM tool_explanation_decisions
            ORDER BY id DESC LIMIT 1 OFFSET ?)""", (30,)),
    ("""DELETE FROM tool_explanation_learned WHERE (prompt_version<>? OR templates_version<>?)
            AND COALESCE(last_hit_at, created_at) < ?""", (1, 1, NOW - 50)),
    ("""DELETE FROM tool_explanation_learned WHERE parameterised=0 AND rowid IN (SELECT rowid FROM
            tool_explanation_learned WHERE parameterised=0 ORDER BY COALESCE(last_hit_at, created_at) DESC
            LIMIT -1 OFFSET ?)""", (20,)),
]
TABLES = ["sse_events", "tts_queue", "state_log", "background_job_events", "clips",
          "turn_usage", "judgment_decisions", "tool_explanation_decisions", "tool_explanation_learned"]


def _seed(connection, rnd: random.Random) -> None:
    def ts():
        return CUTOFF + rnd.randint(-3000, 3000)
    # Rows at the cutoff boundary, and a group whose newest row is older than
    # the cutoff (kept), for both keep-newest tables.
    for at in (CUTOFF - 1, CUTOFF, CUTOFF + 1):
        connection.execute("INSERT INTO state_log(agent_id, ts, kind) VALUES ('synthetic-edge', ?, 'idle')", (at,))
        connection.execute("INSERT INTO background_job_events(job_id, observed_at) VALUES ('job-edge', ?)", (at,))
        connection.execute("INSERT INTO sse_events(ts, type, payload, agent_id) VALUES (?, 'x', '{}', 'synthetic')", (at,))
    for at in (CUTOFF - 900, CUTOFF - 500):
        connection.execute("INSERT INTO state_log(agent_id, ts, kind) VALUES ('synthetic-old', ?, 'idle')", (at,))
        connection.execute("INSERT INTO background_job_events(job_id, observed_at) VALUES ('job-old', ?)", (at,))
    for i in range(300):
        connection.execute("INSERT INTO sse_events(ts, type, payload, agent_id) VALUES (?, 'x', '{}', 'synthetic')", (ts(),))
        connection.execute(
            "INSERT INTO tts_queue(agent_id, text, voice_id, session, source, enqueued_at, status, completed_at) "
            "VALUES ('synthetic', 'x', 'v', 's', 'test', 0, ?, ?)", (rnd.choice(["done", "failed", "pending"]), ts()))
        connection.execute("INSERT INTO state_log(agent_id, ts, kind) VALUES (?, ?, 'idle')",
                           (f"synthetic-{rnd.randrange(6)}", ts()))
        connection.execute("INSERT INTO background_job_events(job_id, observed_at) VALUES (?, ?)",
                           (f"job-{rnd.randrange(6)}", ts()))
        connection.execute(
            "INSERT INTO clips(agent_id, path, created_at, producer_status) VALUES ('synthetic', ?, ?, ?)",
            (f"/synthetic/{i}", ts(), rnd.choice([ClipProducerStatus.COMPLETE, ClipProducerStatus.FAILED, "running"])))
        connection.execute("INSERT INTO turn_usage(backend, agent_id, at) VALUES ('claude', 'synthetic', ?)", (ts(),))
        connection.execute("INSERT INTO judgment_decisions(site, created_at) VALUES ('synthetic', ?)", (ts(),))
        connection.execute("INSERT INTO tool_explanation_decisions(at, tier) VALUES (?, 'template')",
                           (NOW + rnd.randint(-100, 100),))
        stamp = NOW + rnd.randint(-100, 100)
        connection.execute(
            "INSERT INTO tool_explanation_learned(signature, level, producer, parameterised, prompt_version, "
            "templates_version, created_at, last_hit_at) VALUES (?, 0, 'test', ?, ?, 1, ?, ?)",
            (f"sig-{i}", rnd.randrange(2), rnd.choice([1, 2]), stamp, rnd.choice([None, stamp + 5])))


def _rowids(connection) -> dict[str, list[int]]:
    return {t: [r[0] for r in connection.execute(f"SELECT rowid FROM {t} ORDER BY rowid")] for t in TABLES}


def _sweep(monkeypatch):
    monkeypatch.setattr(tool_explanation_learning, "DECISION_MAX_AGE_MS", 50)
    monkeypatch.setattr(tool_explanation_learning, "DECISION_MAX_ROWS", 30)
    monkeypatch.setattr(tool_explanation_learning, "EXACT_MAX_ROWS", 20)
    monkeypatch.setattr(tool_explanation_learning, "STALE_VERSION_GRACE_MS", 50)
    import lib.tool_explanations as te
    import lib.tool_explanation_templates as tt
    monkeypatch.setattr(te, "PROMPT_VERSION", 1)
    monkeypatch.setattr(tt, "VERSION", 1)
    return maintenance.prune_database(now_ms=NOW, policy=POLICY)


def test_the_batched_sweep_deletes_exactly_the_rows_the_single_statements_deleted(monkeypatch):
    monkeypatch.setattr(batched_delete, "READ_SPAN", 37)
    monkeypatch.setattr(batched_delete, "DELETE_BATCH", 11)
    monkeypatch.setattr(batched_delete, "PAUSE_S", 0)
    monkeypatch.setattr(maintenance, "STATE_LOG_DELETE_BATCH", 11)
    monkeypatch.setattr(maintenance, "STATE_LOG_BATCH_PAUSE_S", 0)
    connection = db.conn()
    _seed(connection, random.Random(8))
    oracle, oracle_before = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    connection.backup(oracle)
    connection.backup(oracle_before)
    for sql, params in OLD:
        oracle.execute(sql, params)
    oracle.commit()

    _sweep(monkeypatch)

    expected, got = _rowids(oracle), _rowids(connection)
    assert {t: len(v) for t, v in got.items()} == {t: len(v) for t, v in expected.items()}
    assert got == expected
    assert all(len(expected[t]) < len(_rowids(oracle_before)[t]) for t in TABLES), "the fixture deletes in every table"
    kept = connection.execute(
        "SELECT agent_id, ts FROM state_log WHERE agent_id IN ('synthetic-edge', 'synthetic-old') ORDER BY state_id").fetchall()
    assert [tuple(r) for r in kept] == [("synthetic-edge", CUTOFF), ("synthetic-edge", CUTOFF + 1),
                                        ("synthetic-old", CUTOFF - 500)]


def test_every_write_statement_deletes_by_rowid_at_most_one_batch(monkeypatch):
    monkeypatch.setattr(batched_delete, "DELETE_BATCH", 7)
    monkeypatch.setattr(batched_delete, "PAUSE_S", 0)
    monkeypatch.setattr(maintenance, "STATE_LOG_DELETE_BATCH", 7)
    monkeypatch.setattr(maintenance, "STATE_LOG_BATCH_PAUSE_S", 0)
    connection = db.conn()
    _seed(connection, random.Random(9))
    deletes, batches = [], []

    class Observed:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, sql, parameters=()):
            cursor = connection.execute(sql, parameters)
            flat = " ".join(sql.split())
            if flat.upper().startswith("DELETE FROM") and any(f" {t} " in f" {flat} " for t in TABLES):
                deletes.append((flat, cursor.rowcount))
                if "json_each(?)" in flat:
                    batches.append((flat, json.loads(parameters[0])))
            return cursor

    monkeypatch.setattr(db, "conn", lambda: Observed())
    _sweep(monkeypatch)

    assert deletes
    assert all("WHERE rowid IN (SELECT value FROM json_each(?))" in sql for sql, _ in deletes), \
        [sql for sql, _ in deletes if "json_each" not in sql][:3]
    assert max(rows for _, rows in deletes) <= 7
    # Oldest first within every delete, so a reader mid-sweep sees a state
    # between before and after.
    for statement in {sql for sql, _ in batches}:
        order = [rowid for sql, ids in batches if sql == statement for rowid in ids]
        assert order == sorted(order), statement


def test_a_waiting_writer_gets_the_lock_between_batches(monkeypatch):
    connection = db.conn()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    connection.executemany("INSERT INTO state_log(agent_id, ts, kind) VALUES ('synthetic-a', 1, 'idle')",
                           [()] * 50)
    monkeypatch.setattr(maintenance, "STATE_LOG_DELETE_BATCH", 10)
    other = sqlite3.connect(path, timeout=0, isolation_level=None)
    admitted = []

    def pause(seconds):
        # Another writer with no busy wait at all: it gets in only if the
        # sweep holds no lock while it pauses.
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO settings(key, value, updated_at) VALUES (?, 'x', 0)",
                      (f"rm-n8-{len(admitted)}",))
        other.execute("COMMIT")
        admitted.append(seconds)

    monkeypatch.setattr(batched_delete.time, "sleep", pause)
    try:
        maintenance.prune_database(now_ms=NOW, policy=POLICY)
    finally:
        other.close()

    assert len(admitted) == 4      # 49 expired rows (the newest is kept), 10 per batch
    assert connection.execute("SELECT count(*) FROM state_log WHERE agent_id = 'synthetic-a'").fetchone()[0] == 1


def test_a_row_that_stops_qualifying_after_the_read_is_not_deleted(monkeypatch):
    connection = db.conn()
    connection.executemany("INSERT INTO state_log(agent_id, ts, kind) VALUES ('synthetic-a', 1, 'idle')", [()] * 3)
    connection.execute("INSERT INTO state_log(agent_id, ts, kind) VALUES ('synthetic-a', ?, 'idle')", (NOW,))
    real = batched_delete.delete_rowids

    def rewritten_meanwhile(c, table, rowids, where, params=(), **kwargs):
        if table == "state_log" and rowids:
            connection.execute("UPDATE state_log SET ts = ? WHERE state_id = ?", (NOW, rowids[0]))
        return real(c, table, rowids, where, params, **kwargs)

    monkeypatch.setattr(batched_delete, "delete_rowids", rewritten_meanwhile)
    counts = maintenance.prune_database(now_ms=NOW, policy=POLICY)

    assert counts["state_log"] == 2
    assert connection.execute(
        "SELECT count(*) FROM state_log WHERE agent_id = 'synthetic-a'").fetchone()[0] == 2
