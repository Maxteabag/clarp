import hashlib
import importlib.util
import pathlib

from lib import agents, background_jobs
from tests.patching import patch_shared


_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "agent_bg.py"
_SPEC = importlib.util.spec_from_file_location("agent_bg_script", _SCRIPT)
assert _SPEC and _SPEC.loader
agent_bg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(agent_bg)


def test_message_watch_worker_argv_maps_to_exact_target_job_ids(monkeypatch):
    patch_shared(monkeypatch,
        background_jobs, "process_argv",
        lambda _pid: [
            "python3", "/skills/message-watch/scripts/watch_messages.py",
            "whatsapp", "--watch", "jid=Yoga", "--watch-name", "Studio",
        ],
    )

    ids = background_jobs.adopted_worker_job_ids(4242, "nadia-test")

    expected = []
    for label in ("Studio", "Yoga"):
        digest = hashlib.sha256(
            f"nadia-test\0whatsapp\0{label}".encode()).hexdigest()[:16]
        expected.append(f"message-watch-whatsapp-{digest}")
    assert ids == expected


def test_message_watch_provider_comes_from_subcommand_not_label(monkeypatch):
    patch_shared(monkeypatch,
        background_jobs, "process_argv",
        lambda _pid: [
            "python3", "/skills/message-watch/scripts/watch_messages.py",
            "himalaya", "--subject-keyword", "ticket=123",
        ],
    )

    ids = background_jobs.adopted_worker_job_ids(4242, "nadia-test")

    digest = hashlib.sha256(
        b"nadia-test\0email\0ticket=123").hexdigest()[:16]
    assert ids == [f"message-watch-email-{digest}"]


def test_message_watch_style_registration_captures_worker_and_gates_delivery(
    monkeypatch, capsys,
):
    agents.create_agent(
        persona="Nadia", voice_id="voice", cwd="/tmp", session="nadia-test")
    monkeypatch.setattr(
        background_jobs, "current_worker_identity",
        lambda: (4242, "boot:start"),
    )
    monkeypatch.setattr(
        background_jobs, "adopted_worker_job_ids",
        lambda _pid, _session: ["watch-whatsapp"],
    )

    assert agent_bg.main([
        "agent_bg.py", "nadia-test", "job-upsert", "watch-whatsapp",
        "whatsapp", "WhatsApp: Yoga", "Waiting for Yoga",
    ]) == 0
    handle = capsys.readouterr().out.strip()
    assert handle == "bg1:1:watch-whatsapp"
    job = background_jobs.get("watch-whatsapp", reconcile=False)
    assert job["status"] == "running"
    assert job["worker_pid"] == 4242
    assert job["worker_start_token"] == "boot:start"

    assert agent_bg.main([
        "agent_bg.py", "nadia-test", "on", "Watching replies",
    ]) == 0
    heartbeat = background_jobs.get("watch-whatsapp", reconcile=False)
    assert heartbeat["heartbeat_source"] == "worker_status"

    assert agent_bg.main([
        "agent_bg.py", "_", "job-active", handle,
    ]) == 0
    before_wrong_worker = background_jobs.get("watch-whatsapp", reconcile=False)
    monkeypatch.setattr(
        background_jobs, "current_worker_identity",
        lambda: (9999, "boot:wrong"),
    )
    assert agent_bg.main([
        "agent_bg.py", "_", "job-active", handle,
    ]) == 1
    after_wrong_worker = background_jobs.get("watch-whatsapp", reconcile=False)
    assert after_wrong_worker["revision"] == before_wrong_worker["revision"]
    monkeypatch.setattr(
        background_jobs, "current_worker_identity",
        lambda: (4242, "boot:start"),
    )
    background_jobs.cancel("watch-whatsapp")
    assert agent_bg.main([
        "agent_bg.py", "_", "job-active", handle,
    ]) == 1


def test_cancel_cleanup_handle_is_rejected_after_restart():
    agents.create_agent(
        persona="Nadia", voice_id="voice", cwd="/tmp", session="nadia-test")
    first = background_jobs.upsert(
        session="nadia-test", job_id="restartable", kind="email", title="Watch")
    old_handle = background_jobs.job_handle(first)
    background_jobs.cancel("restartable")
    assert agent_bg.main([
        "agent_bg.py", "_", "job-cancelled", old_handle,
    ]) == 0

    restarted = background_jobs.restart(
        session="nadia-test", job_id="restartable", kind="email", title="Watch")

    assert restarted["generation"] == first["generation"] + 1
    assert agent_bg.main([
        "agent_bg.py", "_", "job-cancelled", old_handle,
    ]) == 1
    assert background_jobs.get("restartable", reconcile=False)["status"] == "running"


