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


def launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def configured_environment(
    config_file: Path, *, home: Path, inherited: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment intentionally inherited by the service and agent turns.

    The fixed Clarp runtime variables stay authoritative. `SSH_AUTH_SOCK` is
    captured from an interactive installer when the user has not configured an
    explicit value, covering external SSH agents without guessing vendor paths.
    """
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
        if "\0" in value or "\n" in value or "\r" in value:
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
        for name, value in sorted(environment.items())
    )


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
    inherited_environment: dict[str, str] | None = None,
) -> bytes:
    home = (home or Path.home()).expanduser()
    config = Path(os.environ.get("CLARP_CONFIG_DIR", xdg.config_dir(home)))
    extra_environment = configured_environment(
        config / "config.toml", home=home,
        inherited=os.environ if inherited_environment is None else inherited_environment,
    )
    if platform_kind() == "macos":
        cache = Path(os.environ.get("CLARP_CACHE_DIR", xdg.cache_dir(home)))
        logs = home / "Library/Logs/Clarp"
        logs.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [str(python), str(share / "server.py")],
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 2,
            "ProcessType": "Background",
            "WorkingDirectory": str(home),
            "Umask": 0o077,
            "StandardOutPath": str(logs / "server.stdout.log"),
            "StandardErrorPath": str(logs / "server.stderr.log"),
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

    template = (share / "current/systemd/clarp.service").read_text()
    rendered = (template.replace("@@PYTHON@@", argument(python))
                .replace("@@SERVER@@", argument(share / "server.py"))
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
    path.parent.mkdir(parents=True, exist_ok=True)
    inherited = dict(
        os.environ if inherited_environment is None else inherited_environment)
    if not str(inherited.get("SSH_AUTH_SOCK") or "").strip():
        previous_socket = _previous_ssh_auth_sock(path)
        if previous_socket:
            inherited["SSH_AUTH_SOCK"] = previous_socket
    rendered = render(
        python=python, share=share, service_path=service_path, home=home,
        inherited_environment=inherited)
    # `[env]` can intentionally carry secrets. Create the file private before
    # writing any bytes; chmod-after-write has a brief disclosure window.
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".next")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(path)
    return path


def _run(*args: str, check: bool = True, runner: Runner = subprocess.run):
    return runner(args, check=check, text=True, capture_output=True)


def install_and_restart(*, runner: Runner = subprocess.run) -> None:
    path = definition_path()
    if platform_kind() == "macos":
        domain = launchd_domain()
        _run("launchctl", "bootout", domain, str(path), check=False, runner=runner)
        _run("launchctl", "bootstrap", domain, str(path), runner=runner)
        _run("launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}",
             runner=runner)
        return
    _run("systemctl", "--user", "daemon-reload", runner=runner)
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
    *, had_previous: bool, runner: Runner = subprocess.run,
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
        return
    _run("systemctl", "--user", "daemon-reload", check=False, runner=runner)
    if had_previous:
        _run("systemctl", "--user", "restart", "clarp.service",
             check=False, runner=runner)
    else:
        _run("systemctl", "--user", "disable", "--now", "clarp.service",
             check=False, runner=runner)


def stop_and_disable(*, runner: Runner = subprocess.run) -> None:
    if platform_kind() == "macos":
        _run("launchctl", "bootout", launchd_domain(), str(definition_path()),
             check=False, runner=runner)
        return
    _run("systemctl", "--user", "disable", "--now", "clarp.service",
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
