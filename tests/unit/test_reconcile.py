"""Reconciliation layer (lib.reconcile invariants INV1-INV3).

Derived state (state_log kind, bound session, in-flight slot) must agree with
reality (live process / terminal / transcript on disk). These are
fault-injection tests: each one breaks an invariant and expects the repair."""
import pathlib
import sqlite3
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import agents as agents_db  # noqa: E402
from lib import db, message_store, reconcile, turn_dispatch, turn_lifecycle  # noqa: E402
from lib.audio_stream import AudioStream  # noqa: E402
from lib.context import ServerContext, StubSTT  # noqa: E402
from lib.snapshot import build_agent_snapshot  # noqa: E402
from lib.tts_engine import FakeTTSEngine  # noqa: E402


def _agent(tmp_path, session="mike", backend="claude"):
    agent_id = agents_db.create_agent(
        persona=session.title(), voice_id="V", cwd=str(tmp_path),
        session=session, backend=backend)
    agents_db.start_runtime(agent_id, session)
    return agent_id


def _ctx(tmp_path, session):
    return ServerContext(
        root=tmp_path, static=tmp_path, audio_dir=tmp_path / "audio",
        agents_path=tmp_path / "agents.json",
        default_session=session, tts=FakeTTSEngine(tmp_path / "audio"),
        stream=AudioStream(tmp_path / "audio"), stt=StubSTT(),
        roster_names=(session.title(),))


@pytest.fixture(autouse=True)
def _clean_dispatcher_state():
    turn_dispatch._INFLIGHT.clear()
    turn_dispatch._QUEUED.clear()
    turn_dispatch._CLAIMED_AT.clear()
    getattr(reconcile, "_BUSY_OBSERVATIONS", {}).clear()
    yield
    turn_dispatch._INFLIGHT.clear()
    turn_dispatch._QUEUED.clear()
    turn_dispatch._CLAIMED_AT.clear()
    getattr(reconcile, "_BUSY_OBSERVATIONS", {}).clear()


@pytest.fixture
def reconcile_clock(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(reconcile, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def test_inv1_stuck_thinking_without_process_is_repaired_at_snapshot_time(tmp_path, reconcile_clock):
    """Handoff fault #2: a turn died without its terminal callback. The
    snapshot must report busy=False and the stuck row must be repaired."""
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "thinking")
    first = build_agent_snapshot(_ctx(tmp_path, "mike"))
    assert next(a for a in first["agents"] if a["session"] == "mike")["busy"] is True
    assert agents_db.latest_state(agent_id)["kind"] == "thinking"
    reconcile_clock.now += 60
    snap = build_agent_snapshot(_ctx(tmp_path, "mike"))
    row = next(a for a in snap["agents"] if a["session"] == "mike")
    assert row["busy"] is False
    assert agents_db.latest_state(agent_id)["kind"] == "idle"
    assert agents_db.latest_state(agent_id)["detail"]["reason"] == "reconcile"


def test_inv1_live_process_keeps_busy(tmp_path, monkeypatch):
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "tool")
    monkeypatch.setattr(reconcile.backends, "active_handles", lambda b, a: ["proc"])
    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert repaired == {}
    assert agents_db.latest_state(agent_id)["kind"] == "tool"


def test_inv1_spawning_slot_counts_as_live(tmp_path):
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "thinking")
    turn_dispatch._INFLIGHT[agent_id] = "t-spawn"
    turn_dispatch._CLAIMED_AT[agent_id] = 1.0
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path) == {}
    assert agents_db.latest_state(agent_id)["kind"] == "thinking"


def test_inv1_background_is_not_a_process_claim(tmp_path):
    """'background' is agent-declared out-of-band work with no server-visible
    process; reconciling it away would kill the Background-task indicator."""
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "background", {"label": "Watching CI"})
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path) == {}
    assert agents_db.latest_state(agent_id)["kind"] == "background"


