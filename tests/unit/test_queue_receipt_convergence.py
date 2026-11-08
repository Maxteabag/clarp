"""Receipt convergence uses durable turn identity, never a second launch."""
from __future__ import annotations

import sqlite3
import threading
import json
from types import SimpleNamespace

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


def _turn(*, agent="a", trace="t", outcome=None, ended=False, observed=True):
    now = db.now_ms()
    turn_id = db.conn().execute(
        "INSERT INTO turns(agent_id,source,trace_id,started_at,ended_at,settled_at,outcome) "
        "VALUES (?,'synthetic',?,?,?,?,?)",
        (agent, trace, now, now if ended or outcome else None,
         now if outcome else None, outcome)).lastrowid
    if observed:
        db.conn().execute(
            "INSERT INTO state_log(agent_id,kind,ts,detail) VALUES (?,'thinking',?,?)",
            (agent, now, json.dumps({"trace_id": trace})))
    return turn_id


def _tick():
    return reconcile.end_idle_agent_receipts({"provider_turns": ["<unknown>"]})


def _stored(queue_id):
    """Inspect the durable receipt in any state; public get is queued-only."""
    row = db.conn().execute("SELECT * FROM queued_turns WHERE queue_id = ?",
                            (queue_id,)).fetchone()
    return dict(row) if row else None


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
    row = _stored("q")
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
    assert _stored("steer")["text"] == "synthetic work"
    # The accepted-steer owner still marks its own receipt without guessing
    # a parent execution in the periodic repair.
    turn_queue.mark_started("steer")
    assert turn_queue.status("steer") == "started"
    assert turn_queue.pending_counts() == {}


def test_explicit_notstarted_turn_stays_retryable_until_real_same_trace_attempt():
    _receipt()
    _turn(outcome="failed", observed=False)
    db.conn().execute(
        "INSERT INTO state_log(agent_id,kind,ts,detail) VALUES ('a','failed',?,?)",
        (db.now_ms(), json.dumps({"trace_id": "t", "dispatch_not_started": True})))
    before = _stored("q")
    _tick()
    assert _stored("q") == before
    assert [row["queue_id"] for row in turn_queue.pending_for_dispatch()] == ["q"]
    _turn(outcome="completed")  # later attempt's positive state supersedes the marker
    _tick()
    assert turn_queue.status("q") == "done"


def test_bare_turn_is_uncertain_not_consumed_or_new_replay_permission():
    _receipt()
    _turn(observed=False)
    before = _stored("q")
    _tick()
    assert _stored("q") == before
    assert turn_queue.claim("q") is None
    assert turn_queue.pending_for_dispatch() == []


def test_start_and_revision_rollback_together_on_revision_failure(monkeypatch):
    _receipt()
    before = turn_queue.state("a")
    def fail(_agent):
        raise sqlite3.OperationalError("synthetic revision fault")
    with monkeypatch.context() as patch:
        patch.setattr(turn_queue, "_bump_revision", fail)
        with pytest.raises(sqlite3.OperationalError, match="synthetic revision fault"):
            turn_queue.mark_started("q")
    assert turn_queue.status("q") == "queued"
    assert _stored("q")["text"] == "synthetic work"
    assert turn_queue.state("a") == before
    assert not db.conn().in_transaction


def test_start_joins_caller_transaction_without_committing_or_rolling_it_back():
    _receipt()
    before = turn_queue.state("a")
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        turn_queue.mark_started("q")
        assert con.in_transaction
        assert turn_queue.status("q") == "started"
    finally:
        con.execute("ROLLBACK")
    assert turn_queue.status("q") == "queued"
    assert turn_queue.state("a") == before


def test_normal_newer_settlement_still_corrects_terminal_receipt():
    _receipt(status="started")
    assert turn_queue.settle_receipt("a", "t", "failed") == 1
    assert turn_queue.settle_receipt("a", "t", "completed") == 1
    assert turn_queue.status("q") == "done"


def test_reconciliation_preserves_pauses_final_siblings_and_unrelated_admission():
    _receipt()
    _receipt("final", status="failed")
    _turn(outcome="completed")
    turn_queue.set_paused("a", True, user_stop=True)
    stop = (turn_queue.is_paused("a"), turn_queue.stop_generation("a"),
            turn_queue.user_stop_generation("a"))
    _tick()
    assert turn_queue.status("q") == "done"
    assert turn_queue.status("final") == "failed"
    assert (turn_queue.is_paused("a"), turn_queue.stop_generation("a"),
            turn_queue.user_stop_generation("a")) == stop


