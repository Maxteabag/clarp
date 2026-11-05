"""The lifecycle kit (server/lib/lifecycle.py): declaration checks, the pure
decision table, compare-and-set, the reaper and the after-commit outbox."""
from __future__ import annotations

import re
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
    ({"waits": {"queued": Deadline(MIN, "lost"), "running": Deadline(MIN, "failed"),
                "parked": WaitForever("user")}}, "deadline of 'queued'"),
    ({"waits": {"queued": Deadline(0, "failed"), "running": Deadline(MIN, "failed"),
                "parked": WaitForever("user")}}, "deadline of 'queued'"),
    ({"waits": {"queued": Deadline(MIN, "failed"), "running": Deadline(MIN, "failed"),
                "parked": WaitForever(" ")}}, "WaitForever of 'parked' needs a reason"),
    ({"waits": {"queued": Deadline(MIN, "failed"), "running": Deadline(MIN, "failed"),
                "parked": WaitForever("user"), "done": Deadline(MIN, "failed")}}, "'done' has a wait"),
    ({"waits": {"queued": "eventually", "running": Deadline(MIN, "failed"),
                "parked": WaitForever("user")}}, "wait of 'queued' must be a Deadline or a WaitForever"),
    ({"waits": {"queued": None, "running": Deadline(MIN, "failed"),
                "parked": WaitForever("user")}}, "'queued' has no deadline or WaitForever"),
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


def test_states_reached_only_by_a_deadline_are_reachable(con):
    lease = Lifecycle(name="lease", owner="lib/lease.py", initial="held", final=frozenset({"expired"}),
                      edges=(), waits={"held": Deadline(100, "expired")},
                      table="jobs", key="job_id", state_column="status", entered_column="status_at")
    with lk.transaction(con):
        lk.create(con, lease, "l1", now=0)
    with lk.transaction(con):
        assert lk.reap(con, lease, now=100).moved == [lk.Transition("l1", lk.EXPIRED, "held", "expired", 100)]


def test_the_documented_example_constructs():
    Lifecycle(
        name="background_job", owner="lib/background_jobs.py",
        initial="queued", final=frozenset({"done", "failed", "cancelled"}),
        edges=(Edge("start", frozenset({"queued"}), "running"),
               Edge("finish", frozenset({"running"}), "done"),
               Edge("cancel", frozenset({"queued", "running"}), "cancelled")),
        waits={"queued": Deadline(10 * MIN, "failed"), "running": Deadline(30 * MIN, "failed")},
        table="background_jobs", key="job_id", state_column="status", entered_column="status_at")


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
        moved = lk.reap(con, job, now=30 * MIN).moved
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



# Kit changes for Phase 3 (docs/architecture/lifecycle-conversion-plan.md,
# "Kit changes this plan needs"): corrections, non-final expiry, bounded reaping.

def _receipt(**overrides) -> Lifecycle:
    """The queue receipt's shape: a claim that goes stale returns to queued,
    and a newer settlement may correct an older one."""
    spec = dict(
        name="receipt", owner="lib/receipts.py", table="jobs", key="job_id",
        state_column="status", entered_column="status_at", initial="queued",
        final=frozenset({"done", "failed", "cancelled"}),
        edges=(
            Edge("claim", frozenset({"queued"}), "claimed"),
            Edge("start", frozenset({"claimed"}), "running"),
            Edge("finish", frozenset({"running"}), "done"),
            Edge("fail", frozenset({"running"}), "failed"),
            Edge("cancel", frozenset({"queued", "claimed"}), "cancelled"),
            Edge("settle_done", frozenset({"failed"}), "done", correction=True),
        ),
        waits={"queued": WaitForever("dispatch or cancel ends it"),
               "claimed": Deadline(30_000, "queued"),
               "running": Deadline(30 * MIN, "failed")},
    )
    spec.update(overrides)
    return Lifecycle(**spec)


def _at(con, lc, key, *events, now=0):
    with lk.transaction(con):
        lk.create(con, lc, key, now=now)
        for event in events:
            lk.transition(con, lc, key, event, now=now)


def test_a_declared_correction_rewrites_one_final_state_to_another(con):
    lc = _receipt()
    _at(con, lc, "r1", "claim", "start", "fail", now=5)
    with lk.transaction(con):
        assert lk.transition(con, lc, "r1", "settle_done", now=9) == lk.Transition("r1", "settle_done", "failed", "done", 9)
    assert _state(con, "r1") == ("done", 9)


