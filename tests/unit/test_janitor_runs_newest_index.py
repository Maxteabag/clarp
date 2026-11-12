"""10 Nov 2026: every 15 seconds and for each janitor, janitor_attention._episode read and
sorted every run of the agent (58,687 completed runs for the largest, in a table of 115,819)
to find the newest one. With idx_janitor_runs_newest the two statements a healthy janitor
runs stop at the first match. Two cases keep growing with the runs and are tested as such:
no valid success, and a newest run that failed."""
from __future__ import annotations

import json
import random

import pytest

from lib import agents, backends, db, db_migrations, db_schema, janitors, janitor_attention as attention

INDEX = "idx_janitor_runs_newest"
INDEX_SQL = "CREATE INDEX idx_janitor_runs_newest ON janitor_runs(agent_id, generation, status, finished_at)"
# A copy of the two statements, pasted here from janitor_attention as of main 5ce2ce9d, the
# commit before the index. It proves nothing about that commit by itself; it is a tripwire.
# The plans and timings behind the index were taken for exactly this text, so a change to
# either statement fails here and has to be measured again.
PINNED_NEWEST_VALID = """SELECT r.rowid AS sequence,r.*,d.result_json FROM janitor_runs r
        LEFT JOIN janitor_demand_results d ON d.run_id=r.run_id
        WHERE r.agent_id=? AND r.generation=? AND r.status='completed'
          AND ((r.outcome IN ('changed','same_task')
          AND EXISTS (SELECT 1 FROM janitor_effects e WHERE e.run_id=r.run_id
                      AND e.outcome IN ('changed','same_task')))
            OR (r.outcome='completed' AND r.finished_at IS NOT NULL
                AND (d.result_json IS NOT NULL OR ?='scheduled-prompt')))
        ORDER BY r.finished_at DESC,r.rowid DESC"""
PINNED_LATEST = """SELECT rowid AS sequence,* FROM janitor_runs
        WHERE agent_id=? AND generation=? AND status IN ('completed','failed','cancelled')
        ORDER BY finished_at DESC,rowid DESC LIMIT 1"""
TEMPLATES = ("scheduled-prompt", "tool-explainer", "task-labels")


@pytest.fixture
def janitor(monkeypatch):
    """One janitor (and a second agent its reviews can target), for rows written by hand."""
    monkeypatch.setattr(backends, "active_handles", lambda *a: [])
    sam = agents.create_agent(persona="Sam", voice_id="", cwd="/tmp", session="sam", backend="codex")
    hugo = agents.create_agent(persona="Hugo", voice_id="", cwd="/tmp", session="hugo", backend="codex")
    config = janitors.create("sam", attachments=[{"trigger_id": "agent-work-completed"}])
    return {"agent_id": sam, "target": hugo, "attachment_id": config["attachments"][0]["attachment_id"]}