def _busy_turn(tmp_path):
    agent_id = _agent(tmp_path)
    agents_db.set_trace(agent_id, "reconcile-test")
    turn_id = turn_lifecycle.open_turn(
        agent_id=agent_id, source="pwa", trace_id="reconcile-test")
    turn_lifecycle.transition(agent_id, turn_lifecycle.TurnEvent.SPAWN_STARTED,
                              {"trace_id": "reconcile-test", "source": "pwa"})
    message_store.record_user_message(
        agent_id=agent_id, backend_session_id="reconcile-session",
        client_msg_id="reconcile-user", text="fixture request", trace_id="reconcile-test")
    db.conn().execute(
        """INSERT INTO queued_turns(queue_id,agent_id,session,text,trace_id,status,enqueued_at,started_at)
           VALUES ('busy-receipt',?,'mike','','reconcile-test','started',?,?)""",
        (agent_id, db.now_ms(), db.now_ms()))
    return agent_id, turn_id


def _turn_row(turn_id):
    return dict(db.conn().execute("SELECT * FROM turns WHERE turn_id = ?", (turn_id,)).fetchone())


def _agent_products(agent_id):
    return {table: [tuple(row) for row in db.conn().execute(
        f"SELECT * FROM {table} WHERE agent_id = ? ORDER BY rowid", (agent_id,))]
        for table in ("state_log", "turns", "queued_turns", "messages", "conversation_heads")}


def test_inv1_grace_boundary_repairs_the_open_turn_exactly_once(
        tmp_path, monkeypatch, reconcile_clock):
    agent_id, turn_id = _busy_turn(tmp_path)
    logs = []
    monkeypatch.setattr(reconcile, "log", lambda event, detail: logs.append((event, detail)))
    before = _agent_products(agent_id)
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert _agent_products(agent_id) == before
    assert _turn_row(turn_id)["outcome"] is None
    reconcile_clock.now += 59.999
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert _agent_products(agent_id) == before
    assert _turn_row(turn_id)["ended_at"] is None
    reconcile_clock.now = 1060.0
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)["state"] == "thinking"
    assert _turn_row(turn_id)["outcome"] == "interrupted"
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert [item for item in logs if item[0] == "reconcileStuckBusy"] == [
        ("reconcileStuckBusy", f"agent={agent_id} was=thinking → idle")]


def test_inv1_real_done_inside_grace_survives_stale_snapshot_observation(
        tmp_path, monkeypatch, reconcile_clock):
    agent_id, turn_id = _busy_turn(tmp_path)
    stale = agents_db.dashboard_states()[agent_id]
    repairs = []
    original = turn_lifecycle.transition

    def transition(aid, event, *args, **kwargs):
        if event == turn_lifecycle.TurnEvent.RECONCILE_REPAIR:
            repairs.append(aid)
        return original(aid, event, *args, **kwargs)

    monkeypatch.setattr(turn_lifecycle, "transition", transition)
    assert "state" not in reconcile.reconcile_agent(
        agent_id, "claude", home=tmp_path, observed_state=stale)
    reconcile_clock.now += 19
    turn_lifecycle.transition(agent_id, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK,
                              {"trace_id": "reconcile-test"})
    reconcile_clock.now += 60
    assert "state" not in reconcile.reconcile_agent(
        agent_id, "claude", home=tmp_path, observed_state=stale)
    snap = build_agent_snapshot(_ctx(tmp_path, "mike"))
    assert next(a for a in snap["agents"] if a["session"] == "mike")["busy"] is False
    assert agents_db.latest_state(agent_id)["kind"] == "done"
    assert _turn_row(turn_id)["outcome"] == "completed"
    assert repairs == []


def test_inv1_new_busy_row_with_identical_timestamp_and_detail_restarts_grace(
        tmp_path, reconcile_clock):
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "thinking")
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    stamp = agents_db.latest_state(agent_id)["ts"]
    reconcile_clock.now += 59
    db.conn().execute("INSERT INTO state_log(agent_id,kind,ts) VALUES (?,?,?)",
                      (agent_id, "thinking", stamp))
    reconcile_clock.now += 1
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 60
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)["state"] == "thinking"