def test_a_correction_that_already_holds_is_a_no_op(con):
    lc = _receipt()
    _at(con, lc, "r1", "claim", "start", "finish", now=5)
    with lk.transaction(con):
        assert lk.transition(con, lc, "r1", "settle_done", now=9) == lk.Transition("r1", "settle_done", "done", "done", 9)
    assert _state(con, "r1") == ("done", 5)  # nothing written, entered_at unchanged


def test_corrections_cover_only_the_declared_pairs(con):
    lc = _receipt()
    _at(con, lc, "r1", "cancel")
    with pytest.raises(lk.IllegalTransition), lk.transaction(con):
        lk.transition(con, lc, "r1", "settle_done", now=9)   # cancelled -> done is not declared
    with pytest.raises(lk.IllegalTransition):
        lc.decide("running", "settle_done")                  # nor from a non-final state


@pytest.mark.parametrize("edge, problem", [
    (Edge("fix", frozenset({"running"}), "done", correction=True), "must run between final states, not ['running']"),
    (Edge("fix", frozenset({"failed"}), "queued", correction=True), "must run between final states, not ['queued']"),
    (Edge("fix", frozenset({"failed", "done"}), "done", correction=True), "lists its own target 'done'"),
    (Edge("reopen", frozenset({"done"}), "queued"), "edge 'reopen' leaves final state 'done'"),
    (Edge("settle_done", frozenset({"running"}), "done"), "event 'settle_done' is both a correction and an ordinary edge"),
])
def test_malformed_corrections_are_refused(edge, problem):
    with pytest.raises(lk.LifecycleError, match=re.escape(problem)):
        _receipt(edges=_receipt().edges + (edge,))


def test_a_deadline_may_return_to_a_non_final_state(con):
    lc = _receipt()
    _at(con, lc, "r1", "claim", now=0)
    with lk.transaction(con):
        assert lk.reap(con, lc, now=30_000).moved == [lk.Transition("r1", lk.EXPIRED, "claimed", "queued", 30_000)]
    assert _state(con, "r1") == ("queued", 30_000)


@pytest.mark.parametrize("waits, problem", [
    ({"claimed": Deadline(30_000, "claimed")}, "deadline of 'claimed' must be positive and end in another declared state"),
    ({"claimed": Deadline(30_000, "nowhere")}, "deadline of 'claimed' must be positive and end in another declared state"),
    ({"queued": Deadline(1, "claimed"), "claimed": Deadline(1, "queued")}, "deadlines alone cycle: claimed -> queued -> claimed"),
])
def test_bad_non_final_expiries_are_refused(waits, problem):
    with pytest.raises(lk.LifecycleError, match=re.escape(problem)):
        _receipt(waits={**_receipt().waits, **waits})


def _many(con, lc, count, *, state_events=("claim",), now=0):
    with lk.transaction(con):
        for i in range(count):
            lk.create(con, lc, f"k{i:03d}", now=now)
            for event in state_events:
                lk.transition(con, lc, f"k{i:03d}", event, now=now + i)


def test_reap_is_bounded_and_resumes_from_its_cursor(con):
    lc = _receipt()
    _many(con, lc, 5)
    with lk.transaction(con):
        first = lk.reap(con, lc, now=MIN, limit=2)
    assert [t.key for t in first.moved] == ["k000", "k001"] and first.cursor == ("claimed", 1, "k001")
    with lk.transaction(con):
        second = lk.reap(con, lc, now=MIN, limit=2, after=first.cursor)
    assert [t.key for t in second.moved] == ["k002", "k003"]
    with lk.transaction(con):
        last = lk.reap(con, lc, now=MIN, limit=2, after=second.cursor)
    assert [t.key for t in last.moved] == ["k004"] and last.cursor is None


def test_eligible_skips_count_and_are_seen_again_after_the_wrap(con):
    lc = _receipt()
    _many(con, lc, 3)
    held = {"k001"}
    with lk.transaction(con):
        batch = lk.reap(con, lc, now=MIN, limit=10, eligible=lambda row: None if row["job_id"] in held else lk.EXPIRED)
    assert batch.examined == 3 and [t.key for t in batch.moved] == ["k000", "k002"] and batch.cursor is None
    held.clear()
    with lk.transaction(con):
        again = lk.reap(con, lc, now=MIN, limit=10, eligible=lambda row: lk.EXPIRED)
    assert [t.key for t in again.moved] == ["k001"]


