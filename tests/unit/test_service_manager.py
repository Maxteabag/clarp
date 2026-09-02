from __future__ import annotations

from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import plistlib
import pytest
import subprocess
import threading

from lib import service_manager


def test_test_suite_blocks_live_service_manager_commands():
    with pytest.raises(AssertionError, match="live service-manager command"):
        service_manager.restart()


def test_test_suite_blocks_live_service_commands_in_subprocesses():
    result = subprocess.run(
        ["systemctl", "--user", "restart", "clarp.service"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 97
    assert "blocked live systemctl mutation" in result.stderr


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_linux_definition_and_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("CLARP_CACHE_DIR", str(tmp_path / "cache/clarp"))
    monkeypatch.setenv("CLAUDE_PWA_LOG_DIR", str(tmp_path / "pytest-logs"))
    share = tmp_path / "share"
    template = share / "current/systemd/clarp.service"
    template.parent.mkdir(parents=True)
    template.write_text(
        "ExecStart=@@PYTHON@@ @@SERVER@@\n"
        "Environment=\"PATH=@@SERVICE_PATH@@\"\n"
        "Environment=\"CLARP_SHARE_DIR=@@SHARE_ENV@@\"\n"
        "Environment=\"CLARP_CONFIG_DIR=@@CONFIG_ENV@@\"\n"
        "Environment=\"CLARP_CACHE_DIR=@@CACHE_ENV@@\"\n"
        "Environment=\"CLAUDE_PWA_CONFIG=@@CONFIG_FILE_ENV@@\"\n"
        "Environment=\"CLAUDE_PWA_DB=@@DATABASE_ENV@@\"\n"
        "Environment=\"CLAUDE_PWA_LOG_DIR=@@LOG_ENV@@\"\n")
    path = service_manager.write_definition(
        python=Path("/runtime/python"), share=share,
        service_path="/toolchain/bin:/usr/bin", home=tmp_path)
    assert path == tmp_path / "config/systemd/user/clarp.service"
    assert 'ExecStart="/runtime/python"' in path.read_text()
    assert 'Environment="PATH=/toolchain/bin:/usr/bin"' in path.read_text()
    assert f'CLARP_SHARE_DIR={share}' in path.read_text()
    assert f'CLAUDE_PWA_LOG_DIR={tmp_path / "cache/clarp/logs"}' in path.read_text()
    assert "pytest-logs" not in path.read_text()
    recorder = Recorder()
    service_manager.install_and_restart(runner=recorder)
    assert [call[0][:3] for call in recorder.calls] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable"],
        ["systemctl", "--user", "restart"],
    ]


def test_macos_launch_agent_is_user_scoped_and_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "macos")
    monkeypatch.setenv("CLARP_CONFIG_DIR", str(tmp_path / "Application Support/Clarp"))
    monkeypatch.setenv("CLARP_CACHE_DIR", str(tmp_path / "Caches/Clarp"))
    share = tmp_path / "Application Support/Clarp"
    path = service_manager.write_definition(
        python=tmp_path / "env/bin/python", share=share,
        service_path=f"{tmp_path}/toolchain/bin:/usr/bin", home=tmp_path)
    assert path == tmp_path / (
        "Library/LaunchAgents/com.maxteabag.clarp.server.plist")
    payload = plistlib.loads(path.read_bytes())
    assert payload["ProgramArguments"] == [
        str(tmp_path / "env/bin/python"), str(share / "server.py")]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["EnvironmentVariables"]["CLARP_SHARE_DIR"] == str(share)
    assert path.stat().st_mode & 0o777 == 0o600


def test_macos_lifecycle_uses_launchctl(monkeypatch):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "macos")
    recorder = Recorder()
    service_manager.install_and_restart(runner=recorder)
    commands = [call[0] for call in recorder.calls]
    assert commands[0][0:2] == ["launchctl", "bootout"]
    assert commands[1][0:2] == ["launchctl", "bootstrap"]
    assert commands[2][0:3] == ["launchctl", "kickstart", "-k"]


