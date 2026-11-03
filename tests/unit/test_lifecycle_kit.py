"""The lifecycle kit (server/lib/lifecycle.py): declaration checks, the pure
decision table, compare-and-set, the reaper and the after-commit outbox."""
from __future__ import annotations

import sqlite3

import pytest

from lib import lifecycle as lk
from lib.lifecycle import Deadline, Edge, Lifecycle, WaitForever

MIN = 60_000


def _job(**overrides) -> Lifecycle:
    spec = dict(
        name="job", owner="lib/jobs.py", table="jobs", key="job_id",
        state_column="status", entered_column="status_at", initial="queued",
        final=frozenset({"done", "failed", "cancelled"}),
        edges=(
            Edge("start", frozenset({"queued"}), "running"),
            Edge("finish", frozenset({"running"}), "done"),
            Edge("fail", frozenset({"queued", "running"}), "failed"),
            Edge("cancel", frozenset({"queued", "running", "parked"}), "cancelled"),
            Edge("park", frozenset({"running"}), "parked"),
        ),
        waits={"queued": Deadline(10 * MIN, "failed"), "running": Deadline(30 * MIN, "failed"),
               "parked": WaitForever("waits for the user to answer")},
    )
    spec.update(overrides)
    return Lifecycle(**spec)


@pytest.fixture
def con():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
                       " status_at INTEGER NOT NULL, note TEXT)")
    yield connection
    connection.close()


def _state(con, key):
    return con.execute("SELECT status, status_at FROM jobs WHERE job_id = ?", (key,)).fetchone()


@pytest.mark.parametrize("state,event,target", [
    ("queued", "start", "running"),
    ("running", "finish", "done"),
    ("queued", "fail", "failed"),
    ("running", "fail", "failed"),
    ("parked", "cancel", "cancelled"),
])
def test_decide_returns_the_declared_target(state, event, target):
    assert _job().decide(state, event) == target


@pytest.mark.parametrize("state,event", [
    ("queued", "finish"), ("done", "start"), ("failed", "cancel"),
    (None, "start"), ("unknown", "start"), ("running", lk.EXPIRED),
])
def test_decide_refuses_undeclared_edges(state, event):
    with pytest.raises(lk.IllegalTransition):
        _job().decide(state, event)


@pytest.mark.parametrize("overrides,problem", [
    ({"waits": {"queued": Deadline(MIN, "failed"), "running": Deadline(MIN, "failed")}},
     "'parked' has no deadline or WaitForever"),
    ({"waits": {"queued": Deadline(MIN, "running"), "running": Deadline(MIN, "failed"),
                "parked": WaitForever("user")}}, "deadline of 'queued'"),
    ({"waits": {"queued": Deadline(0, "failed"), "running": Deadline(MIN, "failed"),
                "parked": WaitForever("user")}}, "deadline of 'queued'"),
    ({"waits": {"queued": Deadline(MIN, "failed"), "running": Deadline(MIN, "failed"),
                "parked": WaitForever(" ")}}, "WaitForever of 'parked' needs a reason"),
    ({"waits": {"queued": Deadline(MIN, "failed"), "running": Deadline(MIN, "failed"),
                "parked": WaitForever("user"), "done": Deadline(MIN, "failed")}}, "'done' has a wait"),
    ({"final": frozenset()}, "no final state"),
    ({"initial": "done"}, "initial state 'done' is final"),
    ({"table": "jobs; DROP TABLE x"}, "bad identifier"),
    ({"entered_column": ""}, "bad identifier ''"),
    ({"edges": (Edge("start", frozenset({"queued"}), "running"),
                Edge("finish", frozenset({"running"}), "done"),
                Edge("reopen", frozenset({"done"}), "running"))},
     "edge 'reopen' leaves final state 'done'"),
    ({"edges": (Edge("start", frozenset({"queued"}), "running"),
                Edge("start", frozenset({"queued"}), "done"))}, "two targets"),
    ({"edges": (Edge(lk.EXPIRED, frozenset({"queued"}), "done"),)}, "reserved for the reaper"),
    ({"edges": (Edge("start", frozenset({"queued"}), "running"),
                Edge("finish", frozenset({"running"}), "done"),
                Edge("park", frozenset({"limbo"}), "parked"))}, "'limbo' is unreachable"),
    ({"edges": (Edge("start", frozenset({"queued"}), "running"),
                Edge("park", frozenset({"running"}), "parked")),
      "waits": {"queued": Deadline(MIN, "failed"), "running": Deadline(MIN, "failed"),
                "parked": WaitForever("user")}}, "'parked' cannot reach a final state"),
])
def test_malformed_declarations_are_refused(overrides, problem):
    with pytest.raises(lk.LifecycleError, match=problem.replace("(", r"\(")):
        _job(**overrides)


def test_deadline_and_overdue():
    job = _job()
    assert job.deadline("running", 1_000) == 1_000 + 30 * MIN
    assert job.deadline("parked", 1_000) is None
    assert job.deadline("done", 1_000) is None
    assert not job.overdue("running", 0, 30 * MIN - 1)
    assert job.overdue("running", 0, 30 * MIN)
    assert not job.overdue("parked", 0, 10**12)


