import sqlite3
import os
import pathlib
import signal
import subprocess
import sys
import time

from lib import agents, background_jobs, db


def test_macos_process_identity_and_liveness(monkeypatch):
    monkeypatch.setattr(background_jobs.sys, "platform", "darwin")
    argv = ["python", "worker.py", "--handle", "bg1:1:job"]
    raw_argv = ((len(argv)).to_bytes(4, byteorder=sys.byteorder, signed=True)
                + b"/usr/bin/python\0\0"
                + b"\0".join(item.encode() for item in argv) + b"\0")
    monkeypatch.setattr(background_jobs, "_macos_procargs", lambda _pid: raw_argv)

    def output(command, **_kwargs):
        if command[:3] == ["/usr/sbin/sysctl", "-n", "kern.boottime"]:
            return "{ sec = 123, usec = 0 }\n"
        if "lstart=" in command:
            return "Thu Aug 28 12:00:00 2026\n"
        if "stat=" in command:
            return "S+\n"
        raise AssertionError(command)

    monkeypatch.setattr(background_jobs.subprocess, "check_output", output)
    token = background_jobs.process_start_token(42)
    assert len(token) == 64
    assert background_jobs.process_argv(42) == [
        "python", "worker.py", "--handle", "bg1:1:job"]
    assert background_jobs.worker_is_alive(42, token) is True
    assert output(["/usr/sbin/sysctl", "-n", "kern.boottime"])

    monkeypatch.setattr(
        background_jobs.subprocess, "check_output",
        lambda command, **kwargs: (
            "Z\n" if "stat=" in command else output(command, **kwargs)))
    assert background_jobs.worker_is_alive(42, token) is False


def test_macos_procargs_preserves_spaces():
    argv = [
        "python", "/Library/Application Support/Clarp/watch_messages.py",
        "whatsapp", "--watch-name", "Jane Smith",
    ]
    raw = ((len(argv)).to_bytes(4, byteorder=sys.byteorder, signed=True)
           + b"/usr/bin/python\0\0"
           + b"\0".join(item.encode() for item in argv) + b"\0ENV=value\0")
    assert background_jobs._parse_macos_procargs(raw) == argv


def test_worker_liveness_rejects_unreaped_zombie():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    token = background_jobs.process_start_token(process.pid)
    try:
        for _ in range(100):
            raw = pathlib.Path(f"/proc/{process.pid}/stat").read_text()
            if raw.rsplit(")", 1)[1].split()[0] == "Z":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("child did not enter zombie state")

        assert background_jobs.worker_is_alive(process.pid, token) is False
    finally:
        process.wait()


def test_computer_owned_job_needs_no_visible_agent():
    job = background_jobs.upsert_computer(
        computer_id="computer-zero", job_id="model-install", kind="model",
        title="Install model", status="queued")

    assert agents.list_agents() == []
    assert job["owner_kind"] == "computer"
    assert job["computer_id"] == "computer-zero"
    assert job["agent_id"] == ""
    assert job["session"] == ""
    assert job["agent_name"] == "Computer"


def test_job_registration_revalidates_session_owner_inside_transaction(monkeypatch):
    agent_id = agents.create_agent(
        persona="Nadia", voice_id="voice", cwd="/tmp", session="nadia")
    stale = agents.get_by_session("nadia")
    agents.soft_delete(agent_id)
    monkeypatch.setattr(agents, "get_by_session", lambda _session: stale)

    with __import__("pytest").raises(ValueError, match="session changed"):
        background_jobs.upsert(
            session="nadia", job_id="stale-owner", kind="other", title="Stale")

    assert background_jobs.get("stale-owner", reconcile=False) is None


def test_only_one_request_claims_queued_worker_launch():
    job = background_jobs.upsert_computer(
        computer_id="computer-zero", job_id="portrait-job", kind="portrait",
        title="Generate", status="queued")

    assert background_jobs.claim_queued_launch(
        job["job_id"], generation=job["generation"]) is True
    repeated = background_jobs.upsert_computer(
        computer_id="computer-zero", job_id="portrait-job", kind="portrait",
        title="Generate", status="queued")
    assert repeated["generation"] == job["generation"]
    assert repeated["heartbeat_source"] == "launch_claimed"
    assert background_jobs.claim_queued_launch(
        job["job_id"], generation=job["generation"]) is False