def test_actual_accepted_steer_marks_its_receipt_without_a_separate_turn(tmp_path):
    from lib.turn_command import DispatchCommand
    from tests.unit.test_turn_dispatch import _make_service, _SteerableBackends
    service, _backend, agent = _make_service(tmp_path)
    backend = _SteerableBackends()
    service.backends = backend
    service.dispatch(text="first", requested_session="mike", trace_id="first",
                     synthesize_audio=False)
    turn_queue.enqueue(
        queue_id="steered", agent_id=agent, session="mike", text="mid turn",
        trace_id="follow", client_msg_id="steered", synthesize_audio=False,
        origin="user", sender_agent_id="")
    service.submit(DispatchCommand(
        text="mid turn", requested_session="mike", trace_id="follow",
        client_msg_id="steered", durable_queue_id="steered",
        queue_if_busy=False, skip_admission=True, synthesize_audio=False))
    assert len(backend.spawned) == len(backend.steered) == 1
    assert turn_queue.status("steered") == "started"
    assert not db.conn().execute(
        "SELECT 1 FROM turns WHERE agent_id=? AND trace_id='follow'", (agent,)).fetchone()
    assert turn_queue.pending_counts() == {}


def test_recovery_does_not_redispatch_more_than_one_repair_page(tmp_path):
    from tests.unit.test_turn_dispatch import _make_service
    service, backend, agent = _make_service(tmp_path)
    backend.live = False
    for index in range(105):
        _receipt(str(index), agent=agent, trace=str(index))
        _turn(agent=agent, trace=str(index), outcome="completed")
    assert service.recover_queued() == 0
    assert backend.spawned == []
    for _ in range(4):
        _tick()
    assert turn_queue.pending_counts() == {}


@pytest.mark.parametrize("stale_outcome", ["completed", "failed", "interrupted"])
def test_latest_ended_only_attempt_and_tied_settlements_use_deterministic_order(stale_outcome):
    _receipt()
    _turn(outcome="completed")
    ended = _turn(ended=True)
    db.conn().execute("UPDATE turns SET ended_at=?, outcome=? WHERE turn_id=?",
                      (db.now_ms()+1, stale_outcome, ended))
    _tick()
    assert turn_queue.status("q") == "ended"
    _receipt("tie", trace="tie")
    first = _turn(trace="tie", outcome="completed")
    second = _turn(trace="tie", outcome="failed")
    db.conn().execute("UPDATE turns SET settled_at=123 WHERE turn_id IN (?,?)", (first, second))
    _tick()
    assert turn_queue.status("tie") == "failed"


@pytest.mark.parametrize("site,initial,expected", [
    ("start", "queued", "started"), ("settle", "started", "done"),
    ("release", "claimed", "queued"), ("reset", "claimed", "queued"),
])
def test_each_receipt_writer_reports_exhausted_admission_and_can_later_progress(
        monkeypatch, site, initial, expected):
    _receipt(status=initial)
    operations = {"start": lambda: turn_queue.mark_started("q"),
                  "settle": lambda: turn_queue.settle_receipt("a", "t", "completed"),
                  "release": lambda: turn_queue.release_claim("q"),
                  "reset": lambda: turn_queue.reset_stale_claims(0)}
    logs = []
    monkeypatch.setattr(turn_queue, "log", lambda event, detail: logs.append((event, detail)))
    holder = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with db.busy_timeout_at_most(0):
            with pytest.raises(sqlite3.OperationalError):
                operations[site]()
            assert db.conn().execute("PRAGMA busy_timeout").fetchone()[0] == 0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert turn_queue.status("q") == initial
    assert not db.conn().in_transaction
    assert any(event == "queueReceiptWriteFail" and "queue=q" in detail
               for event, detail in logs)
    operations[site]()
    assert turn_queue.status("q") == expected


