from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("clarp_admin", ROOT / "bin/clarp-admin.py")
assert SPEC and SPEC.loader
admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admin)


def test_stable_release_tags_exclude_prereleases():
    assert admin.stable_release_tags([
        "v2.0.0-beta.1", "v1.9.0", "v1.8.0+build.2",
    ]) == ["v1.9.0", "v1.8.0+build.2"]


def test_set_toml_value_preserves_other_sections(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[server]\nport = 7000\n\n[audio]\ndelivery = "raw-pcm"\n')
    admin.set_toml_value(config, "server", "port", 7682)
    admin.set_toml_value(config, "whisper", "model", "small.en")
    text = config.read_text()
    assert "port = 7682" in text
    assert '[audio]\ndelivery = "raw-pcm"' in text
    assert '[whisper]\nmodel = "small.en"' in text


def test_managed_skills_link_to_both_tools_without_overwriting_user_skill(
    tmp_path, monkeypatch
):
    share = tmp_path / "share"
    source = share / "current/skills/clarp-test"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: clarp-test\ndescription: test\n---\n")
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setattr(admin, "CLAUDE_SKILLS", claude)
    monkeypatch.setattr(admin, "CODEX_SKILLS", codex)

    admin.link_skill("clarp-test")
    assert (claude / "clarp-test/SKILL.md").is_file()
    assert (codex / "clarp-test/SKILL.md").is_file()

    user_skill = claude / "clarp-user-owned"
    user_skill.mkdir()
    (user_skill / "SKILL.md").write_text("personal")
    other_source = share / "current/skills/clarp-user-owned"
    other_source.mkdir()
    (other_source / "SKILL.md").write_text("managed")
    admin.link_skill("clarp-user-owned")
    assert (user_skill / "SKILL.md").read_text() == "personal"


def test_managed_link_uses_path_components_not_text_prefix(tmp_path, monkeypatch):
    share = tmp_path / "clarp"
    share.mkdir()
    sibling = tmp_path / "clarp-backup/skill"
    sibling.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(sibling, target_is_directory=True)
    monkeypatch.setattr(admin, "SHARE", share)
    assert admin.managed_link(link) is False


def test_skill_manifest_contains_only_agreed_packs():
    manifest = json.loads((ROOT / "skills/manifest.json").read_text())
    packs = {item["pack"] for item in manifest["skills"]}
    assert packs == {"core", "native", "messaging", "artifacts"}
    assert all(item["id"].startswith("clarp-") for item in manifest["skills"])


def test_cli_parses_noninteractive_setup():
    args = admin.parser().parse_args([
        "setup", "--non-interactive", "--backend", "codex",
        "--transcription", "apple-only", "--toolchain", "existing",
    ])
    assert args.backend == "codex"
    assert args.transcription == "apple-only"
    assert args.toolchain == "existing"
    assert args.bind is None
    assert args.port is None
    assert args.func is admin.cmd_setup


def test_cli_parses_custom_voice_adapter_management():
    install = admin.parser().parse_args([
        "tts", "adapters", "install", "/tmp/example", "--replace",
    ])
    assert install.tts_command == "adapters"
    assert install.adapter_command == "install"
    assert install.path == "/tmp/example"
    assert install.replace is True
    assert install.func is admin.cmd_tts

    test = admin.parser().parse_args([
        "tts", "adapters", "test", "custom.example",
    ])
    assert test.adapter_command == "test"
    assert test.provider == "custom.example"


def test_cli_parses_custom_transcription_adapter_management():
    install = admin.parser().parse_args([
        "transcription", "adapters", "install", "/tmp/private-stt",
        "--replace",
    ])
    assert install.transcription_command == "adapters"
    assert install.adapter_command == "install"
    assert install.path == "/tmp/private-stt"
    assert install.replace is True
    assert install.func is admin.cmd_transcription

    test = admin.parser().parse_args([
        "transcription", "adapters", "test", "custom.private",
    ])
    assert test.adapter_command == "test"
    assert test.provider == "custom.private"


def test_paths_reports_platform_runtime_locations(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLARP_SHARE_DIR", str(tmp_path / "share"))
    monkeypatch.setenv("CLARP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CLARP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CLAUDE_PWA_DB", str(tmp_path / "share/state.sqlite"))
    monkeypatch.setenv("CLAUDE_PWA_CONFIG", str(tmp_path / "config/config.toml"))
    monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", "linux")

    assert admin.cmd_paths(None) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["share"] == str(tmp_path / "share")
    assert value["config"] == str(tmp_path / "config/config.toml")
    assert value["toolchain"] == str(tmp_path / "share/toolchain")


def test_sessions_returns_platform_independent_agent_inventory(capsys):
    from lib import agents

    agents.create_agent(
        persona="Rachel", voice_id="voice", cwd="/workspace",
        session="rachel", backend="codex")

    assert admin.cmd_sessions(None) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{
        "session": "rachel", "agent_id": rows[0]["agent_id"],
        "persona": "Rachel", "backend": "codex", "cwd": "/workspace",
    }]


def test_noninteractive_setup_requires_explicit_toolchain_and_transcription():
    import pytest

    missing_toolchain = admin.parser().parse_args([
        "setup", "--non-interactive", "--backend", "codex",
        "--transcription", "apple-only",
    ])
    with pytest.raises(SystemExit, match="--toolchain is required"):
        admin.cmd_setup(missing_toolchain)

    missing_transcription = admin.parser().parse_args([
        "setup", "--non-interactive", "--backend", "codex",
        "--toolchain", "none",
    ])
    with pytest.raises(SystemExit, match="--transcription is required"):
        admin.cmd_setup(missing_transcription)


def test_managed_toolchain_is_passed_to_installer_without_global_mutation(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        admin, "_execute_setup",
        lambda *args: captured.update(args=args) or 0)
    args = admin.parser().parse_args([
        "setup", "--non-interactive", "--backend", "both",
        "--transcription", "apple-only", "--toolchain", "managed",
    ])

    assert admin.cmd_setup(args) == 0
    assert captured["args"][6] == "managed"


def test_existing_toolchain_ignores_clarp_managed_binaries(tmp_path, monkeypatch):
    share = tmp_path / "share"
    managed_bin = share / "toolchains/version/bin"
    external_bin = tmp_path / "external/bin"
    managed_bin.mkdir(parents=True)
    external_bin.mkdir(parents=True)
    for root in (managed_bin, external_bin):
        command = root / "codex"
        command.write_text("#!/bin/sh\n")
        command.chmod(0o700)
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setenv("PATH", f"{managed_bin}:{external_bin}")

    assert admin.external_command("codex") == str(external_bin / "codex")
    (external_bin / "codex").unlink()
    assert admin.external_command("codex") is None


def test_network_tailscale_configures_loopback_https_and_auth(
    tmp_path, monkeypatch, capsys,
):
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\nbind_addr = "0.0.0.0"\nport = 7682\nauth_token = ""\n')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin, "SHARE", tmp_path / "share")
    monkeypatch.setattr(admin.shutil, "which", lambda name: "/usr/bin/tailscale")

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        result = Result()
        if "status" in command:
            result.stdout = json.dumps({
                "Self": {"Online": True,
                         "DNSName": "friend-machine.tailnet.ts.net."},
            })
        return result

    monkeypatch.setattr(admin.subprocess, "run", run)
    monkeypatch.setattr(admin.service_manager, "restart", lambda: None)
    args = argparse.Namespace(
        network_command="use", mode="tailscale", url="")
    assert admin.cmd_network(args) == 0

    value = __import__("tomllib").loads(config.read_text())
    assert value["server"]["bind_addr"] == "127.0.0.1"
    assert value["server"]["public_base_url"] == (
        "https://friend-machine.tailnet.ts.net")
    assert value["server"]["auth_token"]
    assert value["network"] == {"advertise_lan": False, "mode": "tailscale"}
    assert any(command[1:3] == ["serve", "--bg"]
               and "--set-path=/" in command
               for command in calls)
    assert json.loads(capsys.readouterr().out)["mode"] == "tailscale"


def test_manual_network_requires_https(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[server]\nport = 7682\n')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    with pytest.raises(SystemExit, match="https"):
        admin.cmd_network(argparse.Namespace(
            network_command="use", mode="manual",
            url="http://friend.example"))


def test_invalid_replacement_does_not_remove_live_tailscale_route(
    tmp_path, monkeypatch,
):
    config = tmp_path / "config.toml"
    original = (
        '[server]\nport = 7682\n\n'
        '[network]\nmode = "tailscale"\nadvertise_lan = false\n')
    config.write_text(original)
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    cleanup = []
    monkeypatch.setattr(
        admin, "_managed_tailscale_serve_routes",
        lambda _ports: cleanup.append("inspected") or {"443": 7682})

    with pytest.raises(SystemExit, match="https"):
        admin.cmd_network(argparse.Namespace(
            network_command="use", mode="manual",
            url="http://friend.example"))

    assert cleanup == []
    assert config.read_text() == original


def test_missing_tailscale_is_only_an_error_when_managed_state_exists(
    tmp_path, monkeypatch,
):
    config = tmp_path / "config.toml"
    config.write_text('[network]\nmode = "off"\n')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin.shutil, "which", lambda _name: None)
    assert admin._tailscale_serve_status() == {}

    config.with_name("tailscale-serve.json").write_text(
        '{"listener_port":7682,"https_ports":["443"]}')
    with pytest.raises(SystemExit, match="previously managed Serve route"):
        admin._tailscale_serve_status()


def test_stopped_tailscale_daemon_only_blocks_recorded_cleanup(
    tmp_path, monkeypatch,
):
    config = tmp_path / "config.toml"
    config.write_text('[network]\nmode = "off"\n')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin.shutil, "which", lambda _name: "/usr/bin/tailscale")

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "tailscaled is not running"

    monkeypatch.setattr(admin.subprocess, "run", lambda *_args, **_kwargs: Failed())
    assert admin._tailscale_serve_status() == {}

    config.with_name("tailscale-serve.json").write_text(
        '{"listener_port":7682,"https_ports":["443"]}')
    with pytest.raises(SystemExit, match="could not inspect"):
        admin._tailscale_serve_status()