def test_terminate_worker_escalates_suspended_exact_identity():
    process = subprocess.Popen([
        sys.executable, "-c",
        "import os,signal,time; os.kill(os.getpid(), signal.SIGSTOP); time.sleep(30)",
    ])
    token = background_jobs.process_start_token(process.pid)
    try:
        for _ in range(100):
            state = pathlib.Path(f"/proc/{process.pid}/stat").read_text(
            ).rsplit(")", 1)[1].split()[0]
            if state == "T":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("worker did not suspend")

        assert background_jobs.terminate_worker(
            process.pid, token, term_timeout=0.05, kill_timeout=1.0)
        assert process.wait(timeout=2) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGKILL)
            process.wait()


def test_terminate_worker_never_signals_token_mismatch(monkeypatch):
    signals = []
    monkeypatch.setattr(
        background_jobs.os, "kill",
        lambda pid, sig: signals.append((pid, sig)))

    assert background_jobs.terminate_worker(os.getpid(), "wrong-token") is True
    assert signals == []


def _agent(session: str = "nadia-test") -> str:
    return agents.create_agent(
        persona="Nadia", voice_id="voice", cwd="/tmp", session=session,
    )


def test_register_snapshot_and_idempotent_cancel_gate():
    agent_id = _agent()
    created = background_jobs.upsert(
        session="nadia-test", job_id="wa-yoga", kind="whatsapp",
        title="WhatsApp: Hot Yoga", detail="Waiting for a reply",
    )

    assert created["agent_id"] == agent_id
    assert created["status"] == "running"
    assert created["outcome_state"] == "pending"
    assert created["worker_freshness"] == "fresh"
    assert created["heartbeat_timeout_ms"] >= 2 * 180_000
    snapshot = background_jobs.snapshot()
    assert snapshot["snapshot_revision"] == created["revision"]
    assert snapshot["observed_at"] >= created["heartbeat_at"]
    assert snapshot["jobs"][0]["agent_name"] == "Nadia"

    cancelled, changed = background_jobs.cancel_with_result("wa-yoga")
    assert changed is True
    assert cancelled["status"] == "cancelled"
    repeated, changed = background_jobs.cancel_with_result("wa-yoga")
    assert changed is False
    assert repeated["revision"] == cancelled["revision"]
    assert not background_jobs.is_active("wa-yoga")

    background_jobs.finish("wa-yoga", generation=cancelled["generation"])
    assert background_jobs.get("wa-yoga")["status"] == "cancelled"


def test_snapshot_rows_and_revision_share_one_sqlite_read_snapshot(monkeypatch):
    _agent()
    job = background_jobs.upsert(
        session="nadia-test", job_id="atomic", kind="ci", title="Atomic")
    real = db.conn()
    triggered = False

    class ConnectionProxy:
        def execute(self, sql, params=()):
            nonlocal triggered
            if "MAX(event_id)" in sql and not triggered:
                triggered = True
                writer = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
                try:
                    event = writer.execute(
                        "INSERT INTO background_job_events(job_id,observed_at) VALUES (?,?)",
                        ("atomic", job["updated_at"] + 1),
                    )
                    writer.execute(
                        "UPDATE background_jobs SET status='cancelled',revision=? WHERE job_id='atomic'",
                        (int(event.lastrowid),),
                    )
                finally:
                    writer.close()
            return real.execute(sql, params)

    monkeypatch.setattr(background_jobs.db, "conn", lambda: ConnectionProxy())

    snapshot = background_jobs.snapshot()

    assert snapshot["jobs"][0]["status"] == "running"
    assert snapshot["snapshot_revision"] == job["revision"]
    current = real.execute(
        "SELECT status,revision FROM background_jobs WHERE job_id='atomic'"
    ).fetchone()
    assert current["status"] == "cancelled"
    assert current["revision"] > snapshot["snapshot_revision"]


def test_snapshot_never_truncates_active_jobs():
    _agent()
    for index in range(105):
        background_jobs.upsert(
            session="nadia-test", job_id=f"active-{index}", kind="other",
            title=f"Active {index}")

    snapshot = background_jobs.snapshot(include_terminal=False)

    assert len(snapshot["jobs"]) == 105
    assert all(job["status"] == "running" for job in snapshot["jobs"])


