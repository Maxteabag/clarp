import hashlib
import importlib.util
import pathlib
import sqlite3
import threading
import time

from lib import agents, background_jobs
from lib import db
from lib import state_watcher
from lib import turn_lifecycle
from lib.protocol import AgentState, SSEType
from lib.turn_lifecycle import TurnEvent
from tests.patching import patch_shared


_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "agent_bg.py"
_SPEC = importlib.util.spec_from_file_location("agent_bg_script", _SCRIPT)
assert _SPEC and _SPEC.loader
agent_bg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(agent_bg)


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


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


def test_background_off_preserves_a_new_interactive_turn(tmp_path):
    agent_id = agents.create_agent(
        persona="Nadia", voice_id="voice", cwd=str(tmp_path), session="nadia-bg")

    assert agent_bg.main(["agent_bg.py", "nadia-bg", "on", "Watching replies"]) == 0
    assert turn_lifecycle.current_kind(agent_id) == AgentState.BACKGROUND
    assert agent_bg.main(["agent_bg.py", "nadia-bg", "off"]) == 0
    assert turn_lifecycle.current_kind(agent_id) == AgentState.IDLE

    assert agent_bg.main(["agent_bg.py", "nadia-bg", "on", "Watching replies"]) == 0
    turn_lifecycle.transition(agent_id, TurnEvent.PROMPT_ADMITTED, {"source": "pwa"})
    assert turn_lifecycle.current_kind(agent_id) == AgentState.THINKING

    assert agent_bg.main(["agent_bg.py", "nadia-bg", "off"]) == 0
    assert turn_lifecycle.current_kind(agent_id) == AgentState.THINKING
    assert agents.get_by_session("nadia-bg")["custom_status"] == ""
    assert agents.latest_state(agent_id)["detail"]["status_text_cleared"] is True
    assert not db.conn().in_transaction


def test_background_off_targets_its_agents_latest_state(tmp_path):
    target_id = agents.create_agent(
        persona="Target", voice_id="voice", cwd=str(tmp_path), session="target-bg")
    other_id = agents.create_agent(
        persona="Other", voice_id="voice", cwd=str(tmp_path), session="other-bg")
    assert agent_bg.main(["agent_bg.py", "target-bg", "on", "Watching CI"]) == 0
    turn_lifecycle.transition(other_id, TurnEvent.PROMPT_ADMITTED, {"source": "pwa"})
    assert turn_lifecycle.current_kind(target_id) == AgentState.BACKGROUND
    assert turn_lifecycle.current_kind(other_id) == AgentState.THINKING

    assert agent_bg.main(["agent_bg.py", "target-bg", "off"]) == 0
    assert turn_lifecycle.current_kind(target_id) == AgentState.IDLE
    assert agents.latest_state(target_id)["kind"] == AgentState.IDLE


def test_background_off_after_done_broadcasts_only_state_neutral_status_clear(tmp_path):
    agent_id = agents.create_agent(
        persona="Nadia", voice_id="voice", cwd=str(tmp_path), session="nadia-done")
    assert agent_bg.main(["agent_bg.py", "nadia-done", "on", "Watching CI"]) == 0
    turn_lifecycle.transition(agent_id, TurnEvent.PROMPT_ADMITTED, {"source": "pwa"})
    turn_lifecycle.transition(agent_id, TurnEvent.PROCESS_EXITED_OK, {"source": "test"})
    stream = _Stream()
    watcher = state_watcher.StateLogWatcher(stream)
    watcher._last_id = int(db.conn().execute(
        "SELECT COALESCE(MAX(state_id), 0) FROM state_log").fetchone()[0])

    assert agent_bg.main(["agent_bg.py", "nadia-done", "off"]) == 0
    watcher._poll_once()

    assert [event["type"] for event in stream.events] == [SSEType.AGENT_STATE]
    assert stream.events[0]["kind"] == AgentState.DONE
    assert stream.events[0]["status_text"] == ""
    assert "detail" not in stream.events[0]
    assert agents.latest_state(agent_id)["kind"] == AgentState.DONE
    assert watcher._notify_queue.empty()


def test_background_on_without_initial_state_has_a_clean_error(tmp_path, capsys):
    agent_id = agents.create_agent(
        persona="Nadia", voice_id="voice", cwd=str(tmp_path), session="nadia-empty")
    db.conn().execute("DELETE FROM state_log WHERE agent_id = ?", (agent_id,))

    assert agent_bg.main(["agent_bg.py", "nadia-empty", "on", "Watching CI"]) == 1
    stderr = capsys.readouterr().err
    assert "cannot mark background before its initial state is recorded" in stderr
    assert "Traceback" not in stderr
    assert agents.get_by_session("nadia-empty")["custom_status"] == ""


def test_background_finish_waits_for_writer_and_rechecks_agent_state(tmp_path, monkeypatch):
    agent_id = agents.create_agent(
        persona="Nadia", voice_id="voice", cwd=str(tmp_path), session="nadia-lock")
    assert agent_bg.main(["agent_bg.py", "nadia-lock", "on", "Watching CI"]) == 0

    writer = sqlite3.connect(str(db.DB_PATH), isolation_level=None, timeout=2)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        """INSERT INTO state_log(agent_id, runtime_id, ts, kind, detail)
           VALUES (?, NULL, ?, ?, ?)""",
        (agent_id, db.now_ms(), AgentState.THINKING, '{"source":"pwa"}'),
    )
    waiting_for_writer = threading.Event()
    finished = threading.Event()
    connection_ready = threading.Event()
    worker_connections = []
    result = []
    errors = []

    real_conn = turn_lifecycle.conn

    def observe_worker_connection():
        connection = real_conn()
        if threading.current_thread().name == "background-finish":
            worker_connections.append(connection)
            connection_ready.set()
        return connection

    real_critical_write = turn_lifecycle._critical_write

    def signal_writer_wait():
        if threading.current_thread().name == "background-finish":
            waiting_for_writer.set()
        return real_critical_write()

    monkeypatch.setattr(turn_lifecycle, "conn", observe_worker_connection)
    monkeypatch.setattr(turn_lifecycle, "_critical_write", signal_writer_wait)

    def finish():
        try:
            result.append(turn_lifecycle.transition(
                agent_id, TurnEvent.BACKGROUND_FINISHED))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=finish, name="background-finish")
    try:
        worker.start()
        assert connection_ready.wait(2)
        assert waiting_for_writer.wait(2)
        deadline = time.monotonic() + 0.2
        while not worker_connections[0].in_transaction and time.monotonic() < deadline:
            time.sleep(0.001)
        assert worker_connections[0].in_transaction is False
        assert not finished.wait(0.1)
        writer.commit()
        assert finished.wait(5)
        worker.join(timeout=1)
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()
        if worker.is_alive():
            worker.join(timeout=1)

    assert not errors
    assert result == [None]
    assert turn_lifecycle.current_kind(agent_id) == AgentState.THINKING


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