def test_eligible_can_choose_another_legal_event(con):
    lc = _receipt()
    _many(con, lc, 2)
    with lk.transaction(con):
        batch = lk.reap(con, lc, now=MIN, eligible=lambda row: "cancel" if row["job_id"] == "k000" else lk.EXPIRED)
    assert [(t.key, t.target) for t in batch.moved] == [("k000", "cancelled"), ("k001", "queued")]
    _at(con, lc, "x", "claim", now=0)
    with pytest.raises(lk.IllegalTransition), lk.transaction(con):
        lk.reap(con, lc, now=2 * MIN, eligible=lambda row: "finish")   # finish is not legal from claimed
    assert _state(con, "x") == ("claimed", 0)                          # and the batch rolled back


def test_reap_tick_keeps_to_its_row_budget_one_transaction_per_batch(con):
    lc = _receipt()
    _many(con, lc, 7)
    begins = []
    con.set_trace_callback(lambda sql: begins.append(sql) if sql.startswith("BEGIN") else None)
    queued = []
    moved, cursor = lk.reap_tick(con, lc, now=MIN, batch=2, max_rows=5,
                                 effects=lambda outbox, batch: outbox.add(lambda: queued.append(len(batch))))
    con.set_trace_callback(None)
    assert len(moved) == 5 and cursor == ("claimed", 4, "k004")
    assert len(begins) == 3 and queued == [2, 2, 1]          # batches of 2, 2 and the remaining 1
    rest, cursor = lk.reap_tick(con, lc, now=MIN, cursor=cursor, batch=2, max_rows=5)
    assert [t.key for t in rest] == ["k005", "k006"] and cursor is None


def test_reap_tick_stops_at_its_time_budget(con):
    lc = _receipt()
    _many(con, lc, 6)
    ticks = iter(range(0, 1000, 60))                         # each batch "takes" 60 ms
    clock = lambda: next(ticks) / 1000.0
    moved, cursor = lk.reap_tick(con, lc, now=MIN, batch=1, max_rows=500, max_ms=200, clock=clock)
    assert len(moved) == 4 and cursor == ("claimed", 3, "k003")  # 60+60+60 < 200, the 4th reaches 240



def test_an_ordinary_self_loop_is_written_not_skipped(con):
    """Review A2 (60a93ae3, F1): only an already-applied correction is a no-op;
    a lease renewal that stays in its state still writes entered_at and its
    values, so the reaper counts the deadline from the renewal."""
    lc = _receipt(edges=_receipt().edges + (Edge("renew", frozenset({"running"}), "running"),))
    _at(con, lc, "r1", "claim", "start", now=10)
    with lk.transaction(con):
        assert lk.transition(con, lc, "r1", "renew", now=900, values={"note": "renewed"}) == \
            lk.Transition("r1", "renew", "running", "running", 900)
    assert con.execute("SELECT status, status_at, note FROM jobs WHERE job_id = 'r1'").fetchone() == \
        ("running", 900, "renewed")
    with lk.transaction(con):
        assert lk.reap(con, lc, now=10 + 30 * MIN).moved == []     # counted from the renewal at 900


# Kit changes 5 and 6 (lifecycle-conversion-plan.md, queue design delta):
# per-state entry columns and a set-based compare-and-set.

def _queue(**overrides) -> Lifecycle:
    """queued_turns' shape: only claimed and started record when they began."""
    spec = dict(
        name="queue", owner="lib/q.py", table="q", key="id", state_column="status",
        entered_columns={"claimed": "claimed_at", "started": "started_at"},
        initial="queued", final=frozenset({"done", "failed", "cancelled"}),
        edges=(
            Edge("claim", frozenset({"queued"}), "claimed"),
            Edge("release", frozenset({"claimed"}), "queued"),
            Edge("start", frozenset({"queued", "claimed"}), "started"),
            Edge("cancel", frozenset({"queued", "claimed"}), "cancelled"),
            Edge("settle_done", frozenset({"started"}), "done"),
            Edge("settle_failed", frozenset({"started"}), "failed"),
            Edge("resettle_done", frozenset({"failed"}), "done", correction=True),
        ),
        waits={"queued": WaitForever("dispatch or cancel ends it"),
               "claimed": Deadline(30_000, "queued"),
               "started": WaitForever("its turn's settlement ends it")},
    )
    spec.update(overrides)
    return Lifecycle(**spec)


@pytest.fixture
def qcon():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("CREATE TABLE q (id TEXT PRIMARY KEY, status TEXT NOT NULL, claimed_at INTEGER,"
                       " started_at INTEGER, trace TEXT NOT NULL DEFAULT 't1', note TEXT)")
    yield connection
    connection.close()