def test_inv1_live_work_returning_discards_prior_absence(
        tmp_path, monkeypatch, reconcile_clock):
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "thinking")
    live = []
    monkeypatch.setattr(reconcile.backends, "active_handles", lambda *_: live)
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 59
    live.append("process")
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    live.clear()
    reconcile_clock.now += 1
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 60
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)["state"] == "thinking"


@pytest.mark.parametrize("invalid_time", [-1.0, float("nan"), float("inf")])
def test_inv1_invalid_or_regressed_clock_discards_eligibility(
        tmp_path, reconcile_clock, invalid_time):
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "thinking")
    reconcile_clock.now = 0.0
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now = invalid_time
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now = 60.0
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now = 120.0
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)["state"] == "thinking"


def test_inv1_backdated_new_row_does_not_replace_latest_busy_identity(
        tmp_path, reconcile_clock):
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "thinking")
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    latest = agents_db.latest_state(agent_id)
    db.conn().execute("INSERT INTO state_log(agent_id,kind,ts) VALUES (?,?,?)",
                      (agent_id, "tool", latest["ts"] - 1))
    reconcile_clock.now += 60
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)["state"] == "thinking"


def test_inv1_each_agent_has_its_own_absence_grace(tmp_path, reconcile_clock):
    first = _agent(tmp_path, "mike")
    second = _agent(tmp_path, "rachel")
    for agent_id in (first, second):
        agents_db.record_state(agent_id, "thinking")
    assert "state" not in reconcile.reconcile_agent(first, "claude", home=tmp_path)
    reconcile_clock.now += 59
    assert "state" not in reconcile.reconcile_agent(second, "claude", home=tmp_path)
    reconcile_clock.now += 1
    assert reconcile.reconcile_agent(first, "claude", home=tmp_path)["state"] == "thinking"
    assert "state" not in reconcile.reconcile_agent(second, "claude", home=tmp_path)
    reconcile_clock.now += 59
    assert reconcile.reconcile_agent(second, "claude", home=tmp_path)["state"] == "thinking"


def test_inv1_database_switch_cannot_reuse_same_agents_old_grace(tmp_path, reconcile_clock):
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "thinking")
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    copy_path = tmp_path / "other-state.sqlite"
    copied = sqlite3.connect(copy_path)
    try:
        db.conn().backup(copied)
    finally:
        copied.close()
    db.reset_for_tests(copy_path)
    reconcile_clock.now += 60
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 60
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)["state"] == "thinking"


def test_inv1_database_connection_reset_cannot_reuse_old_grace(tmp_path, reconcile_clock):
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "thinking")
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    db.reset_for_tests(db.DB_PATH)
    reconcile_clock.now += 60
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 60
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)["state"] == "thinking"


def test_inv1_capacity_bound_never_accelerates_new_candidate(
        tmp_path, monkeypatch, reconcile_clock):
    monkeypatch.setattr(reconcile, "_BUSY_MAX_OBSERVATIONS", 2, raising=False)
    agent_ids = [_agent(tmp_path, name) for name in ("one", "two", "three")]
    for agent_id in agent_ids:
        agents_db.record_state(agent_id, "thinking")
        assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
        assert len(reconcile._BUSY_OBSERVATIONS) <= 2
    reconcile_clock.now += 59.999
    for agent_id in agent_ids:
        assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
        assert len(reconcile._BUSY_OBSERVATIONS) <= 2