def test_active_unavailable_is_not_cancellation(monkeypatch, capsys):
    import sqlite3
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(background_jobs, "heartbeat", locked)
    assert agent_bg.main(["bg", "fixture", "job-active", "bg1:1:fixture"]) == 2
    assert "unavailable" in capsys.readouterr().err


def test_active_missing_is_unknown():
    assert agent_bg.main(["bg", "fixture", "job-active", "bg1:1:missing"]) == 2


def test_active_recovers_then_stops_for_explicit_cancel(monkeypatch, capsys):
    import sqlite3
    agents.create_agent(persona="Probe", voice_id="voice", cwd="/tmp", session="probe")
    monkeypatch.setattr(background_jobs, "current_worker_identity", lambda: (4242, "boot:probe"))
    agent_bg.main(["bg", "probe", "job-upsert", "probe-job", "watcher", "Probe"])
    handle = capsys.readouterr().out.strip()
    real = background_jobs.heartbeat
    with monkeypatch.context() as patch:
        patch.setattr(background_jobs, "heartbeat", lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
        assert agent_bg.main(["bg", "probe", "job-active", handle]) == 2
    assert agent_bg.main(["bg", "probe", "job-active", handle]) == 0
    background_jobs.cancel("probe-job")
    assert agent_bg.main(["bg", "probe", "job-active", handle]) == 1
    assert agent_bg.main(["bg", "probe", "job-upsert", "probe-job", "watcher", "Probe"]) == 1


def test_real_sqlite_contention_is_unknown_then_recovers(monkeypatch, capsys):
    import sqlite3
    from lib import db
    agents.create_agent(persona="Probe", voice_id="voice", cwd="/tmp", session="probe")
    monkeypatch.setattr(background_jobs, "current_worker_identity", lambda: (4242, "boot:probe"))
    agent_bg.main(["bg", "probe", "job-upsert", "locked-job", "watcher", "Probe"])
    handle = capsys.readouterr().out.strip()
    lock = sqlite3.connect(str(db.DB_PATH))
    lock.execute('BEGIN IMMEDIATE')
    try:
        with db.busy_timeout(10):
            assert agent_bg.main(["bg", "probe", "job-active", handle]) == 2
    finally:
        lock.rollback()
        lock.close()
    assert agent_bg.main(["bg", "probe", "job-active", handle]) == 0
    background_jobs.cancel("locked-job")
    assert agent_bg.main(["bg", "probe", "job-active", handle]) == 1


def _state_row_count(agent_id: str) -> int:
    from lib import db
    return int(
        db.conn()
        .execute("SELECT COUNT(*) AS n FROM state_log WHERE agent_id = ?", (agent_id,))
        .fetchone()["n"]
    )


def test_background_off_preserves_a_new_interactive_turn():
    from lib import db, turn_lifecycle
    from lib.protocol import AgentState
    from lib.turn_lifecycle import TurnEvent

    turn_lifecycle.reset_for_tests()
    agent_id = agents.create_agent(
        persona="Nadia", voice_id="voice", cwd="/tmp", session="nadia-bg")
    assert agent_bg.main(["agent_bg.py", "nadia-bg", "on", "Watching CI"]) == 0
    assert turn_lifecycle.current_kind(agent_id) == AgentState.BACKGROUND
    assert agents.get_by_agent_id(agent_id)["custom_status"] == "Watching CI"

    turn_lifecycle.transition(agent_id, TurnEvent.PROMPT_ADMITTED, {"source": "pwa"})
    rows_before = _state_row_count(agent_id)

    assert agent_bg.main(["agent_bg.py", "nadia-bg", "off"]) == 0
    assert turn_lifecycle.current_kind(agent_id) == AgentState.THINKING
    assert agents.get_by_agent_id(agent_id)["custom_status"] == ""
    assert _state_row_count(agent_id) == rows_before
    assert not db.conn().in_transaction
    assert turn_lifecycle.illegal_counts() == {}


def test_background_off_targets_its_agents_latest_state():
    from lib import turn_lifecycle
    from lib.protocol import AgentState
    from lib.turn_lifecycle import TurnEvent

    target_id = agents.create_agent(
        persona="Target", voice_id="voice", cwd="/tmp", session="target-bg")
    other_id = agents.create_agent(
        persona="Other", voice_id="voice", cwd="/tmp", session="other-bg")
    assert agent_bg.main(["agent_bg.py", "target-bg", "on", "Waiting"]) == 0
    turn_lifecycle.transition(other_id, TurnEvent.PROMPT_ADMITTED, {"source": "pwa"})
    other_rows_before = _state_row_count(other_id)

    assert agent_bg.main(["agent_bg.py", "target-bg", "off"]) == 0
    assert turn_lifecycle.current_kind(target_id) == AgentState.IDLE
    assert agents.get_by_agent_id(target_id)["custom_status"] == ""
    assert turn_lifecycle.current_kind(other_id) == AgentState.THINKING
    assert _state_row_count(other_id) == other_rows_before


def test_background_off_after_done_broadcasts_status_clear_without_state_row_or_notification(
    monkeypatch,
):
    from lib import apns, turn_lifecycle, user_notifications
    from lib.protocol import AgentState, SSEType
    from lib.state_watcher import StateLogWatcher
    from lib.turn_lifecycle import TurnEvent

    classified: list[str] = []
    monkeypatch.setattr(
        user_notifications,
        "classify_completed_turn",
        lambda **kw: classified.append(kw["agent_id"]) or {"notify": False},
    )
    monkeypatch.setattr(apns, "on_user_notification", lambda _n: None)

    agent_id = agents.create_agent(
        persona="Done", voice_id="voice", cwd="/tmp", session="done-bg")
    assert agent_bg.main(["agent_bg.py", "done-bg", "on", "Building"]) == 0
    turn_lifecycle.transition(agent_id, TurnEvent.PROMPT_ADMITTED, {"source": "pwa"})
    turn_lifecycle.transition(
        agent_id, TurnEvent.PROCESS_EXITED_OK, {"backend_session_id": "bs-1"})

    class _Stream:
        def __init__(self):
            self.events = []

        def broadcast(self, event):
            self.events.append(event)

    stream = _Stream()
    watcher = StateLogWatcher(stream)
    watcher.prime()
    rows_before = _state_row_count(agent_id)

    try:
        assert agent_bg.main(["agent_bg.py", "done-bg", "off"]) == 0
        watcher._poll_once()
        assert watcher.wait_for_notifications(2)
    finally:
        watcher.stop()

    assert _state_row_count(agent_id) == rows_before
    assert len(stream.events) == 1
    assert stream.events[0]["type"] == SSEType.AGENT_STATE
    assert stream.events[0]["agent_id"] == agent_id
    assert stream.events[0]["kind"] == AgentState.DONE
    assert stream.events[0]["status_text"] == ""
    assert classified == []


def test_background_on_without_initial_state_has_a_clean_error(capsys):
    from lib import db, turn_lifecycle

    turn_lifecycle.reset_for_tests()
    agent_id = agents.create_agent(
        persona="Unseeded", voice_id="voice", cwd="/tmp", session="unseeded-bg")
    db.conn().execute("DELETE FROM state_log WHERE agent_id = ?", (agent_id,))
    capsys.readouterr()

    rc = agent_bg.main(["agent_bg.py", "unseeded-bg", "on", "Building"])
    err = capsys.readouterr().err

    assert rc == 1
    assert "agent_bg:" in err
    assert "Traceback" not in err
    assert agents.get_by_agent_id(agent_id)["custom_status"] == ""
    assert turn_lifecycle.current_kind(agent_id) == turn_lifecycle.NONE
    assert turn_lifecycle.illegal_counts() == {}


def test_background_finish_waits_for_writer_and_rechecks_agent_state():
    import sqlite3
    import threading
    import time
    from lib import db, turn_lifecycle
    from lib.protocol import AgentState
    from lib.turn_lifecycle import TurnEvent

    agent_id = agents.create_agent(
        persona="Race", voice_id="voice", cwd="/tmp", session="race-bg")
    assert agent_bg.main(["agent_bg.py", "race-bg", "on", "Building"]) == 0
    runtime_id = agents.current_runtime_id(agent_id)
    rows_before = _state_row_count(agent_id)

    writer = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        """INSERT INTO state_log (agent_id, runtime_id, ts, kind, detail)
           VALUES (?, ?, ?, ?, ?)""",
        (agent_id, runtime_id, db.now_ms() + 1, AgentState.THINKING, '{"source":"pwa"}'),
    )

    started_begin = threading.Event()
    outcomes: list[object] = []
    errors: list[BaseException] = []

    def finish_worker() -> None:
        try:
            con = db.conn()
            con.set_trace_callback(
                lambda sql: started_begin.set() if "BEGIN IMMEDIATE" in sql else None
            )
            try:
                outcomes.append(
                    turn_lifecycle.transition(agent_id, TurnEvent.BACKGROUND_FINISHED)
                )
            finally:
                con.set_trace_callback(None)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close_local()

    worker = threading.Thread(target=finish_worker)
    try:
        worker.start()
        assert started_begin.wait(5), "worker never issued BEGIN IMMEDIATE"
        time.sleep(0.02)
        assert worker.is_alive(), "worker did not block on writer lock"
        writer.execute("COMMIT")
        worker.join(timeout=5)
        assert not worker.is_alive()
    finally:
        if writer.in_transaction:
            writer.execute("ROLLBACK")
        writer.close()

    assert errors == []
    assert outcomes == [None]
    assert turn_lifecycle.current_kind(agent_id) == AgentState.THINKING
    assert _state_row_count(agent_id) == rows_before + 1