def test_failed_replacement_restores_tailscale_config_before_cleanup(
    tmp_path, monkeypatch,
):
    config = tmp_path / "config.toml"
    original = (
        '[server]\nbind_addr = "127.0.0.1"\nport = 7682\n'
        'auth_token = "configured"\n\n'
        '[network]\nmode = "tailscale"\nadvertise_lan = false\n')
    config.write_text(original)
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(
        admin, "_managed_tailscale_serve_routes",
        lambda _ports: {"443": 7682})
    removed = []
    monkeypatch.setattr(
        admin, "_remove_managed_tailscale_serve",
        lambda *_args, **_kwargs: removed.append(True))
    restarts = []

    def restart(*, check=True):
        restarts.append(check)
        if len(restarts) == 1:
            raise RuntimeError("restart failed")

    monkeypatch.setattr(admin.service_manager, "restart", restart)
    monkeypatch.setattr(
        admin, "_set_managed_tailscale_serve", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="restart failed"):
        admin.cmd_network(argparse.Namespace(
            network_command="use", mode="off", url=""))

    assert removed == []
    assert restarts == [True, True]
    assert config.read_text() == original


def test_failed_tailscale_activation_removes_new_route(
    tmp_path, monkeypatch,
):
    config = tmp_path / "config.toml"
    original = (
        '[server]\nbind_addr = "127.0.0.1"\nport = 7682\n'
        'auth_token = "configured"\n\n'
        '[network]\nmode = "off"\nadvertise_lan = false\n')
    config.write_text(original)
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(
        admin, "_managed_tailscale_serve_routes", lambda _ports: {})
    monkeypatch.setattr(admin, "_tailscale_info", lambda: {
        "installed": True, "online": True,
        "dns_name": "friend.tailnet.example",
    })
    monkeypatch.setattr(
        admin, "_assert_tailscale_root_available",
        lambda _port, **_kwargs: None)
    routes = []
    monkeypatch.setattr(
        admin, "_set_managed_tailscale_serve",
        lambda _port, **kwargs: routes.append(kwargs))
    restarts = []

    def restart(*, check=True):
        restarts.append(check)
        if len(restarts) == 1:
            raise RuntimeError("restart failed")

    monkeypatch.setattr(admin.service_manager, "restart", restart)

    with pytest.raises(RuntimeError, match="restart failed"):
        admin.cmd_network(argparse.Namespace(
            network_command="use", mode="tailscale", url=""))

    assert routes == [
        {"enabled": True},
        {"https_port": "443", "enabled": False},
    ]
    assert restarts == [True, True]
    assert config.read_text() == original


