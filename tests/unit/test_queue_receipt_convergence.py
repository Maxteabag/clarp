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
            (agent, now, json.dumps({"trace_id": trace, "source": "pwa", "dispatch": "claude"})))
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


def test_unattributed_open_retry_is_protected_until_its_later_settlement():
    _receipt()
    earlier = _turn(outcome="failed")
    active = _turn()
    earlier_before = dict(db.conn().execute("SELECT * FROM turns WHERE turn_id=?", (earlier,)).fetchone())
    before = _stored("q")
    _tick()
    assert _stored("q") == before  # unbound same-trace observations cannot name this attempt
    assert turn_queue.pending_for_dispatch() == []
    db.conn().execute("UPDATE turns SET ended_at=?,settled_at=?,outcome='completed' WHERE turn_id=?",
                      (db.now_ms(), db.now_ms(), active))
    completed = dict(db.conn().execute("SELECT * FROM turns WHERE turn_id=?", (active,)).fetchone())
    assert completed["agent_id"] == "a" and completed["trace_id"] == "t"
    assert completed["outcome"] == "completed" and completed["settled_at"] is not None
    assert completed["ended_at"] is not None and active > earlier
    assert dict(db.conn().execute("SELECT * FROM turns WHERE turn_id=?", (earlier,)).fetchone()) == earlier_before
    revision = turn_queue.revision("a")
    # The bounded owner may use one empty-tail pass to wrap before revisiting.
    _tick()
    _tick()
    assert turn_queue.status("q") == "done"
    assert _stored("q")["text"] == _stored("q")["sender_agent_id"] == ""
    assert turn_queue.revision("a") > revision
    assert dict(db.conn().execute("SELECT * FROM turns WHERE turn_id=?", (earlier,)).fetchone()) == earlier_before
    assert dict(db.conn().execute("SELECT * FROM turns WHERE turn_id=?", (active,)).fetchone()) == completed


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