def test_noncontention_write_failure_is_not_retried_and_does_not_leak_the_writer():
    _receipt()
    attempts = []
    con = db.conn()
    def deny(action, table, column, _database, _trigger):
        if action == sqlite3.SQLITE_UPDATE and table == "queued_turns":
            attempts.append(column)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    con.set_authorizer(deny)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            turn_queue.mark_started("q")
    finally:
        con.set_authorizer(None)
    assert len(attempts) == 1
    assert not con.in_transaction
    assert turn_queue.status("q") == "queued"


def test_opened_but_explicitly_unlaunched_work_uses_existing_recovery_once(tmp_path):
    from lib import agents, turn_lifecycle
    from tests.unit.test_turn_dispatch import _make_service
    service, backend, agent = _make_service(tmp_path)
    backend.live = False
    turn_queue.enqueue(
        queue_id="retry", agent_id=agent, session="mike", text="retry unsent",
        trace_id="retry-trace", client_msg_id="retry", synthesize_audio=False,
        origin="user", sender_agent_id="")
    turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="retry-trace",
                             synthesize_audio=False)
    agents.record_unlaunched_trace(agent, "retry-trace")
    assert agents.trace_launch_status(agent, "retry-trace", client_msg_id="retry",
                                     origin="user", sender_agent_id="") == "retryable"
    _tick()
    assert turn_queue.status("retry") == "queued"
    assert service.recover_queued() == 1
    assert [call["trace_id"] for _, call in backend.spawned] == ["retry-trace"]
    assert turn_queue.status("retry") == "started"
    assert service.recover_queued() == 0
    assert len(backend.spawned) == 1


@pytest.mark.parametrize("joined", [False, True], ids=["owned", "caller-owned"])
def test_both_diagnostic_sinks_failing_preserves_primary_sqlite_error(monkeypatch, joined):
    _receipt()
    con = db.conn()
    before = turn_queue.state("a")
    try:
        con.execute("SELECT nonexistent_column")
    except sqlite3.OperationalError as exc:
        primary = exc
    attempts = []
    def fail_write(*_args, **_kwargs):
        attempts.append(True)
        raise primary
    def fail_log(*_args, **_kwargs):
        raise OSError("synthetic diagnostic sink failure")
    class BrokenSink:
        def write(self, _text):
            raise BrokenPipeError("synthetic fallback sink failure")
        def flush(self):
            raise BrokenPipeError("synthetic fallback sink failure")
    if joined:
        con.execute("BEGIN IMMEDIATE")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(turn_queue, "_cas", fail_write)
            patch.setattr(turn_queue, "log", fail_log)
            patch.setattr(turn_queue, "sys", SimpleNamespace(stderr=BrokenSink()))
            with pytest.raises(sqlite3.OperationalError) as error:
                turn_queue.mark_started("q")
        assert error.value is primary
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
        assert any("queueReceiptDiagnosticLost" in note for note in primary.__notes__)
        assert len(attempts) == 1
        assert con.in_transaction is joined
        assert turn_queue.state("a") == before
        assert _stored("q")["text"] == "synthetic work"
        assert turn_queue.status("q") == "queued"
    finally:
        if joined:
            con.execute("ROLLBACK")


@pytest.mark.parametrize("outcome,observed", [
    (None, True), ("completed", True), ("failed", True),
    ("interrupted", True), (None, False),
])
def test_claimed_identity_guard_blocks_release_independently_of_reconciliation(outcome, observed):
    _receipt(status="claimed")
    _turn(outcome=outcome, observed=observed)
    before = _stored("q"), turn_queue.state("a")
    assert turn_queue.release_claim("q") is False
    assert (_stored("q"), turn_queue.state("a")) == before
    assert turn_queue.get("q") is None  # preserve its existing queued-only contract


def test_expired_preliminary_read_budget_never_acquires_a_writer(monkeypatch):
    _receipt()
    con = db.conn()
    expired, begins = [False], []
    def trace(sql):
        if sql.startswith("SELECT") and "queued_turns" in sql:
            expired[0] = True
        if sql.startswith("BEGIN"):
            begins.append(sql)
    con.set_trace_callback(trace)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(turn_queue, "time", SimpleNamespace(
                monotonic=lambda: 1.0 if expired[0] else 0.0))
            _tick()
    finally:
        con.set_trace_callback(None)
    assert expired[0] and not begins
    assert turn_queue.status("q") == "queued"
