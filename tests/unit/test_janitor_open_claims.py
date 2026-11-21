"""A janitor's open demand claim is found among the open claims (schema 137).

pending_demand_claim walked every run of the agent to find an open claim (over 100,000 runs on one
Host, several times a janitor pass). It now starts from the open claims through a partial index.
These check that it answers as before, that its plan uses that index, and that the migration closes
only the claims it could never count.
"""
import json
import sqlite3
import sys
import time

from lib import db, janitor_store


def _run(c, run_id, agent_id, *, status="running", expires_at=None):
    configuration = {"executor": "ephemeral"} if expires_at is None else \
        {"executor": "ephemeral", "expires_at": expires_at}
    c.execute("""INSERT INTO janitor_runs(run_id,agent_id,session,attachment_id,generation,trace_id,status,
                 candidates_json,configuration_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
              (run_id, agent_id, "s", "att", 1, f"t-{run_id}", status, "[]", json.dumps(configuration), 1))


def _claim(c, run_id, finished_at=None):
    c.execute("INSERT INTO janitor_demand_claims(run_id,claimed_at,finished_at) VALUES (?,?,?)",
              (run_id, 1, finished_at))


def _rows(fill):
    c = db.conn()
    c.execute("PRAGMA foreign_keys=OFF")
    try:
        fill(c)
        c.commit()
    finally:
        c.execute("PRAGMA foreign_keys=ON")
    return c


def test_an_open_unexpired_claim_of_the_agent_is_pending_and_nothing_else_is():
    now = db.now_ms()
    later, earlier = now + 60_000, now - 60_000

    def fill(c):
        for n in range(50):                                   # a long history of finished claims
            _run(c, f"old-{n}", "jan", status="completed", expires_at=earlier)
            _claim(c, f"old-{n}", finished_at=now - 1)
        _run(c, "expired", "jan", status="cancelled", expires_at=earlier)
        _claim(c, "expired")                                  # open, but its expiry passed
        _run(c, "other", "someone-else", expires_at=later)
        _claim(c, "other")                                    # open and live, another agent's
    c = _rows(fill)
    assert not janitor_store.pending_demand_claim("jan", now)
    _rows(lambda c: (_run(c, "live", "jan", expires_at=later), _claim(c, "live")))
    assert janitor_store.pending_demand_claim("jan", now)
    c.execute("UPDATE janitor_demand_claims SET finished_at=? WHERE run_id='live'", (now,))
    c.commit()
    assert not janitor_store.pending_demand_claim("jan", now)


def test_the_question_starts_from_the_open_claims():
    plan = " ".join(str(row[3]) for row in db.conn().execute(
        """EXPLAIN QUERY PLAN SELECT 1 FROM janitor_demand_claims d CROSS JOIN janitor_runs r
           WHERE d.finished_at IS NULL AND r.run_id=d.run_id AND r.agent_id=?
           AND json_extract(r.configuration_json,'$.expires_at')>? LIMIT 1""", ("jan", 0)))
    assert "idx_janitor_demand_claims_open" in plan and "SEARCH r" in plan, plan


def test_the_migration_closes_only_the_claims_no_question_could_count(tmp_path):
    sys.path.insert(0, str(db.__file__).rsplit("/lib/", 1)[0])
    from lib import db_migrations
    path = tmp_path / "claims.sqlite"
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("CREATE TABLE janitor_runs(run_id TEXT PRIMARY KEY, agent_id TEXT, status TEXT,"
                " configuration_json TEXT, finished_at INTEGER)")
    con.execute("CREATE TABLE janitor_demand_claims(run_id TEXT PRIMARY KEY, claimed_at INTEGER NOT NULL,"
                " finished_at INTEGER)")
    now = int(time.time() * 1000)
    for run_id, status, expires in (("ended-expired", "cancelled", now - 1000),
                                    ("ended-no-expiry", "failed", None),
                                    ("ended-live", "cancelled", now + 3_600_000),
                                    ("running", "running", now - 1000)):
        configuration = {} if expires is None else {"expires_at": expires}
        con.execute("INSERT INTO janitor_runs VALUES (?,?,?,?,?)",
                    (run_id, "jan", status, json.dumps(configuration), 5 if status != "running" else None))
        con.execute("INSERT INTO janitor_demand_claims(run_id,claimed_at) VALUES (?,1)", (run_id,))
    db_migrations._migrate_to_v137(con)
    still_open = {r[0] for r in con.execute(
        "SELECT run_id FROM janitor_demand_claims WHERE finished_at IS NULL")}
    # An ended run whose claim is still unexpired keeps it: a release waits on it.
    assert still_open == {"ended-live", "running"}
    assert con.execute("SELECT finished_at FROM janitor_demand_claims WHERE run_id='ended-expired'"
                       ).fetchone()[0] == 5