def test_inv1_concurrent_confirmations_repair_only_once(tmp_path, monkeypatch, reconcile_clock):
    agent_id, turn_id = _busy_turn(tmp_path)
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 60
    gate = threading.Barrier(2)
    first_probe = threading.local()
    results, errors, logs = [], [], []

    def absent(*args):
        if not getattr(first_probe, "seen", False):
            first_probe.seen = True
            gate.wait(5)
        return False

    def confirm():
        try:
            results.append(reconcile.reconcile_agent(agent_id, "claude", home=tmp_path))
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close_local()

    monkeypatch.setattr(reconcile, "has_live_work", absent)
    monkeypatch.setattr(reconcile, "log", lambda *args: logs.append(args))
    workers = [threading.Thread(target=confirm) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert all(not worker.is_alive() for worker in workers) and not errors
    assert sum("state" in result for result in results) == 1
    assert sum(event == "reconcileStuckBusy" for event, _ in logs) == 1
    assert _turn_row(turn_id)["outcome"] == "interrupted"


def test_inv1_failed_settlement_rolls_back_without_success_or_reusing_grace(
        tmp_path, monkeypatch, reconcile_clock):
    agent_id, _ = _busy_turn(tmp_path)
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 60
    before = _agent_products(agent_id)
    logs = []
    monkeypatch.setattr(reconcile, "log", lambda *args: logs.append(args))

    def failed_settlement(*args, **kwargs):
        raise RuntimeError("settlement failed")

    with monkeypatch.context() as patch:
        patch.setattr(turn_lifecycle, "_record_outcome", failed_settlement)
        with pytest.raises(RuntimeError, match="settlement failed"):
            reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert _agent_products(agent_id) == before
    assert not any(event == "reconcileStuckBusy" for event, _ in logs)
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)


@pytest.mark.parametrize("broken_check", ["terminal", "spawning"])
def test_inv1_unknown_liveness_logs_and_keeps_open_turn(
        tmp_path, monkeypatch, reconcile_clock, broken_check):
    agent_id, turn_id = _busy_turn(tmp_path)
    monkeypatch.setattr(reconcile.backends, "active_handles", lambda *_: [])
    monkeypatch.setattr(reconcile.backends, "external_live_work", lambda *_: False)
    logs = []
    monkeypatch.setattr(reconcile, "log_exception",
                        lambda event, exc, detail="": logs.append((event, str(exc), detail)))

    def broken(*args):
        raise RuntimeError("liveness unavailable")

    if broken_check == "terminal":
        monkeypatch.setattr(turn_dispatch, "live_work", broken)
    else:
        monkeypatch.setattr(turn_dispatch, "live_work", lambda *_: SimpleNamespace(terminal=False))
        monkeypatch.setattr(reconcile, "_slot_is_spawning", broken)
    assert reconcile.has_live_work(agent_id, "claude") is True
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 61
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert _turn_row(turn_id)["outcome"] is None
    assert logs and all(message == "liveness unavailable" and detail == agent_id
                        for _, message, detail in logs)


@pytest.mark.parametrize("successor", ["done", "busy_same_timestamp"])
def test_inv1_independent_row_before_writer_admission_refuses_repair(
        tmp_path, monkeypatch, reconcile_clock, successor):
    agent_id, turn_id = _busy_turn(tmp_path)
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 60
    original_conn = turn_lifecycle.conn
    reader_thread = threading.get_ident()
    injected, errors, committed = [], [], []
    logs = []
    monkeypatch.setattr(reconcile, "log", lambda *args: logs.append(args))

    def write_successor():
        try:
            if successor == "done":
                turn_lifecycle.transition(agent_id, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK,
                                          {"trace_id": "reconcile-test"})
            else:
                latest = agents_db.latest_state(agent_id)
                db.conn().execute("INSERT INTO state_log(agent_id,kind,ts) VALUES (?,?,?)",
                                  (agent_id, "thinking", latest["ts"]))
            committed.append(_agent_products(agent_id))
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close_local()

    class BeforeAdmission:
        def __init__(self, database):
            self.database = database

        def __getattr__(self, name):
            return getattr(self.database, name)

        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper() == "BEGIN IMMEDIATE" and not injected:
                injected.append(True)
                writer = threading.Thread(target=write_successor)
                writer.start()
                writer.join(5)
                assert not writer.is_alive() and not errors
            return self.database.execute(sql, *args, **kwargs)

    def connection():
        database = original_conn()
        return BeforeAdmission(database) if threading.get_ident() == reader_thread else database

    monkeypatch.setattr(turn_lifecycle, "conn", connection)
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert injected == [True] and len(committed) == 1
    assert _agent_products(agent_id) == committed[0]
    assert not any(event == "reconcileStuckBusy" for event, _ in logs)
    assert _turn_row(turn_id)["outcome"] == ("completed" if successor == "done" else None)
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert _agent_products(agent_id) == committed[0]


