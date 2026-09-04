from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_installer_leaves_pre_clarp_paths_untouched(tmp_path):
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)

    legacy_paths = {
        home / ".local/share/claude-pwa/data.txt": "legacy data\n",
        home / ".config/claude-pwa/config.toml": "legacy config\n",
        home / ".cache/claude-pwa/cache.txt": "legacy cache\n",
        home / ".cache/claude-tts/hook.log": "legacy hook\n",
        home / ".config/systemd/user/claude-pwa.service": "legacy unit\n",
    }
    for path, contents in legacy_paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    old_streamer = home / ".local/bin/claude-tts-streamer.py"
    old_streamer.parent.mkdir(parents=True, exist_ok=True)
    old_streamer.symlink_to("/legacy/claude-tts-streamer.py")

    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "CLARP_PLATFORM_OVERRIDE": "linux",
        "CLARP_SKIP_ENV": "1",
        "CLARP_SKIP_HEALTHCHECK": "1",
        "PYTHON": subprocess.check_output(["which", "python3"], text=True).strip(),
        "PATH": f"{fake_bin}:{env['PATH']}",
    })
    for var in ("CLARP_CONFIG_DIR", "CLARP_SHARE_DIR", "CLARP_CACHE_DIR"):
        env.pop(var, None)
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        env.pop(var, None)

    subprocess.run([str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)

    for path, contents in legacy_paths.items():
        assert path.read_text() == contents
    assert old_streamer.is_symlink()
    assert os.readlink(old_streamer) == "/legacy/claude-tts-streamer.py"


def test_installer_creates_versioned_release_and_compatibility_links(tmp_path):
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n'
        'case "$*" in\n'
        '  *"restart clarp.service"*) '
        '[ "${CLARP_TEST_FAIL_RESTART:-0}" = 1 ] && exit 9 ;;\n'
        'esac\n'
        'exit 0\n'
    )
    systemctl.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "CLARP_PLATFORM_OVERRIDE": "linux",
        "CLARP_SKIP_ENV": "1",
        "CLARP_SKIP_HEALTHCHECK": "1",
        "PYTHON": subprocess.check_output(["which", "python3"], text=True).strip(),
        "PATH": f"{fake_bin}:{env['PATH']}",
    })
    for var in ("CLARP_CONFIG_DIR", "CLARP_SHARE_DIR", "CLARP_CACHE_DIR"):
        env.pop(var, None)
    config = home / ".config/clarp"
    config.mkdir(parents=True, exist_ok=True)
    (config / "install.json").write_text(
        '{"skills":["clarp-media"],"toolchain":"none"}\n')

    subprocess.run([str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)

    share = home / ".local/share/clarp"
    current = share / "current"
    assert current.is_symlink()
    assert (current / "server.py").is_file()
    assert (current / "runtime.py").is_file()
    assert (current / "RUNTIME_RELEASE_ID").read_text().strip()
    assert (current / "RUNTIME_READY").read_text().strip() == "ready"
    assert (current / "SOURCE_REMOTE").read_text().strip()
    assert (current / "skills/manifest.json").is_file()
    assert (current / "scripts/agent_tasks.py").is_file()
    assert (current / "scripts/agent_artifacts.py").is_file()
    assert (current / "scripts/github_workflow_artifact.py").is_file()
    assert (current / "scripts/server_update_job.py").is_file()
    assert (current / "scripts/transcription_model_job.py").is_file()
    assert (current / "scripts/portrait_generation_job.py").is_file()
    assert (current / "bin/clarp-tui.py").is_file()
    assert (current / "TOOLCHAIN_MODE").read_text().strip() == "none"
    selected = json.loads((config / "install.json").read_text())["skills"]
    assert "clarp-tasks" in selected
    assert "clarp-agent-admin" in selected
    assert "clarp-issue-reporting" in selected
    assert (home / ".claude/skills/clarp-tasks").is_symlink()
    assert (home / ".codex/skills/clarp-tasks").is_symlink()
    assert (home / ".claude/skills/clarp-issue-reporting").is_symlink()
    assert (home / ".codex/skills/clarp-issue-reporting").is_symlink()
    assert (share / "server.py").resolve() == (current / "server.py").resolve()
    assert (share / "runtime.py").resolve() == (current / "runtime.py").resolve()
    assert (home / ".config/systemd/user/clarp-runtime.service").is_file()
    for name in (
        "clarp-admin", "clarp-tui", "clarp-agent-tasks", "clarp-agent-artifacts",
        "clarp-media-publish", "clarp-agent-bg",
        "clarp-runtime-service",
    ):
        wrapper = home / ".local/bin" / name
        assert wrapper.is_file()
        assert "# managed-by-clarp" in wrapper.read_text()
        assert f"active={share / 'current'}" in wrapper.read_text()
        assert f"fallback={current.resolve()}" in wrapper.read_text()

    first_release = current.resolve()
    first_runtime_release = (first_release / "RUNTIME_RELEASE_ID").read_text()
    stale_toolchain = share / "toolchains/stale"
    (stale_toolchain / "bin").mkdir(parents=True)
    stale_codex = stale_toolchain / "bin/codex"
    stale_codex.write_text("#!/bin/sh\n")
    stale_codex.chmod(0o700)
    (share / "toolchain").symlink_to(stale_toolchain, target_is_directory=True)
    (home / ".local/bin/codex").symlink_to(stale_codex)
    external_bin = tmp_path / "external-bin"
    external_bin.mkdir()
    external_codex = external_bin / "codex"
    external_codex.write_text("#!/bin/sh\n")
    external_codex.chmod(0o700)
    env["PATH"] = f"{home / '.local/bin'}:{external_bin}:{env['PATH']}"
    subprocess.run([str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)
    second_release = current.resolve()
    assert (second_release / "RUNTIME_RELEASE_ID").read_text() != first_runtime_release
    assert (second_release / "TOOLCHAIN_MODE").read_text().strip() == "none"
    assert not (share / "toolchain").exists()
    service_path = (second_release / "SERVICE_PATH").read_text().strip().split(":")
    assert str(home / ".local/bin") not in service_path
    assert str(external_bin) in service_path
    assert "/usr/sbin" in service_path
    assert "/sbin" in service_path
    previous_unit = (home / ".config/systemd/user/clarp.service").read_text()
    assert second_release != first_release
    assert first_release.is_dir(), "same-version reinstall must retain active predecessor"
    assert len(list((share / "releases").iterdir())) == 2

    legacy = share / "releases/legacy-no-metadata"
    legacy.mkdir()
    current.unlink()
    current.symlink_to(legacy, target_is_directory=True)
    recovered_paths = json.loads(subprocess.check_output(
        [str(home / ".local/bin/clarp-admin"), "paths"], env=env, text=True))
    assert recovered_paths["share"] == str(share)
    task_rows = json.loads(subprocess.check_output(
        [str(home / ".local/bin/clarp-agent-tasks"), "show", "mike"],
        env=env, text=True))
    assert isinstance(task_rows, dict)
    current.unlink()
    current.symlink_to(second_release, target_is_directory=True)

    failed_env = dict(env, CLARP_TEST_FAIL_RESTART="1")
    failed = subprocess.run([str(ROOT / "install.sh")], cwd=ROOT, env=failed_env)
    assert failed.returncode != 0
    assert current.resolve() == second_release
    assert (home / ".config/systemd/user/clarp.service").read_text() == previous_unit
    failed_releases = [
        item for item in (share / "releases").iterdir()
        if (item / "INSTALL_FAILED").is_file()
    ]
    assert len(failed_releases) == 1


def test_unit_execstart_points_at_the_installed_share_dir(tmp_path):
    """The unit used to hardcode ~/.local/share/claude-pwa/server.py. That
    survived the rename and made the service run the old tree while the
    installer staged into the new one — two servers, one database."""
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("systemctl", "jq", "claude", "clarp", "codex"):
        stub = fake_bin / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "CLARP_PLATFORM_OVERRIDE": "linux",
        "PATH": f"{fake_bin}:{env['PATH']}",
        "CLARP_SKIP_ENV": "1",
        "CLARP_SKIP_HEALTHCHECK": "1",
        "PYTHON": subprocess.check_output(["which", "python3"], text=True).strip(),
    })
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                "CLARP_CONFIG_DIR", "CLARP_SHARE_DIR", "CLARP_CACHE_DIR"):
        env.pop(var, None)
    subprocess.run(["bash", str(ROOT / "install.sh")], env=env,
                   capture_output=True, text=True, timeout=300)

    unit = home / ".config/systemd/user/clarp.service"
    assert unit.is_file(), "unit was not installed"
    exec_line = next(l for l in unit.read_text().splitlines()
                     if l.startswith("ExecStart="))
    assert "/.local/share/clarp/server.py" in exec_line, exec_line
    assert "claude-pwa" not in exec_line.rsplit(" ", 1)[-1], exec_line