def test_explicit_notstarted_turn_stays_retryable_until_real_same_trace_attempt(monkeypatch):
    from lib import agents, turn_lifecycle
    clock = [10_000]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    monkeypatch.setattr(turn_lifecycle, "now_ms", lambda: clock[0])
    _receipt()
    turn_id = turn_lifecycle.open_turn(
        agent_id="a", source="pwa", trace_id="t", synthesize_audio=False)
    assert db.conn().execute("SELECT settled_at FROM turns WHERE turn_id=?", (turn_id,)).fetchone()[0] is None
    agents.record_unlaunched_trace("a", "t", turn_id=turn_id)
    failed = db.conn().execute(
        "SELECT settled_at,outcome FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
    assert failed["settled_at"] is not None and failed["outcome"] == "failed"
    marker = json.loads(db.conn().execute(
        "SELECT detail FROM state_log WHERE json_extract(detail,'$.trace_id')=? ORDER BY state_id DESC LIMIT 1", ("t",)).fetchone()[0])
    assert marker["dispatch_not_started_turn_id"] == turn_id
    before = _stored("q")
    _tick()
    assert _stored("q") == before
    assert [row["queue_id"] for row in turn_queue.pending_for_dispatch()] == ["q"]
    assert agents.trace_launch_status("a", "t", client_msg_id="q", origin="agent",
                                     sender_agent_id="sender") == "retryable"
    clock[0] += 1_000
    _turn(outcome="completed")  # later attempt's positive state supersedes the marker
    assert agents.trace_launch_status("a", "t", client_msg_id="q", origin="agent",
                                     sender_agent_id="sender") == "observed"
    # The earlier scan passed this row. With one row, one pass can reset the
    # tail cursor and the next revisits it: require a full bounded cycle,
    # rather than assuming that a state change resets the maintenance cursor.
    _tick()
    _tick()
    assert turn_queue.status("q") == "done"


@pytest.mark.parametrize("generic_state", [False, True])
def test_bare_turn_is_uncertain_not_consumed_or_new_replay_permission(generic_state):
    _receipt()
    _turn(observed=False)
    if generic_state:
        db.conn().execute(
            "INSERT INTO state_log(agent_id,kind,ts,detail) VALUES ('a','thinking',?,?)",
            (db.now_ms(), json.dumps({"trace_id": "t"})))
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


@pytest.mark.parametrize("terminal", ["completed", "ended-only"])
def test_recovery_does_not_redispatch_more_than_one_repair_page(tmp_path, terminal):
    from tests.unit.test_turn_dispatch import _make_service
    service, backend, agent = _make_service(tmp_path)
    backend.live = False
    for index in range(105):
        _receipt(str(index), agent=agent, trace=str(index))
        turn_id = _turn(agent=agent, trace=str(index),
                        outcome="completed" if terminal == "completed" else None, ended=True)
        if terminal == "ended-only":
            # A stale outcome does not turn an ended-only row into a settlement.
            db.conn().execute("UPDATE turns SET outcome='completed' WHERE turn_id=?", (turn_id,))
    before_revision = turn_queue.revision(agent)
    original_turns = [tuple(row) for row in db.conn().execute(
        "SELECT * FROM turns WHERE agent_id=? ORDER BY turn_id", (agent,))]
    # The FIRST recovery call must fence even receipts beyond its repair page.
    assert service.recover_queued() == 0
    assert backend.spawned == []
    for _ in range(4):
        _tick()
    expected = "done" if terminal == "completed" else "ended"
    assert [turn_queue.status(str(index)) for index in range(105)] == [expected] * 105
    assert all(_stored(str(index))["text"] == _stored(str(index))["sender_agent_id"] == ""
               for index in range(105))
    assert turn_queue.pending_counts() == {}
    assert turn_queue.revision(agent) > before_revision
    # A later boot/recovery cannot resurrect the converged receipt either.
    assert service.recover_queued() == 0
    assert backend.spawned == []
    for index in range(105):
        assert not turn_queue.claim_for_dispatch(str(index))
        assert turn_queue.claim(str(index)) is None
    assert [tuple(row) for row in db.conn().execute(
        "SELECT * FROM turns WHERE agent_id=? ORDER BY turn_id", (agent,))] == original_turns


@pytest.mark.parametrize("stale_outcome", ["completed", "failed", "interrupted"])
def test_latest_ended_only_attempt_and_tied_settlements_use_deterministic_order(stale_outcome):
    _receipt()
    _turn(outcome="completed")
    ended = _turn(ended=True)
    db.conn().execute("UPDATE turns SET ended_at=?, outcome=? WHERE turn_id=?",
                      (db.now_ms()+1, stale_outcome, ended))
    _tick()
    # A stale FAILED outcome plus unbound multi-attempt observations is not
    # positive execution proof for that ended attempt; preserve its payload.
    assert turn_queue.status("q") == ("queued" if stale_outcome == "failed" else "ended")
    _receipt("tie", trace="tie")
    first = _turn(trace="tie", outcome="completed")
    second = _turn(trace="tie", outcome="interrupted")
    db.conn().execute("UPDATE turns SET settled_at=123 WHERE turn_id IN (?,?)", (first, second))
    _tick()
    assert turn_queue.status("tie") == "interrupted"


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
    turn_id = turn_lifecycle.open_turn(agent_id=agent, source="pwa", trace_id="retry-trace",
                                       synthesize_audio=False)
    assert db.conn().execute("SELECT settled_at FROM turns WHERE turn_id=?", (turn_id,)).fetchone()[0] is None
    agents.record_unlaunched_trace(agent, "retry-trace", turn_id=turn_id)
    failed = db.conn().execute(
        "SELECT settled_at,outcome FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
    assert failed["settled_at"] is not None and failed["outcome"] == "failed"
    marker = json.loads(db.conn().execute(
        "SELECT detail FROM state_log WHERE agent_id=? ORDER BY state_id DESC LIMIT 1", (agent,)).fetchone()[0])
    assert marker["dispatch_not_started_turn_id"] == turn_id
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


def test_actual_launched_failed_turn_with_stale_negative_marker_is_not_recovered(tmp_path):
    from lib import agents
    from tests.unit.test_turn_dispatch import _make_service
    service, backend, agent = _make_service(tmp_path)
    service.dispatch(text="synthetic work", requested_session="mike", trace_id="launched-failure",
                     client_msg_id="launched-failure", origin="agent", synthesize_audio=False)
    assert len(backend.spawned) == 1
    backend.spawned[0][1]["on_error"]("some unexpected parser crash")
    failed = db.conn().execute(
        "SELECT turn_id,settled_at,outcome FROM turns WHERE agent_id=? AND trace_id=?",
        (agent, "launched-failure")).fetchone()
    assert failed["settled_at"] is not None and failed["outcome"] == "failed"
    # Model the stale receipt independently; actual dispatch/callback supplied execution evidence.
    turn_queue.enqueue(queue_id="launched-failure", agent_id=agent, session="mike",
                       text="synthetic work", trace_id="launched-failure",
                       client_msg_id="launched-failure", synthesize_audio=False,
                       origin="agent", sender_agent_id="")
    agents.record_unlaunched_trace(agent, "launched-failure")
    assert "launched-failure" not in [r["queue_id"] for r in turn_queue.pending_for_dispatch()]
    assert not turn_queue.claim_for_dispatch("launched-failure")
    assert turn_queue.claim("launched-failure") is None
    backend.live = False
    assert service.recover_queued() == 0
    assert len(backend.spawned) == 1
    assert turn_queue.status("launched-failure") == "failed"
    assert db.conn().execute(
        "SELECT COUNT(*) FROM turns WHERE agent_id=? AND trace_id=?",
        (agent, "launched-failure")).fetchone()[0] == 1


@pytest.mark.parametrize("embedded_trace", [None, "foreign-trace"])
@pytest.mark.parametrize("outcome", [None, "completed"])
def test_receipt_embedded_trace_conflict_protects_payload_and_dispatch(embedded_trace, outcome):
    from lib import launch_receipts, settings_store
    _receipt()
    if outcome:
        _turn(outcome=outcome)
    launch_receipts.record_pending(agent_id="a", trace_id="t", client_msg_id="q",
                                   origin="agent", sender_agent_id="sender")
    raw = launch_receipts.get("t")
    if embedded_trace is None:
        raw.pop("trace_id")
    else:
        raw["trace_id"] = embedded_trace
    settings_store.set_text(launch_receipts._key("t"), json.dumps(raw))
    before, revision = _stored("q"), turn_queue.revision("a")
    _tick()
    assert _stored("q") == before
    assert turn_queue.revision("a") == revision
    assert turn_queue.pending_for_dispatch() == []
    assert not turn_queue.claim_for_dispatch("q")
    assert turn_queue.claim("q") is None
    assert _stored("q") == before


@pytest.mark.parametrize("stored", [None, "", "null", "[]", "invalid-json"])
@pytest.mark.parametrize("outcome", [None, "completed"])
def test_missing_receipt_is_distinct_from_present_malformed_record(stored, outcome):
    from lib import launch_receipts, settings_store
    _receipt()
    if outcome:
        _turn(outcome=outcome)
    key = launch_receipts._key("t")
    assert settings_store.get(key) is None
    if stored is not None:
        settings_store.set_text(key, stored)
    before, revision = _stored("q"), turn_queue.revision("a")
    _tick()
    if stored is None and outcome == "completed":
        assert turn_queue.status("q") == "done"
        assert _stored("q")["text"] == _stored("q")["sender_agent_id"] == ""
        assert turn_queue.revision("a") > revision
        assert turn_queue.pending_counts() == {}
    elif stored is None:
        assert _stored("q") == before
        assert turn_queue.revision("a") == revision
        assert [r["queue_id"] for r in turn_queue.pending_for_dispatch()] == ["q"]
    else:
        assert _stored("q") == before
        assert turn_queue.revision("a") == revision
        assert turn_queue.pending_for_dispatch() == []
        assert not turn_queue.claim_for_dispatch("q")
        assert turn_queue.claim("q") is None


# Backlog58 Track A: normal terminal bookkeeping must not depend on the sweeper.
def _dispatch_receipt_for_terminal_test(tmp_path):
    from lib import agents
    from tests.unit.test_turn_dispatch import _make_service
    service, backend, agent = _make_service(tmp_path)
    backend.live = False
    # An existing idle agent, not a fabricated execution/NOTSTARTED receipt.
    agents.record_state(agent, "idle")
    return service, backend, agent


def _send_terminal_test_receipt(service):
    return service.dispatch(
        text="Complete the queued request", requested_session="mike",
        trace_id="terminal-owned-trace", client_msg_id="terminal-owned-q",
        queue_if_busy=True, synthesize_audio=False)


def _terminal_test_turns(agent):
    return [dict(row) for row in db.conn().execute(
        "SELECT * FROM turns WHERE agent_id=? ORDER BY turn_id", (agent,))]


def test_normal_completion_finishes_a_receipt_after_real_start_write_exhaustion(
        tmp_path, monkeypatch):
    service, backend, agent = _dispatch_receipt_for_terminal_test(tmp_path)
    real_start = turn_queue.mark_started
    faults, diagnostics = [], []
    monkeypatch.setattr(turn_queue, "log", lambda event, detail:
                        diagnostics.append((event, detail)))

    def start_under_contention(queue_id):
        assert queue_id == "terminal-owned-q"
        assert len(backend.spawned) == 1  # the provider was actually handed work
        holder = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            with db.busy_timeout_at_most(0):
                try:
                    return real_start(queue_id)
                except sqlite3.OperationalError as exc:
                    faults.append(exc)
                    raise
        finally:
            holder.rollback()
            holder.close()

    monkeypatch.setattr(turn_queue, "mark_started", start_under_contention)
    with pytest.raises(sqlite3.OperationalError) as failure:
        _send_terminal_test_receipt(service)
    assert faults == [failure.value]
    assert db.is_locked_error(failure.value)
    assert any(event == "queueReceiptWriteFail" and "queue=terminal-owned-q" in detail
               for event, detail in diagnostics)
    queued = _stored("terminal-owned-q")
    assert queued["status"] == "queued" and queued["claimed_at"] is None
    assert queued["agent_id"] == agent and queued["trace_id"] == "terminal-owned-trace"
    assert queued["client_msg_id"] == "terminal-owned-q"
    before = _terminal_test_turns(agent)
    assert len(before) == 1 and before[0]["settled_at"] is None
    revision = turn_queue.revision(agent)
    # The real provider callback and terminal lifecycle run after lock release.
    backend.spawned[0][1]["on_result"]({"result": "Finished", "duration_ms": 1})
    after = _terminal_test_turns(agent)
    assert [row["turn_id"] for row in after] == [before[0]["turn_id"]]
    assert after[0]["outcome"] == "completed" and after[0]["settled_at"] is not None
    assert len(backend.spawned) == 1
    # No boot/periodic reconciliation, manual settlement or another dispatch.
    assert turn_queue.status("terminal-owned-q") == "done"
    terminal = _stored("terminal-owned-q")
    assert terminal["text"] == terminal["sender_agent_id"] == ""
    assert terminal["trace_id"] == queued["trace_id"]
    assert terminal["client_msg_id"] == queued["client_msg_id"]
    assert turn_queue.pending_count(agent) == 0
    assert turn_queue.revision(agent) == revision + 1
    stable = (terminal, turn_queue.state(agent), _terminal_test_turns(agent))
    backend.spawned[0][1]["on_result"]({"result": "duplicate terminal callback"})
    assert (_stored("terminal-owned-q"), turn_queue.state(agent),
            _terminal_test_turns(agent)) == stable
    assert len(backend.spawned) == 1


@pytest.mark.parametrize("joined", [False, True])
@pytest.mark.parametrize("receipt_failure", [False, True])
def test_restamp_failure_does_not_skip_independent_terminal_receipt(
        tmp_path, monkeypatch, joined, receipt_failure):
    from lib import message_writes, turn_lifecycle
    service, backend, agent = _dispatch_receipt_for_terminal_test(tmp_path)
    _send_terminal_test_receipt(service)
    assert len(backend.spawned) == 1
    assert turn_queue.status("terminal-owned-q") == "started"
    turn_queue.set_paused(agent, True)
    original = _stored("terminal-owned-q")
    state = turn_queue.state(agent)
    turns = _terminal_test_turns(agent)
    assert len(turns) == 1 and turns[0]["settled_at"] is None
    primary = RuntimeError("restamp sentinel")
    restamped, errors, denied = [], [], []

    def fail_restamp(agent_id, trace_id):
        restamped.append((agent_id, trace_id))
        raise primary

    monkeypatch.setattr(message_writes, "restamp_turn", fail_restamp)
    monkeypatch.setattr(turn_lifecycle, "log_exception",
                        lambda event, error, **kwargs: errors.append((event, error)))
    con = db.conn()

    def deny_receipt(action, table, column, database, trigger):
        if receipt_failure and action == sqlite3.SQLITE_UPDATE and table == "queued_turns":
            denied.append(column)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    if joined:
        con.execute("BEGIN IMMEDIATE")
    con.set_authorizer(deny_receipt)
    try:
        backend.spawned[0][1]["on_result"]({"result": "Finished", "duration_ms": 1})
        if joined:
            assert con.in_transaction
            con.commit()  # the ACTUAL caller commits after the bookkeeping catch
    except BaseException:
        if joined and con.in_transaction:
            con.rollback()
        raise
    finally:
        con.set_authorizer(None)
    final_turns = _terminal_test_turns(agent)
    assert [row["turn_id"] for row in final_turns] == [turns[0]["turn_id"]]
    assert final_turns[0]["outcome"] == "completed"
    assert final_turns[0]["settled_at"] is not None
    assert len(backend.spawned) == 1
    assert restamped == [(agent, "terminal-owned-trace")]
    assert any(error is primary for _, error in errors)
    if receipt_failure:
        assert denied  # independent receipt SQL was REALLY attempted after restamp failed
        assert any(isinstance(error, sqlite3.DatabaseError) for _, error in errors)
        assert _stored("terminal-owned-q") == original
    else:
        assert turn_queue.status("terminal-owned-q") == "done"
    assert turn_queue.state(agent) == state  # no ordinary-settlement bump or pause effect


def test_exhausted_normal_terminal_receipt_write_keeps_atomic_repair_fallback(
        tmp_path, monkeypatch):
    from lib import turn_lifecycle
    service, backend, agent = _dispatch_receipt_for_terminal_test(tmp_path)
    _send_terminal_test_receipt(service)
    turn_queue.set_paused(agent, True)
    original = _stored("terminal-owned-q")
    assert original["status"] == "started"
    state = turn_queue.state(agent)
    turns = _terminal_test_turns(agent)
    real_settle = turn_queue.settle_receipt
    faults, diagnostics, errors = [], [], []
    monkeypatch.setattr(turn_queue, "log", lambda event, detail:
                        diagnostics.append((event, detail)))
    monkeypatch.setattr(turn_lifecycle, "log_exception",
                        lambda event, error, **kwargs: errors.append((event, error)))

    def settle_under_contention(agent_id, trace_id, outcome):
        # This owner is AFTER the real state+outcome commit, not its blocker.
        committed = _terminal_test_turns(agent_id)
        assert committed[0]["outcome"] == "completed" and committed[0]["settled_at"] is not None
        holder = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            with db.busy_timeout_at_most(0):
                try:
                    return real_settle(agent_id, trace_id, outcome)
                except sqlite3.OperationalError as exc:
                    faults.append(exc)
                    raise
        finally:
            holder.rollback()
            holder.close()

    monkeypatch.setattr(turn_queue, "settle_receipt", settle_under_contention)
    backend.spawned[0][1]["on_result"]({"result": "Finished", "duration_ms": 1})
    assert len(faults) == 1 and db.is_locked_error(faults[0])
    assert any(error is faults[0] for _, error in errors)
    assert any(event == "queueReceiptWriteFail" and "queue=terminal-owned-q" in detail
               and "code=" in detail for event, detail in diagnostics)
    assert _stored("terminal-owned-q") == original
    assert turn_queue.state(agent) == state
    assert not db.conn().in_transaction
    # Release was in finally BEFORE the callback returned; the existing repair
    # is now the fallback, never another provider entry or replay.
    _tick()
    assert turn_queue.status("terminal-owned-q") == "done"
    assert turn_queue.is_paused(agent)
    stable = (_stored("terminal-owned-q"), turn_queue.state(agent))
    _tick()
    assert (_stored("terminal-owned-q"), turn_queue.state(agent)) == stable
    assert [row["turn_id"] for row in _terminal_test_turns(agent)] == [turns[0]["turn_id"]]
    assert len(backend.spawned) == 1