@pytest.mark.parametrize("broken_check", ["terminal", "spawning"])
def test_runtime_idle_receipt_keeps_started_on_unknown_reconcile_probe(
        tmp_path, monkeypatch, broken_check):
    agent_id = _agent(tmp_path)
    now = db.now_ms()
    agents_db.record_state(agent_id, "done")
    db.conn().execute("UPDATE state_log SET ts = ? WHERE agent_id = ?",
                      (now - 120_000, agent_id))
    db.conn().execute(
        """INSERT INTO queued_turns(queue_id,agent_id,session,text,trace_id,status,enqueued_at,started_at)
           VALUES ('reconcile-receipt',?,'mike','','receipt-trace','started',?,?)""",
        (agent_id, now - 240_000, now - 240_000))
    monkeypatch.setattr(reconcile.backends, "active_handles", lambda *_: [])
    monkeypatch.setattr(reconcile.backends, "external_live_work", lambda *_: False)
    logs = []
    monkeypatch.setattr(reconcile, "log_exception", lambda *args, **kwargs: logs.append(args))

    def broken(*args):
        raise RuntimeError("unknown receipt liveness")

    if broken_check == "terminal":
        monkeypatch.setattr(turn_dispatch, "live_work", broken)
    else:
        monkeypatch.setattr(turn_dispatch, "live_work", lambda *_: SimpleNamespace(terminal=False))
        monkeypatch.setattr(reconcile, "_slot_is_spawning", broken)
    assert reconcile.end_idle_agent_receipts({}) == {}
    assert db.conn().execute("SELECT status FROM queued_turns WHERE queue_id = 'reconcile-receipt'").fetchone()[0] == "started"
    assert logs and all(str(args[1]) == "unknown receipt liveness" for args in logs)


def test_inv1_confirming_live_probe_discards_mature_candidate(
        tmp_path, monkeypatch, reconcile_clock):
    agent_id, _ = _busy_turn(tmp_path)
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 60
    answers = iter((False, True))
    monkeypatch.setattr(reconcile, "has_live_work", lambda *_: next(answers))
    before = _agent_products(agent_id)
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert _agent_products(agent_id) == before
    monkeypatch.setattr(reconcile, "has_live_work", lambda *_: False)
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 60
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)["state"] == "thinking"


def test_inv1_pruned_observation_restarts_grace(tmp_path, monkeypatch, reconcile_clock):
    monkeypatch.setattr(reconcile, "_BUSY_IDLE_SECONDS", 120)
    agent_id = _agent(tmp_path)
    agents_db.record_state(agent_id, "thinking")
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 121
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 60
    assert reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)["state"] == "thinking"


@pytest.mark.parametrize("options", [
    {"force": False}, {"event": turn_lifecycle.TurnEvent.PROCESS_EXITED_OK},
    {"ts": 1}, {"kind": "idle"}, {"expect_trace": "other"},
    {"anchor": 1}, {"s_open": 1}, {"begun_at": 1},
    {"_expected_busy_state": (True, "thinking")},
    {"_expected_busy_state": (0, "thinking")},
    {"_expected_busy_state": ("1", "thinking")},
    {"_expected_busy_state": (1, "done")},
    {"_expected_busy_state": [1, "thinking"]},
])
def test_inv1_writer_token_rejects_unsupported_combinations_without_effects(tmp_path, options):
    agent_id, _ = _busy_turn(tmp_path)
    row = db.conn().execute(
        "SELECT state_id,kind FROM state_log WHERE agent_id=? ORDER BY ts DESC,state_id DESC LIMIT 1",
        (agent_id,)).fetchone()
    arguments = {"event": turn_lifecycle.TurnEvent.RECONCILE_REPAIR, "force": True,
                 "_expected_busy_state": (row["state_id"], row["kind"])}
    arguments.update(options)
    before = _agent_products(agent_id)
    with pytest.raises(ValueError):
        turn_lifecycle.transition(agent_id, **arguments)
    assert _agent_products(agent_id) == before
    assert not db.conn().in_transaction