def test_macos_installer_writes_and_bootstraps_launch_agent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "launchctl.calls"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {calls}\n"
        "exit 0\n")
    launchctl.chmod(0o755)
    for name in ("claude", "codex"):
        stub = fake_bin / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
        "CLARP_PLATFORM_OVERRIDE": "macos",
        "CLARP_SKIP_ENV": "1",
        "CLARP_SKIP_HEALTHCHECK": "1",
        "PYTHON": subprocess.check_output(["which", "python3"], text=True).strip(),
    })
    for var in ("CLARP_CONFIG_DIR", "CLARP_SHARE_DIR", "CLARP_CACHE_DIR"):
        env.pop(var, None)

    subprocess.run([str(ROOT / "install.sh")], cwd=ROOT, env=env, check=True)

    share = home / "Library/Application Support/Clarp"
    plist_path = home / (
        "Library/LaunchAgents/com.maxteabag.clarp.server.plist")
    assert (share / "current/server.py").is_file()
    assert plist_path.is_file()
    plist = __import__("plistlib").loads(plist_path.read_bytes())
    assert plist["ProgramArguments"][1] == str(share / "server.py")
    paths = json.loads(subprocess.check_output(
        [str(home / ".local/bin/clarp-admin"), "paths"], env=env, text=True))
    assert paths["share"] == str(share)
    assert paths["config"] == str(
        home / "Library/Application Support/Clarp/config.toml")
    launch_calls = calls.read_text()
    assert "bootstrap" in launch_calls
    assert "kickstart -k" in launch_calls
