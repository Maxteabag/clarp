"""Queue reaper contracts: old cutoff, exclusion and transaction semantics.

Written against the pre-reaper queue owner before the bounded conversion.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

import pytest

from lib import db, turn_queue

NOW = 100_000_000


def receipt(key, *, status="claimed", claimed=None, started=None, enqueued=0, agent="a"):
    db.conn().execute(
        "INSERT INTO queued_turns (queue_id, agent_id, session, text, trace_id,"
        " status, enqueued_at, claimed_at, started_at) VALUES (?, ?, 's', '', ?, ?, ?, ?, ?)",
        (key, agent, key, status, enqueued, claimed, started))


def test_excluded_prefix_cannot_hide_an_eligible_claim(monkeypatch):
    monkeypatch.setattr(db, "now_ms", lambda: NOW)
    excluded = [f"held-{n:04}" for n in range(501)]
    for key in excluded:
        receipt(key, claimed=0)
    receipt("eligible", claimed=1)
    assert turn_queue.reset_stale_claims(exclude=excluded) == 1
    assert turn_queue.status("eligible") == "queued"
    assert turn_queue.claimed_count() == 501
    assert turn_queue.revision("a") == 1


@pytest.mark.parametrize("age", [0, -1, 30_000])
def test_claim_reset_reads_its_own_clock_and_clears_only_due_stamps(monkeypatch, age):
    # The opening owner can read a cutoff one millisecond before reset does.
    times = iter((NOW, NOW + 1))
    monkeypatch.setattr(db, "now_ms", lambda: next(times, NOW + 1))
    opening_cutoff = db.now_ms() - max(0, age)
    receipt("boundary", claimed=opening_cutoff + 1)
    receipt("fresh", claimed=opening_cutoff + 2)
    receipt("missing")
    assert turn_queue.reset_stale_claims(age) == 2
    rows = [tuple(r) for r in db.conn().execute(
        "SELECT queue_id, status, claimed_at FROM queued_turns ORDER BY queue_id")]
    assert rows == [("boundary", "queued", None), ("fresh", "claimed", opening_cutoff + 2),
                    ("missing", "queued", None)]


def test_claim_reset_joins_the_callers_transaction_and_rolls_back_with_it(monkeypatch):
    monkeypatch.setattr(db, "now_ms", lambda: NOW)
    receipt("r", claimed=1)
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        assert turn_queue.reset_stale_claims() == 1
        assert con.in_transaction and turn_queue.revision("a") == 1
    finally:
        con.execute("ROLLBACK")
    assert turn_queue.status("r") == "claimed" and turn_queue.revision("a") == 0


def test_idle_age_falls_back_to_enqueued_time_when_start_is_null():
    receipt("old", status="started", enqueued=NOW - 48 * 3_600_000)
    receipt("young", status="started", enqueued=NOW - 3_600_000)
    assert turn_queue.terminalize_idle_agent_receipts(lambda _: False, now_ms=NOW) == {"ended": 1}
    assert turn_queue.status("old") == "ended" and turn_queue.status("young") == "started"


def test_claim_tick_is_bounded_and_resumes_without_losing_rows(monkeypatch):
    monkeypatch.setattr(db, "now_ms", lambda: NOW)
    for n in range(1007):
        receipt(f"r-{n:04}", claimed=1)
    con = db.conn()
    transactions, initial = [], [0]

    def trace(sql):
        if sql.startswith("BEGIN"):
            initial[0] = con.total_changes
        elif sql == "COMMIT":
            transactions.append(con.total_changes - initial[0])

    con.set_trace_callback(trace)
    counts = []
    try:
        for _ in range(20):
            count = turn_queue.reset_stale_claims()
            counts.append(count)
            if turn_queue.claimed_count() == 0:
                break
    finally:
        con.set_trace_callback(None)
    assert sum(counts) == 1007 and all(0 < n <= 500 for n in counts)
    assert transactions and max(transactions) <= 100
    assert turn_queue.revision("a") == len(counts)
    assert con.execute("SELECT count(*) FROM queued_turns WHERE claimed_at IS NOT NULL").fetchone()[0] == 0


@pytest.mark.parametrize("fail_at, committed", [(0, 0), (100, 100)])
def test_failed_batch_only_announces_previously_committed_claims(monkeypatch, fail_at, committed):
    monkeypatch.setattr(db, "now_ms", lambda: NOW)
    for n in range(170):
        receipt(f"r-{n:04}", claimed=1)
    db.conn().execute(
        f"CREATE TEMP TRIGGER refuse_reap BEFORE UPDATE ON queued_turns "
        f"WHEN OLD.queue_id = 'r-{fail_at:04}' BEGIN SELECT RAISE(ABORT, 'synthetic fault'); END")
    if committed:
        assert turn_queue.reset_stale_claims() == committed
    with pytest.raises(sqlite3.IntegrityError, match="synthetic fault"):
        turn_queue.reset_stale_claims()
    assert turn_queue.claimed_count() == 170 - committed
    assert turn_queue.revision("a") == (1 if committed else 0)


@pytest.mark.parametrize("new_kind, new_at, ended", [
    (None, None, True),
    ("thinking", NOW - 1000, False),
    ("idle", NOW - 1000, False),
])
def test_idle_rechecks_persisted_state_after_liveness_observation(new_kind, new_at, ended):
    receipt("r", status="started", started=NOW - 120_000)
    con = db.conn()
    con.execute("INSERT INTO state_log (agent_id, kind, ts) VALUES ('a', 'idle', ?)",
                (NOW - 60_000,))

    def live(agent):
        assert not con.in_transaction
        if new_kind:
            con.execute("INSERT INTO state_log (agent_id, kind, ts) VALUES (?, ?, ?)",
                        (agent, new_kind, new_at))
        return False

    assert turn_queue.terminalize_idle_agent_receipts(live, now_ms=NOW) == ({"ended": 1} if ended else {})
    assert turn_queue.status("r") == ("ended" if ended else "started")


def test_idle_tick_pages_null_starts_using_enqueued_age():
    for n in range(501):
        receipt(f"old-{n:04}", status="started", enqueued=NOW - 48 * 3_600_000)
    receipt("young", status="started", enqueued=NOW - 3_600_000)
    counts = []
    for _ in range(10):
        counts.append(turn_queue.terminalize_idle_agent_receipts(lambda _: False, now_ms=NOW).get("ended", 0))
        if sum(counts) == 501:
            break
    assert sum(counts) == 501 and max(counts) <= 500
    assert turn_queue.status("young") == "started"


@pytest.mark.timeout(600)
@pytest.mark.skipif(os.environ.get("CLARP_QUEUE_REAPER_BENCHMARK") != "1",
                    reason="production-sized synthetic reaper benchmark is opt-in")
def test_production_size_reaper_budgets(monkeypatch):
    """Remote-only evidence: 1M synthetic log rows and 1,000 open receipts.
    Includes real commits and the latest-state indexed read, never live data.
    """
    con = db.conn()
    monkeypatch.setattr(db, "now_ms", lambda: NOW)
    con.execute("BEGIN")
    try:
        for start in range(0, 1_000_000, 10_000):
            con.executemany("INSERT INTO state_log (agent_id, kind, ts, detail) VALUES (?, 'idle', ?, ?)",
                            ((f"a-{n % 1000}", NOW - 60_000 - (n // 1000) * 1000, "x" * 760)
                             for n in range(start, start + 10_000)))
        for n in range(1000):
            receipt(f"r-{n:04}", claimed=1, agent=f"a-{n}")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    plan = [row[3] for row in con.execute(
        "EXPLAIN QUERY PLAN SELECT kind, ts FROM state_log WHERE agent_id = ? "
        "ORDER BY ts DESC, state_id DESC LIMIT 1", ("a-0",))]
    assert any("idx_state_log_agent_ts" in line for line in plan), plan
    batches, ticks = [], []
    original = turn_queue.lk.transaction

    @contextmanager
    def measured(connection):
        before, started = connection.total_changes, time.perf_counter()
        with original(connection) as outbox:
            yield outbox
        batches.append((connection.total_changes - before, (time.perf_counter() - started) * 1000))

    monkeypatch.setattr(turn_queue.lk, "transaction", measured)
    total = 0
    while total < 1000:
        started = time.perf_counter()
        count = turn_queue.reset_stale_claims()
        ticks.append((count, (time.perf_counter() - started) * 1000, "claims"))
        assert count > 0
        total += count
    con.execute("UPDATE queued_turns SET status = 'started', started_at = ?", (NOW - 120_000,))
    total = 0
    while total < 1000:
        started = time.perf_counter()
        count = turn_queue.terminalize_idle_agent_receipts(lambda _: False, now_ms=NOW).get("ended", 0)
        ticks.append((count, (time.perf_counter() - started) * 1000, "idle"))
        assert count > 0
        total += count
    report = {"state_log_rows": 1_000_000, "receipts": 1000,
              "max_batch_rows": max(row[0] for row in batches),
              "max_batch_ms": max(row[1] for row in batches),
              "max_tick_rows": max(row[0] for row in ticks),
              "max_tick_ms": max(row[1] for row in ticks), "latest_state_plan": plan,
              "max_tick_ms_by_owner": {name: max(row[1] for row in ticks if row[2] == name)
                                        for name in ("claims", "idle")}}
    print(json.dumps(report, sort_keys=True))
    assert report["max_batch_rows"] <= 100 and report["max_batch_ms"] < 50, report
    assert report["max_tick_rows"] <= 500 and report["max_tick_ms"] <= 200, report
