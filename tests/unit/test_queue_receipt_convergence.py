"""Receipt convergence uses durable turn identity, never a second launch."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from lib import db, reconcile, turn_queue


def _receipt(queue_id="q", *, agent="a", trace="t", status="queued"):
    turn_queue.enqueue(
        queue_id=queue_id, agent_id=agent, session="session", text="synthetic work",
        trace_id=trace, client_msg_id=queue_id, synthesize_audio=False,
        origin="agent", sender_agent_id="sender")
    if status == "claimed":
        assert turn_queue.claim(queue_id)
    elif status == "started":
        turn_queue.mark_started(queue_id)
    elif status != "queued":
        db.conn().execute("UPDATE queued_turns SET status=? WHERE queue_id=?", (status, queue_id))


def _turn(*, agent="a", trace="t", outcome=None, ended=False):
    now = db.now_ms()
    return db.conn().execute(
        "INSERT INTO turns(agent_id,source,trace_id,started_at,ended_at,settled_at,outcome) "
        "VALUES (?,'synthetic',?,?,?,?,?)",
        (agent, trace, now, now if ended or outcome else None,
         now if outcome else None, outcome)).lastrowid


def _tick():
    return reconcile.end_idle_agent_receipts({"provider_turns": ["<unknown>"]})


@pytest.mark.parametrize("initial", ["queued", "claimed"])
@pytest.mark.parametrize("outcome,expected", [
    (None, "started"), ("completed", "done"), ("failed", "failed"),
    ("interrupted", "interrupted"),
])
def test_periodic_repair_converges_receipts_with_their_exact_turn(initial, outcome, expected):
    _receipt(status=initial)
    _turn(outcome=outcome)
    before = turn_queue.revision("a")
    _tick()
    assert turn_queue.status("q") == expected
    row = turn_queue.get("q")
    assert row["text"] == row["sender_agent_id"] == ""
    assert row["client_msg_id"] == "q" and row["trace_id"] == "t"
    assert turn_queue.pending_counts() == {}
    assert turn_queue.revision("a") > before
    state = turn_queue.state("a")
    _tick()
    assert turn_queue.state("a") == state


def test_lost_start_write_is_logged_and_later_repaired(monkeypatch):
    _receipt()
    _turn(outcome="completed")
    logs = []
    monkeypatch.setattr(turn_queue, "log", lambda event, detail: logs.append((event, detail)))
    holder = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with db.busy_timeout_at_most(0):
            with pytest.raises(sqlite3.OperationalError):
                turn_queue.mark_started("q")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert turn_queue.status("q") == "queued"
    assert any(event == "queueReceiptWriteFail" and "queue=q" in detail
               for event, detail in logs)
    _tick()
    assert turn_queue.status("q") == "done"
    assert any(event == "queueReceiptReconciled" and "queue=q" in detail
               for event, detail in logs)


def test_polled_start_waits_for_real_writer_without_extending_busy_budget():
    _receipt()
    ready, release = threading.Event(), threading.Event()

    def hold():
        connection = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            ready.set()
            assert release.wait(2)
            connection.execute("ROLLBACK")
        finally:
            connection.close()

    writer = threading.Thread(target=hold)
    writer.start()
    assert ready.wait(2)
    timer = threading.Timer(0.03, release.set)
    timer.start()
    try:
        with db.busy_timeout_at_most(250):
            turn_queue.mark_started("q")
            assert db.conn().execute("PRAGMA busy_timeout").fetchone()[0] == 250
        assert turn_queue.status("q") == "started"
    finally:
        release.set()
        timer.join(2)
        writer.join(2)
    assert not writer.is_alive()


def test_missing_or_foreign_turn_and_final_receipts_are_unchanged():
    _receipt("missing", trace="no-turn")
    _receipt("foreign-agent", trace="same", agent="other")
    _turn(agent="a", trace="same", outcome="completed")
    for status in ("parked", "cancelled", "withdrawn", "unknown", "done"):
        _receipt(status, status=status)
    before = [tuple(row) for row in db.conn().execute(
        "SELECT * FROM queued_turns ORDER BY queue_seq")]
    _tick()
    assert [tuple(row) for row in db.conn().execute(
        "SELECT * FROM queued_turns ORDER BY queue_seq")] == before


def test_open_retry_outranks_an_older_settled_attempt():
    _receipt()
    _turn(outcome="failed")
    active = _turn()
    _tick()
    assert turn_queue.status("q") == "started"
    db.conn().execute("UPDATE turns SET ended_at=?,settled_at=?,outcome='completed' WHERE turn_id=?",
                      (db.now_ms(), db.now_ms(), active))
    _tick()
    assert turn_queue.status("q") == "done"


def test_lost_settlement_converges_after_connection_restart():
    _receipt(status="started")
    _turn(outcome="interrupted")
    db.close_local()
    _tick()
    assert turn_queue.status("q") == "interrupted"


def test_old_claim_with_a_turn_cannot_be_released_or_claimed_for_replay():
    _receipt(status="claimed")
    _turn(outcome="completed")
    assert turn_queue.release_claim("q") is False
    assert turn_queue.reset_stale_claims(max_age_ms=0) == 0
    assert turn_queue.claim("q") is None
    assert not turn_queue.claim_for_dispatch("q")
    _tick()
    assert turn_queue.status("q") == "done"


def test_more_than_one_batch_makes_progress_past_unchanged_running_receipts():
    for index in range(205):
        _receipt(str(index), trace=str(index))
        _turn(trace=str(index), outcome=None if index < 100 else "completed")
    for _ in range(6):
        _tick()
    assert turn_queue.pending_counts() == {}
    assert turn_queue.status("204") == "done"
    assert turn_queue.status("0") == "started"


def test_steered_receipt_with_no_own_turn_is_not_bound_to_the_live_agent_turn():
    _receipt("steer", trace="follow-up")
    _turn(trace="active")
    _tick()
    assert turn_queue.status("steer") == "queued"
    assert turn_queue.get("steer")["text"] == "synthetic work"
    # The accepted-steer owner still marks its own receipt without guessing
    # a parent execution in the periodic repair.
    turn_queue.mark_started("steer")
    assert turn_queue.status("steer") == "started"
    assert turn_queue.pending_counts() == {}