def test_transition_writes_target_and_entered_time(con):
    job = _job()
    with lk.transaction(con):
        lk.create(con, job, "j1", now=5, values={"note": "n"})
    with lk.transaction(con):
        moved = lk.transition(con, job, "j1", "start", now=9)
    assert moved == lk.Transition("j1", "start", "queued", "running", 9)
    assert _state(con, "j1") == ("running", 9)


def test_illegal_event_leaves_the_row_untouched(con):
    job = _job()
    with lk.transaction(con):
        lk.create(con, job, "j1", now=5)
    with pytest.raises(lk.IllegalTransition), lk.transaction(con):
        lk.transition(con, job, "j1", "finish", now=9)
    assert _state(con, "j1") == ("queued", 5)


def test_compare_and_set_with_a_stale_source_is_a_conflict(con):
    job = _job()
    with lk.transaction(con):
        lk.create(con, job, "j1", now=5)
        lk.transition(con, job, "j1", "start", now=6)
    with pytest.raises(lk.Conflict), lk.transaction(con):
        lk.compare_and_set(con, job, "j1", event="start", source="queued", target="running", now=7)
    assert _state(con, "j1") == ("running", 6)


def test_missing_row_is_illegal_not_created(con):
    with pytest.raises(lk.IllegalTransition), lk.transaction(con):
        lk.transition(con, _job(), "nope", "start", now=1)
    assert con.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_writes_outside_a_transaction_are_refused(con):
    with pytest.raises(lk.LifecycleError, match="inside lifecycle.transaction"):
        lk.create(con, _job(), "j1", now=1)


def test_reserved_and_bad_value_columns_are_refused(con):
    with pytest.raises(lk.LifecycleError, match="cannot set"), lk.transaction(con):
        lk.create(con, _job(), "j1", now=1, values={"status": "done"})
    with pytest.raises(lk.LifecycleError, match="cannot set"), lk.transaction(con):
        lk.create(con, _job(), "j1", now=1, values={"note = 1, x": "y"})


def test_reaper_moves_only_expired_rows_with_a_deadline(con):
    job = _job()
    with lk.transaction(con):
        for key, at in (("old", 0), ("fresh", 25 * MIN), ("parked", 0), ("done", 0)):
            lk.create(con, job, key, now=at)
        lk.transition(con, job, "old", "start", now=0)
        lk.transition(con, job, "fresh", "start", now=25 * MIN)
        lk.transition(con, job, "parked", "start", now=0)
        lk.transition(con, job, "parked", "park", now=0)
        lk.transition(con, job, "done", "start", now=0)
        lk.transition(con, job, "done", "finish", now=0)
    with lk.transaction(con):
        moved = lk.reap(con, job, now=30 * MIN)
    assert moved == [lk.Transition("old", lk.EXPIRED, "running", "failed", 30 * MIN)]
    assert {k: _state(con, k)[0] for k in ("old", "fresh", "parked", "done")} == {
        "old": "failed", "fresh": "running", "parked": "parked", "done": "done"}


def test_outbox_runs_after_commit_and_is_dropped_on_rollback(con):
    job, seen = _job(), []
    with lk.transaction(con) as outbox:
        lk.create(con, job, "j1", now=1)
        outbox.add(lambda: seen.append(_state(con, "j1")[0]))
        assert seen == []
    assert seen == ["queued"]
    with pytest.raises(lk.IllegalTransition), lk.transaction(con) as outbox:
        outbox.add(lambda: seen.append("must not run"))
        lk.transition(con, job, "j1", "finish", now=2)
    assert seen == ["queued"]


def test_failed_effects_surface_after_every_effect_ran(con):
    ran = []

    def boom():
        raise RuntimeError("effect")
    with pytest.raises(lk.EffectsFailed) as caught, lk.transaction(con) as outbox:
        lk.create(con, _job(), "j1", now=1)
        outbox.add(boom)
        outbox.add(lambda: ran.append(1))
    assert ran == [1]
    assert [str(e) for e in caught.value.exceptions] == ["effect"]
    assert _state(con, "j1") == ("queued", 1)


def test_in_memory_lifecycles_decide_but_have_no_sql(con):
    job = _job(table="", key="", state_column="", entered_column="")
    assert job.decide("queued", "start") == "running"
    with pytest.raises(lk.LifecycleError, match="no table"), lk.transaction(con):
        lk.create(con, job, "j1", now=1)


def test_a_failed_commit_rolls_back_and_drops_the_outbox(con):
    con.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
    con.execute("CREATE TABLE child (id TEXT REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
    con.execute("PRAGMA foreign_keys = ON")
    seen = []
    with pytest.raises(sqlite3.IntegrityError), lk.transaction(con) as outbox:
        lk.create(con, _job(), "j1", now=1)
        con.execute("INSERT INTO child VALUES ('missing')")
        outbox.add(lambda: seen.append("must not run"))
    assert not con.in_transaction
    assert seen == [] and _state(con, "j1") is None


def test_transactions_do_not_nest(con):
    con.execute("BEGIN")
    with pytest.raises(lk.LifecycleError, match="cannot nest"), lk.transaction(con):
        pass
    con.execute("ROLLBACK")