def _add(janitor, name, *, generation=1, status="completed", outcome="completed", finished_at=1000,
         template="tool-explainer", effect=None, result=None) -> None:
    con = db.conn()
    con.execute(
        """INSERT INTO janitor_runs (run_id,agent_id,session,attachment_id,generation,trace_id,status,outcome,
               candidates_json,configuration_json,created_at,started_at,finished_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, janitor["agent_id"], "sam", janitor["attachment_id"], generation, "trace-" + name, status, outcome,
         "[]", json.dumps({"executor": "ephemeral", "template_id": template}), 1, 1, finished_at))
    if effect:
        con.execute(
            """INSERT INTO janitor_effects (run_id,target_agent_id,target_session,observed_state_id,outcome,
                   before_label,after_label,created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (name, janitor["target"], "hugo", 1, effect, "", "", 1))
    if result is not None:
        con.execute("INSERT INTO janitor_demand_results (run_id,result_json,created_at) VALUES (?,?,?)",
                    (name, json.dumps(result), 1))


def _config(janitor, generation=1, template="scheduled-prompt") -> dict:
    return {"agent_id": janitor["agent_id"], "generation": generation, "template_id": template,
            "persona": "Sam", "session": "sam"}


def _statements(config) -> list[str]:
    """The SQL _episode runs, with its values filled in."""
    seen: list[str] = []
    con = db.conn()
    con.set_trace_callback(seen.append)
    try:
        attention._episode(config)
    finally:
        con.set_trace_callback(None)
    return [sql for sql in seen if "janitor_runs" in sql]


_plans = iter(range(1_000_000))


def _plan(sql: str) -> str:
    # A text of its own every time: sqlite3 caches statements by text, and a cached EXPLAIN is
    # not prepared again after DROP INDEX, so it would answer with the plan from before.
    return " | ".join(row[3] for row in db.conn().execute(f"EXPLAIN QUERY PLAN {sql} -- {next(_plans)}"))


def _steps(config) -> int:
    """Virtual-machine steps one _episode call takes: what it costs, without a clock."""
    count = [0]

    def tick():
        count[0] += 1
        return 0

    con = db.conn()
    con.set_progress_handler(tick, 1)
    try:
        attention._episode(config)
    finally:
        con.set_progress_handler(None, 1)
    return count[0]


def _drop_index() -> None:
    db.conn().execute(f"DROP INDEX {INDEX}")


def _fill(janitor, runs: int, *, generation=1, newest_invalid=0) -> None:
    for n in range(runs):
        _add(janitor, f"g{generation}-ok-{n}", generation=generation, finished_at=1000 + n)
    for n in range(newest_invalid):                       # newer, completed, but not a success
        _add(janitor, f"g{generation}-skip-{n}", generation=generation, outcome="skipped",
             finished_at=1000 + runs + n)


# --- the plan --------------------------------------------------------------------

def test_a_new_database_has_the_index():
    row = db.conn().execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)).fetchone()
    assert " ".join(row[0].split()) == INDEX_SQL
    assert db.conn().execute("PRAGMA user_version").fetchone()[0] == db_schema._SCHEMA_VERSION >= 122


def test_the_newest_valid_run_is_read_from_the_index_without_a_sort(janitor):
    _fill(janitor, 50, newest_invalid=3)
    newest_valid, latest = _statements(_config(janitor))[:2]

    plan = _plan(newest_valid)
    assert f"SEARCH r USING INDEX {INDEX} (agent_id=? AND generation=? AND status=?)" in plan
    assert "TEMP B-TREE" not in plan, plan                # the index order is the ORDER BY read backwards
    assert f"USING INDEX {INDEX} (agent_id=? AND generation=? AND status=?)" in _plan(latest)

    # Control: without the index this is the plan that was measured on the Host.
    _drop_index()
    plan = _plan(newest_valid)
    assert "USING INDEX idx_janitor_runs_agent (agent_id=?)" in plan and "USE TEMP B-TREE FOR ORDER BY" in plan


def _has_statistics() -> bool:
    return bool(db.conn().execute(
        "SELECT count(*) FROM sqlite_master WHERE name LIKE 'sqlite_stat%'").fetchone()[0])


def _newest_valid_plan(janitor) -> str:
    return _plan(_statements(_config(janitor))[0])


def test_the_plan_holds_without_statistics_in_either_index_creation_order(janitor):
    """The Host's database has no sqlite_stat1, so the planner has only the schema to go by,
    and then the order the indexes were created in can decide between them."""
    no_sort = f"SEARCH r USING INDEX {INDEX} (agent_id=? AND generation=? AND status=?)"
    con = db.conn()
    assert not _has_statistics()

    # A fresh database, before it holds a single run: the index sits between the table's others.
    plan = _newest_valid_plan(janitor)
    assert no_sort in plan and "TEMP B-TREE" not in plan, plan
    _fill(janitor, 60, newest_invalid=3)
    plan = _newest_valid_plan(janitor)
    assert no_sort in plan and "TEMP B-TREE" not in plan, plan

    # An upgraded database: the step creates it after every other index.
    _as_v121(con)
    db_migrations._migrate(con)
    plan = _newest_valid_plan(janitor)
    assert no_sort in plan and "TEMP B-TREE" not in plan, plan
    assert not _has_statistics()

    # And with statistics, should a Host ever run ANALYZE.
    con.execute("ANALYZE")
    assert _has_statistics()
    plan = _newest_valid_plan(janitor)
    assert no_sort in plan and "TEMP B-TREE" not in plan, plan