def test_inv1_writer_token_rejects_nested_transaction_without_effects(tmp_path):
    agent_id, _ = _busy_turn(tmp_path)
    row = db.conn().execute(
        "SELECT state_id,kind FROM state_log WHERE agent_id=? ORDER BY ts DESC,state_id DESC LIMIT 1",
        (agent_id,)).fetchone()
    before = _agent_products(agent_id)
    db.conn().execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="caller transaction"):
            turn_lifecycle.transition(agent_id, turn_lifecycle.TurnEvent.RECONCILE_REPAIR,
                                      force=True, _expected_busy_state=tuple(row))
        assert _agent_products(agent_id) == before
        assert db.conn().in_transaction
    finally:
        db.conn().rollback()


@pytest.mark.parametrize("successor", [False, True])
def test_inv1_native_busy_retry_preserves_expected_row_guard(
        tmp_path, monkeypatch, reconcile_clock, successor):
    agent_id, turn_id = _busy_turn(tmp_path)
    assert "state" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    reconcile_clock.now += 60
    stamp = agents_db.latest_state(agent_id)["ts"]
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    errors, native_busy, committed = [], [], []

    def writer():
        database = db.conn()
        try:
            database.execute("BEGIN IMMEDIATE")
            held.set()
            assert release.wait(5)
            if successor:
                database.execute("INSERT INTO state_log(agent_id,kind,ts) VALUES (?,?,?)",
                                 (agent_id, "thinking", stamp))
            database.execute("COMMIT")
            committed.append(_agent_products(agent_id))
        except BaseException as exc:
            errors.append(exc)
        finally:
            if database.in_transaction:
                database.rollback()
            db.close_local()
            done.set()

    original_conn = turn_lifecycle.conn
    reader_thread = threading.get_ident()

    class Admission:
        def __init__(self, database):
            self.database = database

        def __getattr__(self, name):
            return getattr(self.database, name)

        def execute(self, sql, *args, **kwargs):
            try:
                return self.database.execute(sql, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                if sql.strip().upper() == "BEGIN IMMEDIATE" and db.is_locked_error(exc):
                    native_busy.append(True)
                    release.set()
                    assert done.wait(5) and not errors
                raise

    def connection():
        database = original_conn()
        return Admission(database) if threading.get_ident() == reader_thread else database

    database = db.conn()
    previous_timeout = database.execute("PRAGMA busy_timeout").fetchone()[0]
    database.execute("PRAGMA busy_timeout = 0")
    monkeypatch.setattr(turn_lifecycle, "SQLITE_CRITICAL_BUSY_TIMEOUT_MS", 0)
    monkeypatch.setattr(turn_lifecycle, "conn", connection)
    worker = threading.Thread(target=writer)
    worker.start()
    try:
        assert held.wait(5) and not errors
        result = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    finally:
        release.set()
        worker.join(5)
        database.execute(f"PRAGMA busy_timeout = {previous_timeout}")
    assert not worker.is_alive() and not errors and native_busy == [True]
    if successor:
        assert "state" not in result
        assert _agent_products(agent_id) == committed[0]
        assert _turn_row(turn_id)["outcome"] is None
    else:
        assert result["state"] == "thinking"
        assert _turn_row(turn_id)["outcome"] == "interrupted"


def test_inv2_ghost_claude_session_is_unbound(tmp_path):
    """Handoff fault #11 generalised: a bound session with no transcript on
    disk would be resumed forever (exits instantly, rc=0). Unbind it."""
    agent_id = _agent(tmp_path)
    agents_db.bind_backend_session(agent_id, "ghost-uuid-1")
    assert agents_db.live_backend_session(agent_id) == "ghost-uuid-1"
    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert repaired.get("ghost_session") == "ghost-uuid-1"
    assert agents_db.live_backend_session(agent_id) == ""


def test_inv2_real_transcript_keeps_binding(tmp_path):
    agent_id = _agent(tmp_path)
    agents_db.bind_backend_session(agent_id, "real-uuid-1")
    proj = tmp_path / ".claude" / "projects" / "-tmp-proj"
    proj.mkdir(parents=True)
    (proj / "real-uuid-1.jsonl").write_text('{"type":"system","subtype":"init"}\n')
    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert "ghost_session" not in repaired
    assert agents_db.live_backend_session(agent_id) == "real-uuid-1"


def test_inv2_stale_transcript_index_keeps_binding(tmp_path, monkeypatch):
    """The index can miss a transcript that is on disk (its watches outlived a
    re-pointed ~/.claude). Unbinding on that miss wiped the fleet's history."""
    from lib import claude_transcript
    agent_id = _agent(tmp_path)
    agents_db.bind_backend_session(agent_id, "real-uuid-2")
    proj = tmp_path / ".claude" / "projects" / "-tmp-proj"
    proj.mkdir(parents=True)
    (proj / "real-uuid-2.jsonl").write_text('{"type":"system","subtype":"init"}\n')
    monkeypatch.setattr(claude_transcript, "find_latest_jsonl", lambda *_a, **_k: None)
    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert "ghost_session" not in repaired
    assert agents_db.live_backend_session(agent_id) == "real-uuid-2"


def test_inv3_dead_inflight_slot_is_freed(tmp_path):
    agent_id = _agent(tmp_path)
    turn_dispatch._INFLIGHT[agent_id] = "dead-trace"
    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert repaired.get("slot") == "dead-trace"
    assert agent_id not in turn_dispatch._INFLIGHT


def test_inv3_leaves_queued_and_terminal_slots_alone(tmp_path):
    agent_id = _agent(tmp_path)
    turn_dispatch._INFLIGHT[agent_id] = "t1"
    turn_dispatch._QUEUED[agent_id] = ["spec"]
    assert turn_dispatch.free_stale_slot(agent_id) is None
    turn_dispatch._QUEUED.clear()
    turn_dispatch._INFLIGHT[agent_id] = turn_dispatch._TERMINAL_SENTINEL
    assert turn_dispatch.free_stale_slot(agent_id) is None


# --- INV3 with an external runtime ------------------------------------------
#
# The runtime owns the slot table then: the Host's own table is empty, and a
# slot the runtime still holds makes the agent look live here, so the old
# INV3 could never free anything. It asks the runtime to run its own guarded
# leak check instead, once the turn has visibly settled.

class _Runtime:
    def __init__(self, agent_id, *, releases=True, spawning=False, fail=False):
        self.agent_id = agent_id
        self.releases = releases
        self.fail = fail
        self.active = {agent_id: "tA"}
        self.spawning = [agent_id] if spawning else []
        self.asked = []

    def status(self):
        return {"active": dict(self.active), "spawning": list(self.spawning),
                "terminals": [], "queued": dict(getattr(self, "queued", {}))}

    def release_leaked_slots(self, agent_id):
        self.asked.append(agent_id)
        if self.fail:
            raise RuntimeError("runtime went away")
        if not self.releases:
            return {}
        self.active.pop(agent_id, None)
        return {agent_id: "tA"}


@pytest.fixture
def runtime_owned(monkeypatch):
    from lib import backends
    installed = []

    def install(runtime):
        backends.configure_runtime_client(runtime)
        turn_dispatch.configure_runtime_client(runtime)
        installed.append(runtime)
        return runtime

    reconcile._LEAK_CHECK_ASKED.clear()
    yield install
    backends.configure_runtime_client(None)
    turn_dispatch.configure_runtime_client(None)
    reconcile._LEAK_CHECK_ASKED.clear()


def _settled(agent_id, kind="done", ms_ago=10 * 60_000):
    from lib import db
    agents_db.record_state(agent_id, kind)
    db.conn().execute("UPDATE state_log SET ts = ts - ? WHERE agent_id = ?",
                      (ms_ago, agent_id))


def test_inv3_asks_the_runtime_to_release_a_settled_turns_slot(tmp_path, runtime_owned):
    agent_id = _agent(tmp_path)
    _settled(agent_id)
    runtime = runtime_owned(_Runtime(agent_id))

    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)

    assert runtime.asked == [agent_id]
    assert repaired.get("slot") == "tA"