def test_explicit_queued_running_and_terminal_semantics():
    _agent()
    queued = background_jobs.upsert(
        session="nadia-test", job_id="queued", kind="ci",
        title="Queued build", status="queued",
    )
    assert queued["status"] == "queued"
    running = background_jobs.start("queued")
    assert running["status"] == "running"
    succeeded = background_jobs.finish("queued", generation=running["generation"])
    assert succeeded["status"] == "succeeded"
    assert succeeded["outcome_state"] == "succeeded"
    assert succeeded["terminal_at"] is not None

    background_jobs.upsert(
        session="nadia-test", job_id="failed", kind="other",
        title="Failing worker",
    )
    current = background_jobs.get("failed", reconcile=False)
    failed = background_jobs.finish(
        "failed", generation=current["generation"], status="failed",
        reason="provider exited")
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "provider exited"
    assert failed["outcome_state"] == "failed"


def test_queued_job_does_not_expire_on_running_worker_heartbeat_timeout():
    _agent()
    queued = background_jobs.upsert(
        session="nadia-test", job_id="waiting", kind="ci",
        title="Waiting for capacity", status="queued",
        heartbeat_timeout_ms=30_000)

    changed = background_jobs.reconcile_stale(
        job_id="waiting",
        now_ms=queued["heartbeat_at"] + queued["heartbeat_timeout_ms"] + 1,
        process_probe=lambda _pid, _token: False,
    )

    assert changed == []
    assert background_jobs.get("waiting", reconcile=False)["status"] == "queued"


def test_stale_worker_without_live_identity_becomes_failed_terminal():
    _agent()
    job = background_jobs.upsert(
        session="nadia-test", job_id="vanished", kind="email",
        title="Email watcher", heartbeat_timeout_ms=30_000,
    )
    stale_now = job["heartbeat_at"] + job["heartbeat_timeout_ms"] + 1

    changed = background_jobs.reconcile_stale(
        job_id="vanished", now_ms=stale_now,
        process_probe=lambda _pid, _token: False,
    )

    assert changed == ["vanished"]
    reconciled = background_jobs.get("vanished", reconcile=False)
    assert reconciled["status"] == "failed"
    assert reconciled["terminal_reason"] == "heartbeat_expired"
    assert reconciled["worker_freshness"] == "stale"
    assert reconciled["outcome_state"] == "unknown"


def test_live_shared_process_does_not_replace_per_job_heartbeat():
    _agent()
    job = background_jobs.upsert(
        session="nadia-test", job_id="message-watch", kind="whatsapp",
        title="WhatsApp watcher", heartbeat_timeout_ms=30_000,
        worker_pid=4242, worker_start_token="boot:start",
    )
    first_stale = job["heartbeat_at"] + job["heartbeat_timeout_ms"] + 1
    changed = background_jobs.reconcile_stale(
        job_id="message-watch", now_ms=first_stale,
        process_probe=lambda pid, token: pid == 4242 and token == "boot:start",
    )
    failed = background_jobs.get("message-watch", reconcile=False)
    assert changed == ["message-watch"]
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "heartbeat_expired"
    assert failed["outcome_state"] == "unknown"


def test_dead_adopted_worker_is_reconciled_as_vanished():
    _agent()
    job = background_jobs.upsert(
        session="nadia-test", job_id="dead-watch", kind="whatsapp",
        title="WhatsApp watcher", heartbeat_timeout_ms=30_000,
        worker_pid=4242, worker_start_token="boot:start",
    )

    background_jobs.reconcile_stale(
        job_id="dead-watch",
        now_ms=job["heartbeat_at"] + job["heartbeat_timeout_ms"] + 1,
        process_probe=lambda _pid, _token: False,
    )

    vanished = background_jobs.get("dead-watch", reconcile=False)
    assert vanished["status"] == "failed"
    assert vanished["terminal_reason"] == "worker_vanished"
    assert vanished["outcome_state"] == "unknown"


def test_stale_reconciler_cannot_overwrite_concurrent_heartbeat():
    _agent()
    job = background_jobs.upsert(
        session="nadia-test", job_id="racing", kind="email", title="Racing",
        heartbeat_timeout_ms=30_000, worker_pid=4242,
        worker_start_token="boot:start")

    def heartbeat_during_probe(_pid, _token):
        background_jobs.heartbeat(
            "racing", worker_pid=4242, worker_start_token="boot:start")
        return True

    changed = background_jobs.reconcile_stale(
        job_id="racing",
        now_ms=job["heartbeat_at"] + job["heartbeat_timeout_ms"] + 1,
        process_probe=heartbeat_during_probe,
    )

    assert changed == []
    current = background_jobs.get("racing", reconcile=False)
    assert current["status"] == "running"
    assert current["revision"] == job["revision"]
    assert current["heartbeat_at"] > job["heartbeat_at"]