def test_the_index_is_ascending_because_the_row_id_must_run_the_same_way(janitor):
    """ORDER BY finished_at DESC, rowid DESC. The row id is the index's last key and always
    ascending, so only an ascending finished_at read backwards gives both terms; with
    finished_at DESC the row ids of equal finish times come out ascending and need a sort."""
    _fill(janitor, 60)
    con = db.conn()
    con.execute(f"DROP INDEX {INDEX}")
    con.execute(f"CREATE INDEX {INDEX} ON janitor_runs(agent_id, generation, status, finished_at DESC)")
    assert "USE TEMP B-TREE FOR LAST TERM OF ORDER BY" in _newest_valid_plan(janitor)
    con.execute(f"DROP INDEX {INDEX}")
    con.execute(INDEX_SQL)
    assert "TEMP B-TREE" not in _newest_valid_plan(janitor)


def test_the_statements_do_not_depend_on_the_index_and_match_the_measured_text(janitor):
    """What a rollback needs is that no statement names the index: then code without it runs
    the same SQL on a database that has it, and code with it on a database that lacks it. The
    comparison with the pinned text is only the tripwire described at its definition."""
    _fill(janitor, 5)
    for n in range(3):                                    # the failed branch too: all four statements
        _add(janitor, f"fail-{n}", status="failed", outcome="failed", finished_at=5000 + n)
    statements = _statements(_config(janitor))
    assert len(statements) == 4
    for sql in statements:
        assert INDEX not in sql and "INDEXED BY" not in sql.upper(), sql

    def filled(sql: str, values: tuple) -> str:
        for value in values:
            sql = sql.replace("?", f"'{value}'" if isinstance(value, str) else str(value), 1)
        return " ".join(sql.split())

    identity = (janitor["agent_id"], 1)
    assert " ".join(statements[0].split()) == filled(PINNED_NEWEST_VALID, (*identity, "scheduled-prompt"))
    assert " ".join(statements[1].split()) == filled(PINNED_LATEST, identity)


# --- it stops early ----------------------------------------------------------------

@pytest.mark.parametrize("newest_invalid", [0, 7], ids=["newest-is-valid", "seven-newer-invalid"])
def test_the_work_does_not_grow_with_the_number_of_runs(janitor, newest_invalid):
    _fill(janitor, 40, generation=1, newest_invalid=newest_invalid)
    _fill(janitor, 4000, generation=2, newest_invalid=newest_invalid)
    small, large = _steps(_config(janitor, 1)), _steps(_config(janitor, 2))
    assert large <= small + 20, (small, large)            # a hundred times the runs, the same work

    _drop_index()
    without = _steps(_config(janitor, 2))
    assert without > 50 * large, (large, without)          # the control: every run read and sorted


def test_with_no_valid_success_every_completed_run_is_still_walked(janitor):
    """No bound is promised here. The statement has no LIMIT: the caller takes the first run
    that is a success, and a janitor whose completed runs never are one (a demand worker with
    no accepted receipt) makes it walk them all, on every tick, as before the index. The
    index only spares it the sort and the runs of other generations and statuses."""
    _fill(janitor, 40, generation=1)                      # completed, outcome 'completed', no receipt
    _fill(janitor, 4000, generation=2)
    demand = "tool-explainer"                             # not a scheduled prompt: a receipt is required
    assert attention._episode(_config(janitor, 2, demand))[0].endswith(":initial")
    small, large = _steps(_config(janitor, 1, demand)), _steps(_config(janitor, 2, demand))
    assert large > 50 * small, (small, large)             # it grows with the runs: the whole walk

    _drop_index()
    assert _steps(_config(janitor, 2, demand)) > large    # though it was more work still without it


def _failing(janitor, runs: int, *, generation: int) -> None:
    for n in range(runs):
        _add(janitor, f"g{generation}-fail-{n}", generation=generation, status="failed", outcome="failed",
             finished_at=1000 + n)