def test_linux_definition_reload_after_unlink(monkeypatch):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "linux")
    recorder = Recorder()
    service_manager.reload_definitions(runner=recorder)
    assert recorder.calls[0][0] == ["systemctl", "--user", "daemon-reload"]


def test_macos_definition_reload_after_unlink_is_noop(monkeypatch):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "macos")
    recorder = Recorder()
    service_manager.reload_definitions(runner=recorder)
    assert recorder.calls == []


def test_macos_failed_install_reboots_restored_definition(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "macos")
    definition = tmp_path / "restored.plist"
    definition.write_text("restored")
    monkeypatch.setattr(service_manager, "definition_path", lambda *_args: definition)
    recorder = Recorder()

    service_manager.restore_after_failed_install(
        had_previous=True, runner=recorder)

    commands = [call[0] for call in recorder.calls]
    assert commands[0][:2] == ["launchctl", "bootout"]
    assert commands[0][-1].endswith("/com.maxteabag.clarp.server")
    assert commands[1] == [
        "launchctl", "bootstrap", service_manager.launchd_domain(), str(definition)]
    assert commands[2][:3] == ["launchctl", "kickstart", "-k"]


def test_macos_failed_fresh_install_only_unloads_job(monkeypatch):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "macos")
    recorder = Recorder()
    service_manager.restore_after_failed_install(
        had_previous=False, runner=recorder)
    assert len(recorder.calls) == 1
    assert recorder.calls[0][0][:2] == ["launchctl", "bootout"]


def test_wait_until_ready_uses_configured_token(tmp_path):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            release = "old-release" if len(seen) == 1 else "release-123"
            self.wfile.write(f'{{"release_id":"{release}"}}'.encode())

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[server]\nbind_addr = "127.0.0.1"\nport = {server.server_port}\n'
        'auth_token = "secret"\n')
    try:
        service_manager.wait_until_ready(
            config, expected_release_id="release-123", timeout=2.0)
    finally:
        server.shutdown()
        thread.join()
    assert len(seen) >= 2
    assert all(item == ("/status", "Bearer secret") for item in seen)


def test_linux_detached_launcher_uses_transient_unit(monkeypatch):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "linux")
    recorder = Recorder()
    ok, error = service_manager.launch_detached(
        ["/runtime/python", "/worker.py"], unit="clarp-worker",
        delay_seconds=30, runner=recorder)
    assert ok is True and error == ""
    command = recorder.calls[0][0]
    assert command[:5] == [
        "systemd-run", "--user", "--collect", "--unit=clarp-worker",
        "--on-active=30s"]
    assert command[-2:] == ["/runtime/python", "/worker.py"]


def test_macos_detached_launcher_uses_new_session(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "macos")
    monkeypatch.setenv("CLAUDE_PWA_LOG_DIR", str(tmp_path / "logs"))
    calls = []

    class Process:
        pass

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    monkeypatch.setattr(service_manager.subprocess, "Popen", fake_popen)
    ok, error = service_manager.launch_detached(
        ["/runtime/python", "/worker.py"], unit="clarp-worker",
        delay_seconds=30, environment={"CLARP_MEDIA_DIR": "/private/media"})
    assert ok is True and error == ""
    assert calls[0][0][:4] == [
        "/bin/sh", "-c", 'sleep "$1"; shift; exec "$@"', "clarp-delay"]
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["env"]["CLARP_MEDIA_DIR"] == "/private/media"


def test_container_detached_launcher_does_not_require_systemd(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "linux")
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    monkeypatch.setenv("CLAUDE_PWA_LOG_DIR", str(tmp_path / "logs"))
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return object()

    monkeypatch.setattr(service_manager.subprocess, "Popen", fake_popen)
    ok, error = service_manager.launch_detached(
        ["/runtime/python", "/worker.py"], unit="clarp-worker")
    assert ok is True and error == ""
    assert calls[0][0] == ["/runtime/python", "/worker.py"]
    assert calls[0][1]["start_new_session"] is True
