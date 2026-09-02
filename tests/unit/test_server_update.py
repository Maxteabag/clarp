import threading

from lib import agents, background_jobs, server_update, service_manager


def _reset_cache():
    server_update._cache = None


def test_update_remote_falls_back_to_release_metadata(tmp_path, monkeypatch):
    monkeypatch.delenv("CLARP_UPDATE_REMOTE", raising=False)
    monkeypatch.setenv("CLARP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CLARP_SHARE_DIR", str(tmp_path / "share"))
    current = tmp_path / "share/current"
    current.mkdir(parents=True)
    (current / "SOURCE_REMOTE").write_text("https://example.test/clarp.git\n")

    assert server_update._update_remote() == "https://example.test/clarp.git"


def test_update_remote_strips_http_credentials(monkeypatch):
    monkeypatch.setenv(
        "CLARP_UPDATE_REMOTE",
        "https://account:secret@example.test:8443/org/clarp.git?token=secret#ref",
    )

    assert server_update._update_remote() == (
        "https://example.test:8443/org/clarp.git")

    monkeypatch.setenv(
        "CLARP_UPDATE_REMOTE",
        "https://example.test/org/clarp.git?access_token=secret#ref",
    )
    assert server_update._update_remote() == "https://example.test/org/clarp.git"


def test_container_update_status_compares_release_tags(monkeypatch):
    _reset_cache()
    monkeypatch.setattr(server_update, "get_server_info", lambda: {
        "server_id": "one", "name": "Docker", "deployment_mode": "container",
        "version": "v1.2.0", "image_version": "v1.2.0",
    })
    monkeypatch.setattr(server_update, "_remote_refs", lambda remote: ("a" * 40, "v1.3.0", "b" * 40))

    status = server_update.get_update_status(force=True)

    assert status["update_available"] is True
    assert status["latest_version"] == "v1.3.0"
    assert status["can_update_in_app"] is False
    assert status["update_method"] == "docker-compose"


def test_container_update_request_never_mutates_immutable_image(monkeypatch):
    monkeypatch.setattr(server_update, "get_update_status", lambda: {
        "update_method": "docker-compose",
        "update_command": "docker compose pull && docker compose up -d",
    })

    code, result = server_update.request_update()

    assert code == 409
    assert result["requires_host"] is True
    assert "docker compose pull" in result["update_command"]


def test_final_release_is_newer_than_its_prerelease():
    assert server_update._compare_versions("v1.3.0-beta.1", "v1.3.0") < 0
    assert server_update._compare_versions("v1.3.0-beta.2", "v1.3.0-beta.11") < 0


def test_remote_refs_excludes_prereleases_from_stable_channel(monkeypatch):
    class Result:
        stdout = "\n".join([
            "a" * 40 + "\trefs/heads/main",
            "b" * 40 + "\trefs/tags/v1.2.0",
            "c" * 40 + "\trefs/tags/v1.3.0-beta.1",
        ])

    monkeypatch.setattr(server_update.subprocess, "run", lambda *args, **kwargs: Result())

    _, latest, sha = server_update._remote_refs("example")

    assert latest == "v1.2.0"
    assert sha == "b" * 40


def test_docker_command_is_withheld_when_host_directory_is_unknown(monkeypatch):
    _reset_cache()
    monkeypatch.setenv("CLARP_COMPOSE_PROJECT", "clarp-work")
    monkeypatch.setenv("CLARP_COMPOSE_DIRECTORY", "")
    monkeypatch.setattr(server_update, "get_server_info", lambda: {
        "server_id": "one", "name": "Docker", "deployment_mode": "container",
        "version": "v1.2.0", "image_version": "v1.2.0",
    })
    monkeypatch.setattr(server_update, "_remote_refs", lambda remote: ("a" * 40, "v1.2.0", "b" * 40))

    status = server_update.get_update_status(force=True)

    assert status["update_command"] == ""


def test_docker_command_uses_explicit_host_project_and_directory(monkeypatch):
    _reset_cache()
    monkeypatch.setenv("CLARP_COMPOSE_PROJECT", "clarp-work")
    monkeypatch.setenv("CLARP_COMPOSE_DIRECTORY", "/srv/clarp")
    monkeypatch.setattr(server_update, "get_server_info", lambda: {
        "server_id": "one", "name": "Docker", "deployment_mode": "container",
        "version": "v1.2.0", "image_version": "v1.2.0",
    })
    monkeypatch.setattr(server_update, "_remote_refs", lambda remote: ("a" * 40, "v1.2.0", "b" * 40))

    status = server_update.get_update_status(force=True)

    assert status["update_command"].startswith("cd /srv/clarp")
    assert "-p clarp-work" in status["update_command"]