@pytest.mark.parametrize("case", ["running", "fresh_settle", "no_slot"])
def test_inv3_leaves_the_runtime_alone_while_a_turn_may_be_live(
        tmp_path, runtime_owned, case):
    agent_id = _agent(tmp_path)
    if case == "running":
        _settled(agent_id, kind="thinking")
    else:
        _settled(agent_id, ms_ago=1_000 if case == "fresh_settle" else 10 * 60_000)
    runtime = runtime_owned(_Runtime(agent_id, spawning=case == "spawning"))
    if case == "no_slot":
        runtime.active.clear()

    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)

    assert runtime.asked == []
    assert "slot" not in repaired


def test_inv3_asks_the_runtime_about_a_settled_agents_spawning_slot(
        tmp_path, runtime_owned):
    # Pebble, 2026-10-10: a queued handoff failed on "database is locked" and
    # left the slot "spawning" with no launch behind it. Only the runtime
    # knows whether a launch or account recovery still owns such a slot.
    agent_id = _agent(tmp_path)
    _settled(agent_id)
    runtime = runtime_owned(_Runtime(agent_id, spawning=True))

    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)

    assert runtime.asked == [agent_id]
    assert repaired.get("slot") == "tA"


def test_inv3_asks_the_runtime_about_queued_work_with_no_owner(tmp_path, runtime_owned):
    agent_id = _agent(tmp_path)
    _settled(agent_id, kind="idle")
    runtime = _Runtime(agent_id, releases=False)
    runtime.active.clear()
    runtime.queued = {agent_id: 8}
    runtime_owned(runtime)

    reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)

    assert runtime.asked == [agent_id]