def test_when_the_newest_run_failed_all_four_statements_use_the_index(janitor):
    _failing(janitor, 3000, generation=1)
    statements = _statements(_config(janitor))
    assert len(statements) == 4                           # the failed branch: newest not failed, and the count
    for sql in statements:
        assert INDEX in _plan(sql), _plan(sql)
    reference, latest, count = attention._episode(_config(janitor))
    assert (reference.rsplit(":", 1)[-1], latest["run_id"], count) == ("initial", "g1-fail-2999", 3000)


def test_when_the_newest_run_failed_the_work_still_grows_with_the_failed_runs(janitor):
    """No bound is promised here either, and nothing stops at the first match. The search for
    the newest run that did not fail passes every failed index entry of the generation, and
    the count passes every terminal entry after that run, or all of them when there is none.
    They are index entries, not rows, which is what the index buys in this branch."""
    _failing(janitor, 300, generation=1)                  # never succeeded: no newest good run
    _failing(janitor, 3000, generation=2)
    small, large = _steps(_config(janitor, 1)), _steps(_config(janitor, 2))
    assert large > 5 * small, (small, large)              # ten times the failed runs, most of ten times the work

    # The same with a good run before the failures: the count starts there, the search does not.
    _add(janitor, "g3-good", generation=3, finished_at=500)
    _add(janitor, "g4-good", generation=4, finished_at=500)
    _failing(janitor, 300, generation=3)
    _failing(janitor, 3000, generation=4)
    small, large = _steps(_config(janitor, 3)), _steps(_config(janitor, 4))
    assert attention._episode(_config(janitor, 4))[2] == 3000
    assert large > 5 * small, (small, large)

    _drop_index()
    assert _steps(_config(janitor, 4)) > large            # and more still without the index


# --- the same answers --------------------------------------------------------------

def _mixed_history(janitor) -> None:
    """Several generations; valid and invalid newest runs; ties and missing finish times."""
    rng = random.Random(20261110)
    shapes = [("completed", "completed"), ("completed", "completed"), ("completed", "changed"),
              ("completed", "same_task"), ("completed", "skipped"), ("completed", "error"),
              ("failed", "failed"), ("cancelled", "cancelled")]
    for n in range(360):
        status, outcome = rng.choice(shapes)
        _add(janitor, f"run-{n}", generation=rng.choice([1, 2, 2, 3]), status=status, outcome=outcome,
             finished_at=rng.choice([None, 5000, *range(1000, 1040)]),       # many ties, some unfinished
             template=rng.choice(["tool-explainer", "label-auditor"]),
             effect=rng.choice([None, outcome if outcome in ("changed", "same_task") else None, "insufficient_context"]),
             result=rng.choice([None, {"summary": "done"}, {"summary": " "}, {"status": "ok"}]))
    for n in range(12):                                   # generation 4 never succeeded
        _add(janitor, f"only-fail-{n}", generation=4, status="failed", outcome="failed", finished_at=2000 + n)
    _add(janitor, "active", generation=2, status="running", outcome="", finished_at=None)


def _answers(janitor) -> dict:
    con = db.conn()
    out: dict = {}
    for generation in (1, 2, 3, 4, 5):
        for template in TEMPLATES:
            reference, latest, count = attention._episode(_config(janitor, generation, template))
            identity = (janitor["agent_id"], generation)
            out[generation, template] = (
                reference, latest and (latest["run_id"], latest["sequence"]), count,
                # Every candidate, in order: the caller takes the first that passes its own check.
                [row["run_id"] for row in con.execute(PINNED_NEWEST_VALID, (*identity, template))],
                [row["run_id"] for row in con.execute(PINNED_LATEST, identity)])
    return out