def test_docker_command_shell_quotes_host_metadata(monkeypatch):
    _reset_cache()
    monkeypatch.setenv("CLARP_COMPOSE_PROJECT", "the user's work")
    monkeypatch.setenv("CLARP_COMPOSE_DIRECTORY", "/srv/the user's Clarp")
    monkeypatch.setattr(server_update, "get_server_info", lambda: {
        "server_id": "one", "name": "Docker", "deployment_mode": "container",
        "version": "v1.2.0", "image_version": "v1.2.0",
    })
    monkeypatch.setattr(server_update, "_remote_refs", lambda remote: ("a" * 40, "v1.2.0", "b" * 40))

    command = server_update.get_update_status(force=True)["update_command"]

    assert "'\"'\"'" in command


def test_development_version_uses_main_revision(monkeypatch):
    _reset_cache()
    monkeypatch.setattr(server_update, "get_server_info", lambda: {
        "server_id": "one", "name": "Native", "deployment_mode": "native",
        "version": "1234567", "image_version": "",
    })
    monkeypatch.setattr(server_update, "_remote_refs", lambda remote: ("1234567abc", "v1.3.0", "b" * 40))
    monkeypatch.setattr(server_update, "_install_channel", lambda: "development")

    status = server_update.get_update_status(force=True)

    assert status["update_available"] is False
    assert status["latest_version"] == "1234567"
    assert status["can_update_in_app"] is True


def test_native_stable_sha_is_compared_with_latest_tag_commit(monkeypatch):
    _reset_cache()
    monkeypatch.setattr(server_update, "get_server_info", lambda: {
        "server_id": "one", "name": "Native", "deployment_mode": "native",
        "version": "7654321", "image_version": "",
    })
    monkeypatch.setattr(
        server_update, "_remote_refs",
        lambda remote: ("abcdef0" * 5, "v1.3.0", "7654321abc"),
    )
    monkeypatch.setattr(server_update, "_install_channel", lambda: "stable")

    status = server_update.get_update_status(force=True)

    assert status["update_available"] is False
    assert status["latest_version"] == "v1.3.0"


def test_native_update_runs_in_independent_systemd_unit(monkeypatch):
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    monkeypatch.setattr(server_update, "get_update_status", lambda: {
        "update_method": "managed", "update_command": "clarp-admin update",
        "update_available": True,
    })
    calls = []

    class Result:
        returncode = 1
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append(command)
        result = Result()
        if command[0] == "systemd-run":
            result.returncode = 0
        return result

    monkeypatch.setattr(server_update.subprocess, "run", fake_run)
    monkeypatch.setattr(
        server_update, "_worker_script", lambda: server_update.pathlib.Path("/worker.py"))

    code, result = server_update.request_update("mike")

    assert code == 202
    assert result["status"] == "queued"
    assert result["job_handle"] == "bg1:1:managed-server-update"
    assert background_jobs.get(
        server_update.UPDATE_JOB_ID, reconcile=False)["status"] == "queued"
    assert calls[1][:5] == [
        "systemd-run", "--user", "--collect", "--unit=clarp-update",
        "--property=Type=exec",
    ]
    assert calls[1][-5:] == [
        "/worker.py", "--session", "mike", "--handle",
        "bg1:1:managed-server-update",
    ]


def test_native_update_is_noop_when_current(monkeypatch):
    monkeypatch.setattr(server_update, "get_update_status", lambda: {
        "update_method": "managed", "update_command": "clarp-admin update",
        "update_available": False,
    })

    code, result = server_update.request_update()

    assert code == 409
    assert "No newer" in result["message"]


def test_systemd_launch_failure_marks_registered_update_failed(monkeypatch):
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    monkeypatch.setattr(server_update, "get_update_status", lambda: {
        "update_method": "managed", "update_command": "clarp-admin update",
        "update_available": True, "latest_version": "next",
    })

    class Result:
        returncode = 1
        stderr = "launch failed"

    monkeypatch.setattr(
        server_update.subprocess, "run", lambda *_args, **_kwargs: Result())

    code, result = server_update.request_update("mike")

    assert code == 500
    assert result["message"] == "launch failed"
    job = background_jobs.get(server_update.UPDATE_JOB_ID, reconcile=False)
    assert job["status"] == "failed"
    assert job["terminal_reason"] == "update_launch_failed"


