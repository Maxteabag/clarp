"""Per-user service lifecycle for Linux systemd and macOS launchd."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import json
import re
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Callable
import urllib.error
import urllib.request

from . import xdg

LAUNCHD_LABEL = "com.maxteabag.clarp.server"
RUNTIME_LAUNCHD_LABEL = "com.maxteabag.clarp.runtime"
Runner = Callable[..., subprocess.CompletedProcess]
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_ENVIRONMENT = frozenset({
    "HOME", "PATH", "PYTHONUNBUFFERED", "CLARP_SHARE_DIR",
    "CLARP_CONFIG_DIR", "CLARP_CACHE_DIR", "CLAUDE_PWA_CONFIG",
    "CLAUDE_PWA_DB", "CLAUDE_PWA_LOG_DIR",
})


def platform_kind() -> str:
    override = os.environ.get("CLARP_PLATFORM_OVERRIDE", "").strip().lower()
    if override:
        return override
    return "macos" if sys.platform == "darwin" else "linux"


def definition_path(home: Path | None = None) -> Path:
    home = (home or Path.home()).expanduser()
    if platform_kind() == "macos":
        return home / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return config_home / "systemd/user/clarp.service"


def runtime_definition_path(home: Path | None = None) -> Path:
    home = (home or Path.home()).expanduser()
    if platform_kind() == "macos":
        return home / "Library/LaunchAgents" / f"{RUNTIME_LAUNCHD_LABEL}.plist"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return config_home / "systemd/user/clarp-runtime.service"


def launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def configured_environment(
    config_file: Path, *, home: Path, inherited: dict[str, str] | None = None,
) -> dict[str, str]:
    """Validated environment shared by the server and runtime services."""
    try:
        payload = tomllib.loads(config_file.read_text())
    except FileNotFoundError:
        payload = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read service environment: {exc}") from exc
    raw = payload.get("env", {})
    if not isinstance(raw, dict):
        raise ValueError("[env] must be a TOML table")
    result: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _ENVIRONMENT_NAME.fullmatch(name):
            raise ValueError(f"invalid [env] variable name: {name}")
        if (name in _RESERVED_ENVIRONMENT
                or name.startswith("CLARP_")
                or name.startswith("CLAUDE_PWA_")):
            raise ValueError(f"[env].{name} is managed by Clarp")
        if not isinstance(value, str):
            raise ValueError(f"[env].{name} must be a string")
        if any(character in value for character in ("\0", "\n", "\r")):
            raise ValueError(f"[env].{name} contains an unsupported line break")
        result[name] = _expand_home(value, home)
    if "SSH_AUTH_SOCK" not in raw:
        candidate = str((inherited or {}).get("SSH_AUTH_SOCK") or "").strip()
        if candidate:
            result["SSH_AUTH_SOCK"] = _expand_home(candidate, home)
    return result


def _expand_home(value: str, home: Path) -> str:
    if value == "~":
        return str(home)
    if value.startswith("~/"):
        return str(home / value[2:])
    return value


def _systemd_environment_lines(environment: dict[str, str]) -> str:
    def escaped(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return "\n".join(
        f'Environment="{name}={escaped(value)}"'
        for name, value in sorted(environment.items()))


def _previous_ssh_auth_sock(path: Path) -> str:
    try:
        if path.suffix == ".plist":
            payload = plistlib.loads(path.read_bytes())
            environment = payload.get("EnvironmentVariables") or {}
            return str(environment.get("SSH_AUTH_SOCK") or "").strip()
        for line in path.read_text().splitlines():
            if not line.startswith("Environment="):
                continue
            for assignment in shlex.split(line.removeprefix("Environment=")):
                if assignment.startswith("SSH_AUTH_SOCK="):
                    return assignment.split("=", 1)[1].replace("%%", "%").strip()
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    return ""


def render(
    *, python: Path, share: Path, service_path: str, home: Path | None = None,
    runtime: bool = False,
    inherited_environment: dict[str, str] | None = None,
) -> bytes:
    home = (home or Path.home()).expanduser()
    config = Path(os.environ.get("CLARP_CONFIG_DIR", xdg.config_dir(home)))
    extra_environment = configured_environment(
        config / "config.toml", home=home,
        inherited=os.environ if inherited_environment is None else inherited_environment)
    if platform_kind() == "macos":
        cache = Path(os.environ.get("CLARP_CACHE_DIR", xdg.cache_dir(home)))
        logs = home / "Library/Logs/Clarp"
        logs.mkdir(parents=True, exist_ok=True)
        label = RUNTIME_LAUNCHD_LABEL if runtime else LAUNCHD_LABEL
        program = share / "server.py"
        payload = {
            "Label": label,
            "ProgramArguments": (
                [str(share / "bin/clarp-runtime-service")]
                if runtime else [str(python), str(program)]),
            "RunAtLoad": True,
            "KeepAlive": True if runtime else {"SuccessfulExit": False},
            "ThrottleInterval": 2,
            "ProcessType": "Background",
            "WorkingDirectory": str(home),
            "Umask": 0o077,
            "StandardOutPath": str(logs / (
                "runtime.stdout.log" if runtime else "server.stdout.log")),
            "StandardErrorPath": str(logs / (
                "runtime.stderr.log" if runtime else "server.stderr.log")),
            "EnvironmentVariables": {
                **extra_environment,
                "HOME": str(home),
                "PATH": service_path,
                "PYTHONUNBUFFERED": "1",
                "CLARP_SHARE_DIR": str(share),
                "CLARP_CONFIG_DIR": str(config),
                "CLARP_CACHE_DIR": str(cache),
                "CLAUDE_PWA_CONFIG": str(config / "config.toml"),
                "CLAUDE_PWA_DB": str(share / "state.sqlite"),
                "CLAUDE_PWA_LOG_DIR": str(logs),
            },
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)
    cache = Path(os.environ.get("CLARP_CACHE_DIR", xdg.cache_dir(home)))
    logs = cache / "logs"

    def argument(value: str | Path) -> str:
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    def environment_value(value: str | Path) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    template_name = "clarp-runtime.service" if runtime else "clarp.service"
    template = (share / "current/systemd" / template_name).read_text()
    rendered = (template.replace("@@PYTHON@@", argument(python))
                .replace("@@SERVER@@", argument(share / "server.py"))
                .replace("@@RUNTIME@@", argument(share / "runtime.py"))
                .replace("@@RUNTIME_LAUNCHER@@",
                         argument(share / "bin/clarp-runtime-service"))
                .replace("@@SERVICE_PATH@@", environment_value(service_path))
                .replace("@@SHARE_ENV@@", environment_value(share))
                .replace("@@CONFIG_ENV@@", environment_value(config))
                .replace("@@CACHE_ENV@@", environment_value(cache))
                .replace("@@CONFIG_FILE_ENV@@", environment_value(config / "config.toml"))
                .replace("@@DATABASE_ENV@@", environment_value(share / "state.sqlite"))
                .replace("@@LOG_ENV@@", environment_value(logs))
                .replace("@@EXTRA_ENVIRONMENT@@",
                         _systemd_environment_lines(extra_environment)))
    return rendered.encode()


def write_definition(
    *, python: Path, share: Path, service_path: str, home: Path | None = None,
    inherited_environment: dict[str, str] | None = None,
) -> Path:
    path = definition_path(home)
    runtime_path = runtime_definition_path(home)
    runtime_supported = (
        (share / "current/runtime.py").is_file()
        and (share / "current/systemd/clarp-runtime.service").is_file()
    )
    inherited = dict(
        os.environ if inherited_environment is None else inherited_environment)
    if not str(inherited.get("SSH_AUTH_SOCK") or "").strip():
        for previous in (runtime_path, path):
            previous_socket = _previous_ssh_auth_sock(previous)
            if previous_socket:
                inherited["SSH_AUTH_SOCK"] = previous_socket
                break
    targets = [(path, False)]
    if runtime_supported:
        targets.append((runtime_path, True))
    for target, is_runtime in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        rendered = render(
            python=python, share=share, service_path=service_path, home=home,
            runtime=is_runtime, inherited_environment=inherited)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".next")
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(rendered)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        temporary.replace(target)
    if not runtime_supported:
        runtime_path.unlink(missing_ok=True)
    return path


def _run(*args: str, check: bool = True, runner: Runner = subprocess.run):
    return runner(args, check=check, text=True, capture_output=True)


def _legacy_busy_sessions() -> list[str]:
    """Busy rows owned by a pre-runtime monolith during the one-time split."""
    try:
        from . import agents as agents_db
        return [
            str(agent["session"]) for agent in agents_db.list_agents()
            if agents_db.is_busy(str(agent["agent_id"]))
        ]
    except Exception as exc:  # uncertainty must never authorize a destructive cutover
        raise RuntimeError(
            f"cannot prove the legacy Clarp server is idle: {exc}") from exc


def _require_legacy_idle() -> None:
    busy = _legacy_busy_sessions()
    if busy:
        raise RuntimeError(
            "initial clarp-runtime migration requires idle agents; still busy: "
            + ", ".join(busy))


def install_and_restart(*, runner: Runner = subprocess.run) -> None:
    path = definition_path()
    runtime_path = runtime_definition_path()
    runtime_supported = runtime_path.is_file()
    if platform_kind() == "macos":
        domain = launchd_domain()
        if not runtime_supported:
            _run("launchctl", "bootout", domain, str(runtime_path),
                 check=False, runner=runner)
            _run("launchctl", "bootout", domain, str(path),
                 check=False, runner=runner)
            _run("launchctl", "bootstrap", domain, str(path), runner=runner)
            _run("launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}",
                 runner=runner)
            return
        runtime_target = f"{domain}/{RUNTIME_LAUNCHD_LABEL}"
        running = _run(
            "launchctl", "print", runtime_target, check=False, runner=runner)
        if running.returncode != 0:
            _require_legacy_idle()
        # On the one-time migration from the monolith, the old server still
        # owns provider children. Never start a second process owner beside it.
        _run("launchctl", "bootout", domain, str(path), check=False, runner=runner)
        if running.returncode != 0:
            _run("launchctl", "bootstrap", domain,
                 str(runtime_definition_path()), runner=runner)
        _run("launchctl", "bootstrap", domain, str(path), runner=runner)
        _run("launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}",
             runner=runner)
        return
    _run("systemctl", "--user", "daemon-reload", runner=runner)
    if not runtime_supported:
        _run("systemctl", "--user", "disable", "--now",
             "clarp-runtime.service", check=False, runner=runner)
        _run("systemctl", "--user", "enable", "clarp.service", runner=runner)
        _run("systemctl", "--user", "restart", "clarp.service", runner=runner)
        return
    runtime_running = _run(
        "systemctl", "--user", "is-active", "--quiet",
        "clarp-runtime.service", check=False, runner=runner)
    if runtime_running.returncode != 0:
        _require_legacy_idle()
        _run("systemctl", "--user", "stop", "clarp.service",
             check=False, runner=runner)
    _run("systemctl", "--user", "enable", "--now", "clarp-runtime.service",
         runner=runner)
    _run("systemctl", "--user", "enable", "clarp.service", runner=runner)
    _run("systemctl", "--user", "restart", "clarp.service", runner=runner)


def restart(*, runner: Runner = subprocess.run, check: bool = True) -> None:
    if platform_kind() == "macos":
        domain = launchd_domain()
        result = _run(
            "launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}",
            check=False, runner=runner)
        if check and result.returncode != 0:
            _run("launchctl", "bootstrap", domain, str(definition_path()),
                 runner=runner)
        return
    _run("systemctl", "--user", "daemon-reload", check=False, runner=runner)
    _run("systemctl", "--user", "restart", "clarp.service", check=check,
         runner=runner)


def restore_after_failed_install(
    *, had_previous: bool, had_runtime_previous: bool = True,
    runner: Runner = subprocess.run,
) -> None:
    """Make the restored service definition authoritative after rollback."""
    if platform_kind() == "macos":
        domain = launchd_domain()
        _run("launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}",
             check=False, runner=runner)
        if had_previous and definition_path().is_file():
            _run("launchctl", "bootstrap", domain, str(definition_path()),
                 runner=runner)
            _run("launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}",
                 runner=runner)
        if not had_runtime_previous:
            _run("launchctl", "bootout", domain,
                 str(runtime_definition_path()), check=False, runner=runner)
        return
    _run("systemctl", "--user", "daemon-reload", check=False, runner=runner)
    if had_previous:
        _run("systemctl", "--user", "restart", "clarp.service",
             check=False, runner=runner)
    else:
        _run("systemctl", "--user", "disable", "--now", "clarp.service",
             check=False, runner=runner)
    if not had_runtime_previous:
        _run("systemctl", "--user", "disable", "--now",
             "clarp-runtime.service", check=False, runner=runner)


def stop_and_disable(*, runner: Runner = subprocess.run) -> None:
    if platform_kind() == "macos":
        _run("launchctl", "bootout", launchd_domain(), str(definition_path()),
             check=False, runner=runner)
        _run("launchctl", "bootout", launchd_domain(),
             str(runtime_definition_path()), check=False, runner=runner)
        return
    _run("systemctl", "--user", "disable", "--now", "clarp.service",
         check=False, runner=runner)
    _run("systemctl", "--user", "disable", "--now", "clarp-runtime.service",
         check=False, runner=runner)
    _run("systemctl", "--user", "daemon-reload", check=False, runner=runner)


def reload_definitions(*, runner: Runner = subprocess.run) -> None:
    """Forget a service definition after its file has been removed."""
    if platform_kind() == "linux":
        _run("systemctl", "--user", "daemon-reload", check=False, runner=runner)


def is_active(*, runner: Runner = subprocess.run) -> bool:
    if platform_kind() == "macos":
        result = _run("launchctl", "print",
                      f"{launchd_domain()}/{LAUNCHD_LABEL}", check=False,
                      runner=runner)
    else:
        result = _run("systemctl", "--user", "is-active", "--quiet",
                      "clarp.service", check=False, runner=runner)
    return result.returncode == 0


def is_runtime_active(*, runner: Runner = subprocess.run) -> bool:
    if platform_kind() == "macos":
        result = _run(
            "launchctl", "print", f"{launchd_domain()}/{RUNTIME_LAUNCHD_LABEL}",
            check=False, runner=runner)
    else:
        result = _run(
            "systemctl", "--user", "is-active", "--quiet",
            "clarp-runtime.service", check=False, runner=runner)
    return result.returncode == 0


def wait_until_ready(
    config_path: Path, *, expected_release_id: str, timeout: float = 20.0,
) -> None:
    """Wait for the newly activated server to answer its authenticated status."""
    config = tomllib.loads(config_path.read_text())
    server = config.get("server", {})
    host = str(server.get("bind_addr", "127.0.0.1")).strip()
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1" if host == "0.0.0.0" else "::1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    url = f"http://{host}:{int(server.get('port', 7682))}/status"
    request = urllib.request.Request(url)
    token = str(server.get("auth_token", "")).strip()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last_error = "server did not answer"
    while time.monotonic() < deadline:
        try:
            with opener.open(request, timeout=1.0) as response:
                if response.status == 200:
                    payload = json.loads(response.read())
                    if payload.get("release_id") == expected_release_id:
                        return
                    last_error = (
                        "another Clarp release answered: "
                        f"{payload.get('release_id') or 'unknown'}")
                    continue
                last_error = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError(
        f"Clarp did not become ready at {url} within {timeout:g}s: {last_error}")


def launch_detached(
    command: list[str], *, unit: str, delay_seconds: int = 0,
    environment: dict[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> tuple[bool, str]:
    """Launch work independently of the server service on Linux or macOS."""
    if (platform_kind() == "linux"
            and os.environ.get("CLARP_DEPLOYMENT_MODE") != "container"):
        args = ["systemd-run", "--user", "--collect", f"--unit={unit}"]
        if delay_seconds:
            args.append(f"--on-active={int(delay_seconds)}s")
        else:
            args.append("--property=Type=exec")
        if environment:
            for name, value in sorted(environment.items()):
                args.append(f"--setenv={name}={value}")
        result = _run(*args, *command, check=False, runner=runner)
        return result.returncode == 0, (result.stderr or "").strip()
    logs = Path(os.environ.get("CLAUDE_PWA_LOG_DIR", xdg.log_dir()))
    logs.mkdir(parents=True, exist_ok=True)
    output = (logs / f"{unit}.log").open("ab")
    argv = list(command)
    if delay_seconds:
        argv = [
            "/bin/sh", "-c", 'sleep "$1"; shift; exec "$@"',
            "clarp-delay", str(int(delay_seconds)), *argv,
        ]
    try:
        subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
            env={**os.environ, **(environment or {})},
        )
    except OSError as exc:
        output.close()
        return False, str(exc)
    output.close()
    return True, ""