def test_the_answers_are_the_same_with_and_without_the_index(janitor):
    _mixed_history(janitor)
    with_index = _answers(janitor)
    _drop_index()
    without_index = _answers(janitor)
    assert with_index == without_index
    # The fixture reaches the cases that matter.
    boundaries = {key: value[0].rsplit(":", 1)[-1] for key, value in with_index.items()}
    assert boundaries[4, "scheduled-prompt"] == boundaries[5, "task-labels"] == "initial"
    assert len({boundaries[2, template] for template in TEMPLATES}) > 1, "the templates disagree on the newest valid run"
    assert any(value[1] for value in with_index.values()), "some generation ends in a failure"
    assert max(len(value[3]) for value in with_index.values()) > 20


def test_equal_finish_times_fall_back_to_the_newest_row(janitor):
    for name in ("first", "second", "third"):
        _add(janitor, name, finished_at=7000)
    assert attention._episode(_config(janitor))[0].endswith(":third")
    _drop_index()
    assert attention._episode(_config(janitor))[0].endswith(":third")


# --- upgrading ----------------------------------------------------------------------

def _as_v121(con) -> None:
    con.execute(f"DROP INDEX {INDEX}")
    con.execute("PRAGMA user_version = 121")


def _index_rows(con) -> list[str]:
    return [" ".join(row[0].split()) for row in con.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='janitor_runs' AND name=?", (INDEX,))]


def test_upgrading_a_database_that_has_runs_builds_the_index_once(janitor):
    _mixed_history(janitor)
    con = db.conn()
    rows = con.execute("SELECT count(*), sum(rowid), group_concat(DISTINCT status) FROM janitor_runs").fetchone()
    expected = _answers(janitor)
    _as_v121(con)
    assert _index_rows(con) == []

    db_migrations._migrate(con)

    assert con.execute("PRAGMA user_version").fetchone()[0] == db_schema._SCHEMA_VERSION
    assert _index_rows(con) == [INDEX_SQL]
    assert tuple(con.execute(
        "SELECT count(*), sum(rowid), group_concat(DISTINCT status) FROM janitor_runs").fetchone()) == tuple(rows)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert _answers(janitor) == expected                  # and the index it built answers the same

    # Idempotent: the step again, and the whole upgrade again from the old version stamp.
    db_migrations._migrate_to_v122(con)
    con.execute("PRAGMA user_version = 121")
    db_migrations._migrate(con)
    assert len(_index_rows(con)) == 1
    assert con.execute("PRAGMA user_version").fetchone()[0] == db_schema._SCHEMA_VERSION


def test_code_that_stops_at_version_121_leaves_the_index_alone_and_keeps_working(janitor, monkeypatch):
    """A SIMULATED older runtime, not a real one: this is today's code with its version constant
    lowered to 121 and its v122 step removed, opening the upgraded database. It shows that
    _migrate leaves a newer database alone and that the statements work beside an index the
    code never asks for. Main's real code from before the index (5ce2ce9d) was run by hand
    against a database made by this version, with the same result; no test can import it."""
    _mixed_history(janitor)
    expected = _answers(janitor)
    con = db.conn()
    newest = db_schema._SCHEMA_VERSION                     # 122 when this was written; later steps add to it
    monkeypatch.setattr(db_schema, "_SCHEMA_VERSION", 121)
    monkeypatch.delattr(db_migrations, "_migrate_to_v122")

    db_migrations._migrate(con)                            # the database is newer: nothing to do

    assert con.execute("PRAGMA user_version").fetchone()[0] == newest
    assert len(_index_rows(con)) == 1
    assert _answers(janitor) == expected
    # It writes runs as before; the index it does not know about follows along.
    _add(janitor, "after-rollback", generation=2, finished_at=9000)
    assert attention._episode(_config(janitor, 2))[0].endswith(":after-rollback")
    con.execute("UPDATE janitor_runs SET status='failed', outcome='failed' WHERE run_id='after-rollback'")
    assert attention._episode(_config(janitor, 2))[1]["run_id"] == "after-rollback"
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_the_statements_still_work_on_a_database_without_the_index(janitor):
    """No statement names the index, so a database that lacks it is only slower."""
    _fill(janitor, 30, newest_invalid=2)
    _drop_index()
    assert attention._episode(_config(janitor))[0].endswith(":g1-ok-29")
