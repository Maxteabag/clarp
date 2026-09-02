import hashlib
import importlib.util
import pathlib

from lib import agents, background_jobs


_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "agent_bg.py"
_SPEC = importlib.util.spec_from_file_location("agent_bg_script", _SCRIPT)
assert _SPEC and _SPEC.loader
agent_bg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(agent_bg)


def test_message_watch_worker_argv_maps_to_exact_target_job_ids(monkeypatch):
    monkeypatch.setattr(
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
    monkeypatch.setattr(
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