def test_worker_status_heartbeat_does_not_keep_another_worker_alive():
    _agent()
    first = background_jobs.upsert(
        session="nadia-test", job_id="first", kind="whatsapp", title="First",
        worker_pid=100, worker_start_token="boot:first")
    second = background_jobs.upsert(
        session="nadia-test", job_id="second", kind="email", title="Second",
        worker_pid=200, worker_start_token="boot:second")

    updated = background_jobs.heartbeat_jobs(
        ["first"], worker_pid=100,
        worker_start_token="boot:first")

    assert updated == ["first"]
    refreshed = background_jobs.get("first", reconcile=False)
    assert refreshed["revision"] == first["revision"]
    assert refreshed["heartbeat_at"] > first["heartbeat_at"]
    assert background_jobs.get("second", reconcile=False)["revision"] == second["revision"]


def test_liveness_heartbeats_do_not_persist_change_feed_events():
    _agent()
    job = background_jobs.upsert(
        session="nadia-test", job_id="quiet-feed", kind="email", title="Quiet")
    initial_event = background_jobs.latest_event_id()

    for _ in range(25):
        refreshed = background_jobs.heartbeat("quiet-feed")
        assert refreshed["status"] == "running"

    assert background_jobs.latest_event_id() == initial_event
    finished = background_jobs.finish(
        "quiet-feed", generation=job["generation"])
    assert finished["status"] == "succeeded"
    assert background_jobs.latest_event_id() == initial_event + 1


def test_cancelled_job_is_not_reactivated_by_worker_restart():
    _agent()
    background_jobs.upsert(
        session="nadia-test", job_id="stable", kind="ci", title="Build")
    background_jobs.cancel("stable")

    restarted = background_jobs.upsert(
        session="nadia-test", job_id="stable", kind="ci", title="Build again")

    assert restarted["status"] == "cancelled"
    assert background_jobs.start("stable")["status"] == "cancelled"

    explicit = background_jobs.restart(
        session="nadia-test", job_id="stable", kind="ci", title="Build again")
    assert explicit["status"] == "running"
    assert explicit["generation"] == restarted["generation"] + 1


def test_succeeded_job_can_start_a_new_observed_run():
    _agent()
    first = background_jobs.upsert(
        session="nadia-test", job_id="repeatable", kind="ci", title="Build")
    background_jobs.finish("repeatable", generation=first["generation"])

    restarted = background_jobs.upsert(
        session="nadia-test", job_id="repeatable", kind="ci", title="Build")

    assert restarted["status"] == "running"
    assert restarted["terminal_at"] is None
    assert restarted["terminal_reason"] == ""
    assert restarted["started_at"] >= first["started_at"]
    assert restarted["generation"] == first["generation"] + 1


def test_superseded_worker_cannot_finish_new_generation():
    _agent()
    old = background_jobs.upsert(
        session="nadia-test", job_id="watch", kind="email", title="Watch",
        worker_pid=100, worker_start_token="boot:old")
    new = background_jobs.upsert(
        session="nadia-test", job_id="watch", kind="email", title="Watch",
        worker_pid=200, worker_start_token="boot:new")
    assert new["generation"] == old["generation"] + 1
    assert not background_jobs.is_active("watch", generation=old["generation"])
    assert background_jobs.is_active("watch", generation=new["generation"])

    assert background_jobs.finish(
        "watch", generation=old["generation"], worker_pid=100,
        worker_start_token="boot:old") is None
    assert background_jobs.get("watch", reconcile=False)["status"] == "running"
    finished = background_jobs.finish(
        "watch", generation=new["generation"], worker_pid=200,
        worker_start_token="boot:new")
    assert finished["status"] == "succeeded"


def test_stable_job_id_cannot_be_taken_over_by_another_agent():
    _agent("nadia-test")
    _agent("arnold-test")
    background_jobs.upsert(
        session="nadia-test", job_id="shared-name", kind="ci",
        title="Nadia job",
    )

    try:
        background_jobs.upsert(
            session="arnold-test", job_id="shared-name", kind="ci",
            title="Arnold job",
        )
        raise AssertionError("cross-agent collision should fail")
    except ValueError as exc:
        assert "another agent" in str(exc)

    job = background_jobs.get("shared-name")
    assert job["session"] == "nadia-test"