def test_inv3_asks_the_runtime_at_most_once_per_grace_window(tmp_path, runtime_owned):
    # Snapshots reconcile every agent on every read; a slot the runtime
    # decides to keep must not cost one RPC per read.
    agent_id = _agent(tmp_path)
    _settled(agent_id)
    runtime = runtime_owned(_Runtime(agent_id, releases=False))

    for _ in range(3):
        assert "slot" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)

    assert runtime.asked == [agent_id]


def test_inv3_logs_a_failed_runtime_release(tmp_path, runtime_owned, monkeypatch):
    logged = []
    monkeypatch.setattr(reconcile, "log_exception",
                        lambda event, exc, detail="": logged.append((event, str(exc))))
    agent_id = _agent(tmp_path)
    _settled(agent_id)
    runtime_owned(_Runtime(agent_id, fail=True))

    repaired = reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)

    assert "slot" not in repaired
    assert logged == [("reconcileSlotFail", "runtime went away")]


def test_reconcile_all_covers_every_agent(tmp_path, reconcile_clock):
    a = _agent(tmp_path, "mike")
    b = _agent(tmp_path, "rachel")
    agents_db.record_state(a, "thinking")
    agents_db.record_state(b, "compacting")
    assert reconcile.reconcile_all(home=tmp_path) == 0
    reconcile_clock.now += 60
    assert reconcile.reconcile_all(home=tmp_path) == 2
    assert agents_db.latest_state(a)["kind"] == "idle"
    assert agents_db.latest_state(b)["kind"] == "idle"


def test_inv3_runtime_outage_costs_no_slot_check(tmp_path, runtime_owned, monkeypatch):
    logged = []
    monkeypatch.setattr(reconcile, "log_exception",
                        lambda event, exc, detail="": logged.append(event))
    agent_id = _agent(tmp_path)
    _settled(agent_id)
    runtime = runtime_owned(_Runtime(agent_id))
    monkeypatch.setattr(runtime, "status", lambda: (_ for _ in ()).throw(
        RuntimeError("runtime restarting")))

    assert "slot" not in reconcile.reconcile_agent(agent_id, "claude", home=tmp_path)
    assert runtime.asked == [] and "reconcileSlotFail" not in logged