def _q(con, key):
    return con.execute("SELECT status, claimed_at, started_at, note FROM q WHERE id = ?", (key,)).fetchone()


def test_each_state_stamps_only_its_own_entry_column(qcon):
    lc = _queue()
    with lk.transaction(qcon):
        lk.create(qcon, lc, "a", now=1)
    assert _q(qcon, "a") == ("queued", None, None, None)               # queued records no time
    with lk.transaction(qcon):
        lk.transition(qcon, lc, "a", "claim", now=5)
    assert _q(qcon, "a") == ("claimed", 5, None, None)
    with lk.transaction(qcon):
        lk.transition(qcon, lc, "a", "release", now=7, values={"claimed_at": None})
    assert _q(qcon, "a") == ("queued", None, None, None)               # clearing it is an explicit value
    with lk.transaction(qcon):
        lk.transition(qcon, lc, "a", "start", now=9)
    assert _q(qcon, "a") == ("started", None, 9, None)
    with pytest.raises(lk.LifecycleError, match="cannot set"), lk.transaction(qcon):
        lk.transition(qcon, lc, "a", "settle_done", now=11, values={"status": "x"})


def test_a_values_write_to_the_target_stamp_is_refused(qcon):
    lc = _queue()
    with lk.transaction(qcon):
        lk.create(qcon, lc, "a", now=1)
    with pytest.raises(lk.LifecycleError, match=re.escape("cannot set ['claimed_at']")), lk.transaction(qcon):
        lk.transition(qcon, lc, "a", "claim", now=5, values={"claimed_at": 99})


def test_the_reaper_reads_the_source_states_own_column(qcon):
    lc = _queue()
    with lk.transaction(qcon):
        for key, at in (("old", 0), ("fresh", 20_000)):
            lk.create(qcon, lc, key, now=at)
            lk.transition(qcon, lc, key, "claim", now=at)
        lk.create(qcon, lc, "started", now=0)
        lk.transition(qcon, lc, "started", "start", now=0)
        qcon.execute("INSERT INTO q (id, status) VALUES ('stampless', 'claimed')")   # claimed_at NULL
    with lk.transaction(qcon):
        batch = lk.reap(qcon, lc, now=30_000)
    # A NULL entry time counts as 0, so the stampless claim is overdue (review A3,
    # queue design delta F2: today's claim reset uses COALESCE(claimed_at, 0)).
    assert [t.key for t in batch.moved] == ["old", "stampless"] and batch.cursor is None  # both at 0, by key
    assert _q(qcon, "old") == ("queued", 0, None, None)   # leaving claimed does not clear it by itself


def test_the_reaper_keyset_works_across_null_entry_times(qcon):
    lc = _queue()
    with lk.transaction(qcon):
        for key in ("n1", "n2", "n3"):
            qcon.execute("INSERT INTO q (id, status) VALUES (?, 'claimed')", (key,))
    with lk.transaction(qcon):
        first = lk.reap(qcon, lc, now=30_000, limit=2)
    assert [t.key for t in first.moved] == ["n1", "n2"] and first.cursor == ("claimed", 0, "n2")
    with lk.transaction(qcon):
        rest = lk.reap(qcon, lc, now=30_000, limit=2, after=first.cursor)
    assert [t.key for t in rest.moved] == ["n3"] and rest.cursor is None


@pytest.mark.parametrize("overrides, problem", [
    ({"entered_column": "status_at"}, "use entered_column or entered_columns, not both"),
    ({"entered_columns": {"claimed": "claimed_at", "nowhere": "x_at"}}, "entered_columns names unknown state 'nowhere'"),
    ({"entered_columns": {"started": "started_at"}}, "state 'claimed' has a deadline but no entered column"),
    ({"entered_columns": {"claimed": "claimed at"}}, "bad identifier 'claimed at'"),
    ({"table": "", "key": "", "state_column": ""}, "entered_columns needs a table"),
])
def test_bad_entered_columns_are_refused(overrides, problem):
    with pytest.raises(lk.LifecycleError, match=re.escape(problem)):
        _queue(**overrides)


def _rows_in(con, *states_by_key):
    for key, state in states_by_key:
        con.execute("INSERT INTO q (id, status, trace) VALUES (?, ?, ?)", (key, state, key.split("-")[0]))