def test_failed_tailscale_rollback_is_retried_and_reported(
    tmp_path, monkeypatch,
):
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\nport = 7682\nauth_token = "configured"\n\n'
        '[network]\nmode = "off"\n')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(
        admin, "_managed_tailscale_serve_routes", lambda _ports: {})
    monkeypatch.setattr(admin, "_tailscale_info", lambda: {
        "installed": True, "online": True,
        "dns_name": "friend.tailnet.example",
    })
    monkeypatch.setattr(
        admin, "_assert_tailscale_root_available",
        lambda _port, **_kwargs: None)
    route_calls = []

    def route(_port, **kwargs):
        route_calls.append(kwargs)
        if not kwargs["enabled"]:
            raise PermissionError("tailscale unavailable")

    monkeypatch.setattr(admin, "_set_managed_tailscale_serve", route)
    restarts = []

    def restart(*, check=True):
        restarts.append(check)
        if len(restarts) == 1:
            raise RuntimeError("restart failed")

    monkeypatch.setattr(admin.service_manager, "restart", restart)
    monkeypatch.setattr(admin.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="rollback also failed"):
        admin.cmd_network(argparse.Namespace(
            network_command="use", mode="tailscale", url=""))

    assert route_calls == [
        {"enabled": True},
        {"https_port": "443", "enabled": False},
        {"https_port": "443", "enabled": False},
        {"https_port": "443", "enabled": False},
    ]
    assert restarts == [True, True]


def test_reapplying_tailscale_keeps_existing_route(
    tmp_path, monkeypatch,
):
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\nbind_addr = "127.0.0.1"\nport = 7682\n'
        'auth_token = "configured"\n\n'
        '[network]\nmode = "tailscale"\nadvertise_lan = false\n')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(
        admin, "_managed_tailscale_serve_routes",
        lambda _ports: {"443": 7682})
    monkeypatch.setattr(admin, "_tailscale_info", lambda: {
        "installed": True, "online": True,
        "dns_name": "friend.tailnet.example",
    })
    monkeypatch.setattr(
        admin, "_assert_tailscale_root_available",
        lambda _port, **_kwargs: None)
    configured = []
    removed = []
    monkeypatch.setattr(
        admin, "_set_managed_tailscale_serve",
        lambda _port, **kwargs: configured.append(kwargs))
    monkeypatch.setattr(
        admin, "_remove_managed_tailscale_serve",
        lambda *_args, **kwargs: removed.append(kwargs))
    monkeypatch.setattr(admin.service_manager, "restart", lambda: None)

    assert admin.cmd_network(argparse.Namespace(
        network_command="use", mode="tailscale", url="")) == 0

    assert configured == [{"enabled": True}]
    assert removed == []