def test_macos_update_worker_is_detached_from_server(monkeypatch, tmp_path):
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    monkeypatch.setattr(server_update, "get_update_status", lambda: {
        "update_method": "managed", "update_command": "clarp-admin update",
        "update_available": True, "latest_version": "next",
    })
    monkeypatch.setattr(
        service_manager, "platform_kind", lambda: "macos")
    monkeypatch.setattr(
        server_update, "_worker_script", lambda: tmp_path / "worker.py")
    monkeypatch.setenv("CLAUDE_PWA_LOG_DIR", str(tmp_path / "logs"))
    calls = []

    class Process:
        pid = 42

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr(server_update.subprocess, "Popen", fake_popen)

    code, result = server_update.request_update("mike")

    assert code == 202
    assert result["status"] == "queued"
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][0][1] == str(tmp_path / "worker.py")


def test_concurrent_update_requests_launch_only_one_transient_worker(monkeypatch):
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    monkeypatch.setattr(server_update, "get_update_status", lambda: {
        "update_method": "managed", "update_command": "clarp-admin update",
        "update_available": True, "latest_version": "next",
    })
    active = False
    launches = 0
    state_lock = threading.Lock()

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(command, **_kwargs):
        nonlocal active, launches
        result = Result()
        with state_lock:
            if command[0] == "systemctl":
                result.returncode = 0 if active else 1
            else:
                launches += 1
                active = True
        return result

    monkeypatch.setattr(server_update.subprocess, "run", fake_run)
    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(server_update.request_update("mike")))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert launches == 1
    assert sorted(result[1]["status"] for result in results) == ["queued", "running"]


def test_update_singleton_reuses_persisted_owner_when_default_changes(monkeypatch):
    agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    agents.create_agent(
        persona="Rachel", voice_id="voice", cwd="/tmp", session="rachel")
    previous = background_jobs.upsert(
        session="mike", job_id=server_update.UPDATE_JOB_ID,
        kind="server-update", title="Update Clarp")
    background_jobs.finish(
        previous["job_id"], generation=previous["generation"],
        status="failed", reason="old_failure")
    monkeypatch.setattr(server_update, "get_update_status", lambda: {
        "update_method": "managed", "update_command": "clarp-admin update",
        "update_available": True, "latest_version": "next",
    })

    class Result:
        returncode = 1
        stderr = ""

    def fake_run(command, **_kwargs):
        result = Result()
        if command[0] == "systemd-run":
            result.returncode = 0
        return result

    monkeypatch.setattr(server_update.subprocess, "run", fake_run)

    code, result = server_update.request_update("rachel")

    assert code == 202
    assert result["job"]["session"] == "mike"
    assert background_jobs.get(
        server_update.UPDATE_JOB_ID, reconcile=False)["session"] == "mike"


def test_update_singleton_reassigns_terminal_deleted_owner(monkeypatch):
    mike_id = agents.create_agent(
        persona="Mike", voice_id="voice", cwd="/tmp", session="mike")
    agents.create_agent(
        persona="Rachel", voice_id="voice", cwd="/tmp", session="rachel")
    previous = background_jobs.upsert(
        session="mike", job_id=server_update.UPDATE_JOB_ID,
        kind="server-update", title="Update Clarp")
    background_jobs.finish(
        previous["job_id"], generation=previous["generation"],
        status="failed", reason="old_failure")
    agents.soft_delete(mike_id)
    monkeypatch.setattr(server_update, "get_update_status", lambda: {
        "update_method": "managed", "update_command": "clarp-admin update",
        "update_available": True, "latest_version": "next",
    })

    class Result:
        returncode = 1
        stderr = ""

    def fake_run(command, **_kwargs):
        result = Result()
        if command[0] == "systemd-run":
            result.returncode = 0
        return result

    monkeypatch.setattr(server_update.subprocess, "run", fake_run)

    code, result = server_update.request_update("rachel")

    assert code == 202
    assert result["job"]["session"] == "rachel"
    assert background_jobs.get(
        server_update.UPDATE_JOB_ID, reconcile=False)["session"] == "rachel"