def test_compare_and_set_where_moves_only_declared_sources_matching_the_filter(qcon):
    lc = _queue()
    _rows_in(qcon, ("t1-a", "queued"), ("t1-b", "claimed"), ("t1-c", "started"), ("t1-d", "done"),
             ("t2-a", "queued"))
    with lk.transaction(qcon):
        moved = lk.compare_and_set_where(qcon, lc, "cancel", where="trace = ?", params=("t1",), now=50,
                                         values={"note": "x"})
    assert moved == 2
    assert {k: _q(qcon, k)[0] for k in ("t1-a", "t1-b", "t1-c", "t1-d", "t2-a")} == {
        "t1-a": "cancelled", "t1-b": "cancelled", "t1-c": "started", "t1-d": "done", "t2-a": "queued"}


def test_compare_and_set_where_groups_events_by_target_and_stamps_each(qcon):
    lc = _queue()
    _rows_in(qcon, ("t1-a", "started"), ("t1-b", "failed"), ("t1-c", "done"), ("t1-d", "cancelled"))
    with lk.transaction(qcon):
        rows = lk.compare_and_set_where(qcon, lc, ("settle_done", "resettle_done"), where="trace = ?",
                                        params=("t1",), now=60, returning="id")
    assert sorted(r[0] for r in rows) == ["t1-a", "t1-b"]                 # done stays, cancelled untouched
    assert [_q(qcon, k)[0] for k in ("t1-a", "t1-b", "t1-c", "t1-d")] == ["done", "done", "done", "cancelled"]
    with lk.transaction(qcon):
        assert lk.compare_and_set_where(qcon, lc, "claim", where="1", now=70, returning="id") == []


def test_compare_and_set_where_refuses_unknown_events_and_bad_returning(qcon):
    lc = _queue()
    with pytest.raises(lk.LifecycleError, match="no edge for event"), lk.transaction(qcon):
        lk.compare_and_set_where(qcon, lc, "nope", where="1", now=1)
    with pytest.raises(lk.LifecycleError, match="bad returning"), lk.transaction(qcon):
        lk.compare_and_set_where(qcon, lc, "cancel", where="1", now=1, returning="id; DROP TABLE q")


def test_compare_and_set_where_stamps_the_target_column(qcon):
    lc = _queue()
    _rows_in(qcon, ("t1-a", "queued"), ("t1-b", "claimed"))
    with lk.transaction(qcon):
        assert lk.compare_and_set_where(qcon, lc, "start", where="1", now=80) == 2
    assert [_q(qcon, k) for k in ("t1-a", "t1-b")] == [("started", None, 80, None), ("started", None, 80, None)]


def _traced(con):
    seen = []
    con.set_trace_callback(seen.append)
    return seen


def test_compare_and_set_where_outside_a_transaction_is_one_autocommit_statement(qcon):
    """Review A3 (queue design delta F1): today's writers run in autocommit;
    a single target stays one statement, with no BEGIN of its own."""
    lc = _queue()
    _rows_in(qcon, ("t1-a", "queued"))
    seen = _traced(qcon)
    assert lk.compare_and_set_where(qcon, lc, "claim", where="id = ?", params=("t1-a",), now=5) == 1
    qcon.set_trace_callback(None)
    assert len(seen) == 1 and seen[0].startswith("UPDATE") and not qcon.in_transaction


def test_compare_and_set_where_wraps_several_targets_in_one_transaction(qcon):
    lc = _queue()
    _rows_in(qcon, ("t1-a", "started"), ("t1-b", "failed"))
    seen = _traced(qcon)
    lk.compare_and_set_where(qcon, lc, ("settle_failed", "resettle_done"), where="1", now=6)
    qcon.set_trace_callback(None)
    assert [x.split()[0] for x in seen] == ["BEGIN", "UPDATE", "UPDATE", "COMMIT"]
    assert [_q(qcon, k)[0] for k in ("t1-a", "t1-b")] == ["failed", "done"]


def test_compare_and_set_where_joins_the_callers_transaction(qcon):
    lc = _queue()
    _rows_in(qcon, ("t1-a", "started"), ("t1-b", "failed"))
    qcon.execute("BEGIN IMMEDIATE")
    seen = _traced(qcon)
    lk.compare_and_set_where(qcon, lc, ("settle_failed", "resettle_done"), where="1", now=6)
    qcon.set_trace_callback(None)
    assert [x.split()[0] for x in seen] == ["UPDATE", "UPDATE"]          # no nested BEGIN or COMMIT
    qcon.execute("ROLLBACK")                                             # the caller's rollback undoes both
    assert [_q(qcon, k)[0] for k in ("t1-a", "t1-b")] == ["started", "failed"]