def test_tailscale_activation_rejects_unrelated_root_handler(
    tmp_path, monkeypatch,
):
    config = tmp_path / "config.toml"
    original = (
        '[server]\nport = 7682\nauth_token = "configured"\n\n'
        '[network]\nmode = "off"\n')
    config.write_text(original)
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(
        admin, "_managed_tailscale_serve_routes", lambda _ports: {})
    monkeypatch.setattr(admin, "_tailscale_info", lambda: {
        "installed": True, "online": True,
        "dns_name": "friend.tailnet.example",
    })
    monkeypatch.setattr(admin, "_tailscale_serve_status", lambda: {
        "Web": {"friend.tailnet.example:443": {"Handlers": {
            "/": {"Proxy": "http://127.0.0.1:9000"},
        }}},
    })
    configured = []
    monkeypatch.setattr(
        admin, "_set_managed_tailscale_serve",
        lambda *_args, **kwargs: configured.append(kwargs))
    monkeypatch.setattr(admin.service_manager, "restart", lambda **_kwargs: None)

    with pytest.raises(SystemExit, match="another application"):
        admin.cmd_network(argparse.Namespace(
            network_command="use", mode="tailscale", url=""))

    assert configured == []
    assert config.read_text() == original


def test_tailscale_port_change_accepts_previous_clarp_root(
    monkeypatch,
):
    monkeypatch.setattr(admin, "_tailscale_serve_status", lambda: {
        "Web": {"friend.tailnet.example:443": {"Handlers": {
            "/": {"Proxy": "http://127.0.0.1:7000"},
        }}},
    })
    admin._assert_tailscale_root_available(
        7682, allowed_proxy_ports={7000, 7682})


def test_uninstall_requires_explicit_force_when_tailscale_cleanup_is_unavailable(
    tmp_path, monkeypatch, capsys,
):
    config = tmp_path / "config.toml"
    config.write_text('[server]\nport = 7682\n')
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setattr(admin, "HOME", tmp_path / "home")
    monkeypatch.setattr(admin, "selected_skills", lambda: [])
    monkeypatch.setattr(
        admin, "_managed_tailscale_serve_routes",
        lambda _ports: (_ for _ in ()).throw(
            PermissionError("daemon unavailable")))
    monkeypatch.setattr(admin.service_manager, "stop_and_disable", lambda: None)
    monkeypatch.setattr(admin.service_manager, "reload_definitions", lambda: None)
    monkeypatch.setattr(
        admin.service_manager, "definition_path", lambda *_args: tmp_path / "unit")

    with pytest.raises(SystemExit, match="--force-network-cleanup"):
        admin.cmd_uninstall(argparse.Namespace(
            purge_data=False, force_network_cleanup=False))
    assert share.is_dir()

    assert admin.cmd_uninstall(argparse.Namespace(
        purge_data=False, force_network_cleanup=True)) == 0
    assert "continuing forced uninstall" in capsys.readouterr().err


def test_leaving_tailscale_removes_only_clarp_root_handler(
    tmp_path, monkeypatch,
):
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\nbind_addr = "127.0.0.1"\nport = 7682\n'
        'auth_token = "configured"\n\n'
        '[network]\nmode = "tailscale"\nadvertise_lan = false\n')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin.shutil, "which", lambda _name: "/usr/bin/tailscale")

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        result = Result()
        if command[1:4] == ["serve", "status", "--json"]:
            result.stdout = json.dumps({
                "Web": {
                    "friend.tailnet.ts.net:443": {"Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:7000"},
                        "/term/": {"Proxy": "http://127.0.0.1:7681/term/"},
                    }},
                    "friend.tailnet.ts.net:8443": {"Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:8222"},
                    }},
                },
            })
        return result

    monkeypatch.setattr(admin.subprocess, "run", run)
    monkeypatch.setattr(admin.service_manager, "restart", lambda: None)

    assert admin.cmd_network(argparse.Namespace(
        network_command="use", mode="off", url="",
        previous_port=7000)) == 0

    assert [
        "/usr/bin/tailscale", "serve", "--https=443", "--set-path=/", "off",
    ] in calls
    assert not any("8443" in part for call in calls for part in call
                   if call[-1:] == ["off"])


def test_local_only_removes_orphaned_route_despite_stale_mode(
    tmp_path, monkeypatch,
):
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\nport = 7682\nauth_token = "configured"\n\n'
        '[network]\nmode = "off"\n')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(
        admin, "_managed_tailscale_serve_routes",
        lambda _ports: {"443": 7682})
    removed = []
    monkeypatch.setattr(
        admin, "_remove_managed_tailscale_serve",
        lambda port, **kwargs: removed.append((port, kwargs)))
    monkeypatch.setattr(admin.service_manager, "restart", lambda: None)

    assert admin.cmd_network(argparse.Namespace(
        network_command="use", mode="off", url="")) == 0
    assert removed == [(7682, {"https_ports": {"443"}})]


def test_network_cleanup_includes_listener_port_from_marker(
    tmp_path, monkeypatch,
):
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\nport = 8000\nauth_token = "configured"\n\n'
        '[network]\nmode = "off"\n')
    config.with_name("tailscale-serve.json").write_text(
        '{"listener_port":7682,"https_ports":["443"]}')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    inspected = []

    def routes(ports):
        inspected.append(ports)
        return {"443": 7682}

    monkeypatch.setattr(admin, "_managed_tailscale_serve_routes", routes)
    removed = []
    monkeypatch.setattr(
        admin, "_remove_managed_tailscale_serve",
        lambda port, **kwargs: removed.append((port, kwargs)))
    monkeypatch.setattr(admin.service_manager, "restart", lambda: None)

    assert admin.cmd_network(argparse.Namespace(
        network_command="use", mode="off", url="")) == 0
    assert inspected == [{7682, 8000}]
    assert removed == [(8000, {"https_ports": {"443"}})]
    assert not config.with_name("tailscale-serve.json").exists()


def test_uninstall_checks_actual_tailscale_route_even_when_mode_is_off(
    tmp_path, monkeypatch,
):
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\nport = 7000\n\n[network]\nmode = "off"\n')
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setattr(admin, "HOME", tmp_path / "home")
    monkeypatch.setattr(admin, "selected_skills", lambda: [])
    cleaned = []
    monkeypatch.setattr(
        admin, "_managed_tailscale_serve_routes",
        lambda _ports: {"443": 7000})
    monkeypatch.setattr(
        admin, "_remove_managed_tailscale_serve",
        lambda port, **kwargs: cleaned.append((port, kwargs)))
    monkeypatch.setattr(admin.service_manager, "stop_and_disable", lambda: None)
    monkeypatch.setattr(admin.service_manager, "reload_definitions", lambda: None)
    monkeypatch.setattr(
        admin.service_manager, "definition_path", lambda *_args: tmp_path / "unit")

    assert admin.cmd_uninstall(argparse.Namespace(purge_data=False)) == 0
    assert cleaned == [(7000, {"https_ports": {"443"}})]


def test_lan_network_status_reports_bonjour_pairing_url(
        tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\nbind_addr = "0.0.0.0"\nport = 7682\n'
        'auth_token = "configured"\n\n'
        '[network]\nmode = "lan"\nadvertise_lan = true\n')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(admin, "_tailscale_info", lambda: {
        "installed": False, "online": False, "dns_name": ""})

    assert admin.cmd_network(argparse.Namespace(
        network_command="status")) == 0

    status = json.loads(capsys.readouterr().out)
    assert status["pairing_url"] == "http://host.local:7682"
    assert status["auth_configured"] is True


def test_pair_create_json_never_contains_administrator_token(
    tmp_path, monkeypatch, capsys,
):
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\nauth_token = "administrator-secret"\n'
        'public_base_url = "https://friend.example"\n')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin, "SHARE", tmp_path / "share")
    from lib import server_identity
    monkeypatch.setattr(server_identity, "get_server_info", lambda: {
        "server_id": "server-1", "name": "Friend Computer",
    })
    args = argparse.Namespace(
        pair_command="create", url="", name="iPhone", scope="full",
        ttl=600, allow_loopback=False, json=True)
    assert admin.cmd_pair(args) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["uri"].startswith("clarp://pair?")
    assert "administrator-secret" not in value["uri"]
    assert "clp_" in value["uri"]


def test_macos_setup_accepts_platform_recommended_transcription(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        admin, "_execute_setup",
        lambda *args: captured.update(args=args) or 0)
    args = admin.parser().parse_args([
        "setup", "--non-interactive", "--backend", "both",
        "--toolchain", "none", "--transcription", "recommended",
    ])
    assert admin.cmd_setup(args) == 0
    assert captured["args"][1] == "recommended"


def test_doctor_uses_recorded_locked_python(tmp_path, monkeypatch, capsys):
    share = tmp_path / "share"
    current = share / "current"
    current.mkdir(parents=True)
    runtime = tmp_path / "environment/bin/python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("")
    (current / "SERVICE_PYTHON").write_text(str(runtime) + "\n")
    config = tmp_path / "config.toml"
    config.write_text('[tts]\nprovider = "none"\nfallback = "none"\n')
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin, "INSTALL_STATE", tmp_path / "install.json")
    monkeypatch.setenv("CLAUDE_PWA_CONFIG", str(config))
    from lib import config as config_module
    config_module.reset_cache()
    monkeypatch.setattr(admin, "installed_command", lambda _name: "/tool")
    monkeypatch.setattr(admin.service_manager, "is_active", lambda: True)
    monkeypatch.setattr(admin.shutil, "which", lambda _name: None)

    assert admin.cmd_doctor(None) == 0
    assert "locked python" in capsys.readouterr().out


def test_releases_only_returns_completed_installs(tmp_path, monkeypatch):
    root = tmp_path / "share/releases"
    complete = root / "complete"; complete.mkdir(parents=True)
    incomplete = root / "incomplete"; incomplete.mkdir()
    failed = root / "failed"; failed.mkdir()
    (complete / "INSTALL_OK").write_text("")
    (failed / "INSTALL_FAILED").write_text("")
    monkeypatch.setattr(admin, "SHARE", tmp_path / "share")
    assert admin.releases() == [complete]


def test_setup_choices_are_validated_before_mutation(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"skills":[{"id":"clarp-calendar"}]}')
    monkeypatch.setattr(admin, "load_manifest", lambda: json.loads(manifest.read_text()))
    import pytest
    with pytest.raises(SystemExit, match="backend must"):
        admin.validate_setup_choices("typo", [])
    with pytest.raises(SystemExit, match="unknown optional skills"):
        admin.validate_setup_choices("codex", ["clarp-calender"])
    admin.validate_setup_choices("both", ["clarp-calendar"])


def test_fresh_roster_seeding_uses_selected_backend(monkeypatch):
    calls = []
    monkeypatch.setattr(admin, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    admin.seed_fresh_roster("codex")
    assert calls[0][0][-2:] == ("codex", str(admin.HOME))


def test_database_needs_roster_counts_deleted_rows_as_existing(tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    database_path = share / "state.sqlite"
    ambient = tmp_path / "ambient.sqlite"
    with sqlite3.connect(ambient) as database:
        database.execute("CREATE TABLE agents (agent_id TEXT, deleted_at INTEGER)")
        database.execute("INSERT INTO agents VALUES ('ambient', NULL)")
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setenv("CLAUDE_PWA_DB", str(ambient))
    assert admin.database_needs_roster() is True
    with sqlite3.connect(database_path) as database:
        database.execute("CREATE TABLE agents (agent_id TEXT, deleted_at INTEGER)")
        database.execute("INSERT INTO agents VALUES ('old', 1)")
    assert admin.database_needs_roster() is False


def test_setup_refuses_default_live_paths_under_pytest(monkeypatch):
    monkeypatch.setattr(admin, "CONFIG_DIR", admin.xdg.config_dir(admin.HOME))
    monkeypatch.setattr(
        admin, "CONFIG_FILE", admin.xdg.config_dir(admin.HOME) / "config.toml")
    monkeypatch.setattr(
        admin, "INSTALL_STATE", admin.xdg.config_dir(admin.HOME) / "install.json")
    monkeypatch.setattr(admin, "SHARE", admin.xdg.data_dir(admin.HOME))
    with pytest.raises(RuntimeError, match="refusing to run setup"):
        admin._execute_setup(
            "codex", "apple-only", None, None, [], "stable", "none")


def test_setup_restores_live_configuration_when_install_fails(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "config.toml"
    config.write_text('[server]\nport = 7000\n')
    state = config_dir / "install.json"
    state.write_text('{"old": true}')
    share = tmp_path / "share"
    monkeypatch.setattr(admin, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin, "INSTALL_STATE", state)
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setattr(admin, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("install failed")))
    import pytest
    with pytest.raises(RuntimeError, match="install failed"):
        admin._execute_setup("codex", "apple-only", "127.0.0.1", 7682, [], "stable")
    assert config.read_text() == '[server]\nport = 7000\n'
    assert state.read_text() == '{"old": true}'
    assert not (config_dir / "user-values.md").exists()


def test_setup_removes_model_downloaded_by_failed_transaction(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"; config_dir.mkdir()
    config = config_dir / "config.toml"; config.write_text('[server]\nport=7000\n')
    state = config_dir / "install.json"
    monkeypatch.setattr(admin, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin, "INSTALL_STATE", state)
    monkeypatch.setattr(admin, "SHARE", tmp_path / "share")
    monkeypatch.setattr(admin, "import_transcription_modules", lambda: (
        lambda: [], [], "faster-whisper:small.en", lambda _model_id: {
            "id": "faster-whisper:small.en", "provider": "faster-whisper",
            "model": "small.en",
        }))
    installed, removed = [], []
    monkeypatch.setattr(admin, "install_model", installed.append)
    monkeypatch.setattr(admin, "remove_model", lambda model_id, **kwargs: removed.append(
        (model_id, kwargs)))
    monkeypatch.setattr(admin, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("install failed")))
    import pytest
    with pytest.raises(RuntimeError, match="install failed"):
        admin._execute_setup(
            "codex", "recommended", "127.0.0.1", 7682, [], "stable")
    assert installed == ["faster-whisper:small.en"]
    assert removed == [("faster-whisper:small.en", {"allow_active": True})]


def test_setup_reconciles_deselected_optional_skills(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "config.toml"
    config.write_text(
        '[server]\nbind_addr = "192.0.2.10"\nport = 7000\n')
    state = config_dir / "install.json"
    state.write_text('{"skills": ["clarp-old"]}')
    monkeypatch.setattr(admin, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin, "INSTALL_STATE", state)
    monkeypatch.setattr(admin, "SHARE", tmp_path / "share")
    monkeypatch.setattr(admin, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(admin, "load_manifest", lambda: {
        "skills": [{"id": "clarp-core", "pack": "core"}],
    })
    linked, unlinked = [], []
    monkeypatch.setattr(admin, "link_skill", linked.append)
    monkeypatch.setattr(admin, "unlink_skill", unlinked.append)
    admin._execute_setup("codex", "apple-only", None, None, [], "stable")
    assert unlinked == ["clarp-old"]
    assert linked == ["clarp-core"]
    assert json.loads(state.read_text())["skills"] == ["clarp-core"]
    server = __import__("tomllib").loads(config.read_text())["server"]
    assert server["bind_addr"] == "192.0.2.10"
    assert server["port"] == 7000


def test_setup_post_processing_failure_reactivates_previous_release(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "config"; config_dir.mkdir()
    config = config_dir / "config.toml"; config.write_text('[server]\nport=7000\n')
    state = config_dir / "install.json"; state.write_text('{"skills": []}')
    share = tmp_path / "share"
    old = share / "releases/old"; new = share / "releases/new"
    old.mkdir(parents=True); new.mkdir()
    current = share / "current"; current.symlink_to(old, target_is_directory=True)
    monkeypatch.setattr(admin, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    monkeypatch.setattr(admin, "INSTALL_STATE", state)
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setattr(admin, "load_manifest", lambda: {
        "skills": [{"id": "clarp-core", "pack": "core"}],
    })
    def fake_run(*args, **_kwargs):
        if str(args[0]).endswith("install.sh"):
            current.unlink(); current.symlink_to(new, target_is_directory=True)
    monkeypatch.setattr(admin, "run", fake_run)
    monkeypatch.setattr(admin, "link_skill", lambda _skill: (_ for _ in ()).throw(
        RuntimeError("link failed")))
    monkeypatch.setattr(admin, "unlink_skill", lambda _skill: None)
    restored = []
    monkeypatch.setattr(admin, "activate_release", restored.append)
    import pytest
    with pytest.raises(RuntimeError, match="link failed"):
        admin._execute_setup("codex", "apple-only", "127.0.0.1", 7682, [], "stable")
    assert restored == [old]


def test_activate_release_restores_previous_on_restart_failure(tmp_path, monkeypatch):
    share = tmp_path / "share"
    old = share / "releases/old"
    new = share / "releases/new"
    old_toolchain = share / "toolchains/old"
    new_toolchain = share / "toolchains/new"
    old_toolchain.mkdir(parents=True)
    new_toolchain.mkdir(parents=True)
    for release in (old, new):
        release.mkdir(parents=True)
        (release / "server.py").write_text("# server")
        (release / "skills").mkdir()
        (release / "skills/manifest.json").write_text('{"skills": []}')
        (release / "systemd").mkdir()
        (release / "systemd/clarp.service").write_text(
            "ExecStart=@@PYTHON@@ @@SHARE@@/server.py\n"
            "Environment=PATH=@@SERVICE_PATH@@\n")
        (release / "SERVICE_PYTHON").write_text("/runtime/python\n")
        (release / "SERVICE_PATH").write_text("/toolchain/bin:/usr/bin\n")
    (old / "TOOLCHAIN_DIR").write_text(str(old_toolchain) + "\n")
    (new / "TOOLCHAIN_DIR").write_text(str(new_toolchain) + "\n")
    current = share / "current"
    current.symlink_to(old, target_is_directory=True)
    (share / "toolchain").symlink_to(old_toolchain, target_is_directory=True)
    state = tmp_path / "install.json"
    state.write_text('{"skills": []}')
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setattr(admin, "INSTALL_STATE", state)
    monkeypatch.setattr(admin, "HOME", tmp_path)
    live_unit = tmp_path / ".config/systemd/user/clarp.service"
    live_unit.parent.mkdir(parents=True)
    live_unit.write_text("old live unit")
    monkeypatch.setattr(
        admin.service_manager, "install_and_restart",
        lambda: (_ for _ in ()).throw(RuntimeError("restart failed")))
    restored = []
    monkeypatch.setattr(
        admin.service_manager, "restore_after_failed_install",
        lambda **kwargs: restored.append(kwargs))
    import pytest
    with pytest.raises(RuntimeError, match="restart failed"):
        admin.activate_release(new)
    assert current.resolve() == old.resolve()
    assert (share / "toolchain").resolve() == old_toolchain.resolve()
    assert live_unit.read_text() == "old live unit"
    assert restored == [{"had_previous": True}]


def test_prompt_accepts_explicit_watcher_origin(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        admin, "api_request",
        lambda method, path, body: captured.update(
            method=method, path=path, body=body) or {"ok": True},
    )
    args = admin.parser().parse_args([
        "prompt", "--to", "agent", "--text", "Reply arrived",
        "--origin", "watcher",
    ])

    assert admin.cmd_prompt(args) == 0
    assert captured["body"]["origin"] == "watcher"
    assert "sender" not in captured["body"]


def test_prompt_rejects_explicit_origin_with_agent_sender():
    import pytest

    args = admin.parser().parse_args([
        "prompt", "--to", "agent", "--from", "leader",
        "--text", "Delegated work", "--origin", "watcher",
    ])
    with pytest.raises(SystemExit, match="cannot be combined"):
        admin.cmd_prompt(args)

    delayed = admin.parser().parse_args([
        "prompt", "--to", "agent", "--from", "leader",
        "--text", "Delegated work", "--origin", "watcher", "--delay", "5m",
    ])
    with pytest.raises(SystemExit, match="cannot be combined"):
        admin.cmd_prompt(delayed)


def test_git_origin_strips_http_credentials(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run([
        "git", "-C", str(tmp_path), "remote", "add", "origin",
        "https://account:secret@example.test/org/clarp.git?token=secret#ref",
    ], check=True)

    assert admin.git_origin(tmp_path) == "https://example.test/org/clarp.git"

    subprocess.run([
        "git", "-C", str(tmp_path), "remote", "set-url", "origin",
        "https://example.test/org/clarp.git?access_token=secret#ref",
    ], check=True)
    assert admin.git_origin(tmp_path) == "https://example.test/org/clarp.git"


def test_setup_preserves_configured_bind_and_port(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[server]\nbind_addr = "192.0.2.10"\nport = 7700\n')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    args = admin.parser().parse_args(["setup", "--non-interactive"])
    assert args.bind is None and args.port is None
    assert admin.configured_server_value("bind_addr", "127.0.0.1") == "192.0.2.10"
    assert admin.configured_server_value("port", 7682) == 7700


def test_setup_falls_back_to_loopback_without_a_config(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "CONFIG_FILE", tmp_path / "missing.toml")
    assert admin.configured_server_value("bind_addr", "127.0.0.1") == "127.0.0.1"
    assert admin.configured_server_value("port", 7682) == 7682


def test_setup_honours_an_explicit_bind_over_the_configured_one(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[server]\nbind_addr = "192.0.2.10"\nport = 7700\n')
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    args = admin.parser().parse_args([
        "setup", "--non-interactive", "--bind", "0.0.0.0", "--port", "7682",
    ])
    assert args.bind == "0.0.0.0"
    assert args.port == 7682


# ---- issue #12: quick-start installs must be able to find their remote -----

def test_update_remote_prefers_install_state(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "SHARE", tmp_path / "share")
    (tmp_path / "share/current").mkdir(parents=True)
    (tmp_path / "share/current/SOURCE_REMOTE").write_text("https://file.test/x.git\n")
    assert admin.resolve_update_remote(
        {"source_remote": "https://state.test/clarp.git"}) == "https://state.test/clarp.git"


def test_update_remote_falls_back_to_recorded_source_remote_file(tmp_path, monkeypatch):
    share = tmp_path / "share"
    monkeypatch.setattr(admin, "SHARE", share)
    (share / "current").mkdir(parents=True)
    (share / "current/SOURCE_REMOTE").write_text("https://example.test/clarp.git\n")
    assert admin.resolve_update_remote({"source_remote": ""}) == "https://example.test/clarp.git"

    (share / "current/SOURCE_REMOTE").unlink()
    (share / "SOURCE_REMOTE").write_text("https://share.test/clarp.git\n")
    assert admin.resolve_update_remote({}) == "https://share.test/clarp.git"


def test_update_remote_falls_back_to_canonical_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "SHARE", tmp_path / "share")
    assert admin.resolve_update_remote({}) == admin.CANONICAL_SOURCE_REMOTE
    assert admin.CANONICAL_SOURCE_REMOTE.startswith("https://github.com/")


def test_update_remote_strips_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "SHARE", tmp_path / "share")
    assert admin.resolve_update_remote(
        {"source_remote": "https://user:secret@example.test/clarp.git"}
    ) == "https://example.test/clarp.git"


def test_cmd_update_clones_recorded_remote_for_tarball_install(tmp_path, monkeypatch):
    share = tmp_path / "share"
    (share / "current").mkdir(parents=True)
    (share / "current/SOURCE_REMOTE").write_text("https://example.test/clarp.git\n")
    tarball = tmp_path / "clarp-src.abc123"  # unpacked archive, no .git
    tarball.mkdir()
    state_file = tmp_path / "install.json"
    state_file.write_text(json.dumps({
        "source_repo": str(tarball), "source_remote": "", "channel": "stable"}))
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setattr(admin, "INSTALL_STATE", state_file)

    calls: list[tuple] = []

    class Stop(Exception):
        pass

    def fake_run(*cmd, **kwargs):
        calls.append(cmd)
        raise Stop()  # the clone is the assertion; do not go further

    monkeypatch.setattr(admin, "run", fake_run)
    with pytest.raises(Stop):
        admin.cmd_update(argparse.Namespace(ref=""))
    assert calls[0][:2] == ("git", "clone")
    assert "https://example.test/clarp.git" in calls[0]
    assert calls[0][-1] == str(share / "update-source")


def test_cmd_update_never_refuses_for_missing_remote_record(tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    state_file = tmp_path / "install.json"
    state_file.write_text(json.dumps({"source_repo": str(tmp_path / "gone")}))
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setattr(admin, "INSTALL_STATE", state_file)
    calls: list[tuple] = []

    class Stop(Exception):
        pass

    def fake_run(*cmd, **kwargs):
        calls.append(cmd)
        raise Stop()

    monkeypatch.setattr(admin, "run", fake_run)
    with pytest.raises(Stop):
        admin.cmd_update(argparse.Namespace(ref=""))
    assert admin.CANONICAL_SOURCE_REMOTE in calls[0]


# ---- issue #10: setup must hand the user a PWA link that carries the token --

def _config(tmp_path, monkeypatch, body: str):
    config = tmp_path / "config.toml"
    config.write_text(body)
    monkeypatch.setattr(admin, "CONFIG_FILE", config)
    return config


def test_pwa_access_url_carries_the_token(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch,
            '[server]\nport = 7682\nbind_addr = "127.0.0.1"\nauth_token = "abc/def+1"\n')
    assert admin.pwa_access_url() == "http://127.0.0.1:7682/?token=abc%2Fdef%2B1"


def test_pwa_access_url_uses_public_base_url(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch,
            '[server]\npublic_base_url = "https://mac.tail.ts.net/"\nauth_token = "tok"\n')
    assert admin.pwa_access_url() == "https://mac.tail.ts.net/?token=tok"


def test_pwa_access_url_without_auth_is_plain(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, '[server]\nport = 7000\n')
    assert admin.pwa_access_url() == "http://127.0.0.1:7000/"


def test_url_command_prints_the_link(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch, '[server]\nauth_token = "tok"\n')
    assert admin.cmd_url(argparse.Namespace(qr=False, json=False)) == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:7682/?token=tok"


def test_setup_summary_names_the_pwa_link(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, '[server]\nauth_token = "tok"\n')
    text = admin.setup_complete_message()
    assert "http://127.0.0.1:7682/?token=tok" in text
    assert "clarp-admin url" in text
