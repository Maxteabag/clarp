"""Per-user service lifecycle for Linux systemd and macOS launchd."""
from __future__ import annotations

import os
import math
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
SERVER_UNIT = "clarp.service"
RUNTIME_UNIT = "clarp-runtime.service"
_LAUNCHD_LABELS = ((SERVER_UNIT, LAUNCHD_LABEL), (RUNTIME_UNIT, RUNTIME_LAUNCHD_LABEL))
Runner = Callable[..., subprocess.CompletedProcess]
# Why a unit action was refused (ServiceActionRefused.reason); a closed set.
REFUSAL_REASONS = (
    "not_in_unit",           # a Host that does not run inside the unit it would act on
    "no_definition",         # no readable service definition where this process looks
    "foreign_directories",   # the definition names another share directory or database
    "foreign_definition",    # the service manager loaded the unit from another file
    "manager_unavailable",   # the service manager could not be asked
    "disposable_host",       # tests/qa/host.py: a QA Host never touches a service
)


class ServiceActionRefused(RuntimeError):
    """This process has no standing to touch the Clarp services: nothing was
    run and nothing was written."""

    def __init__(self, action: str, reason: str, detail: str):
        super().__init__(f"Clarp service action refused ({action}): {detail}")
        self.action, self.reason, self.detail = action, reason, detail
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


def _definition_environment(path: Path) -> dict[str, str] | None:
    """The environment a service definition gives its process; None when the
    file is missing or unreadable."""
    try:
        if path.suffix == ".plist":
            payload = plistlib.loads(path.read_bytes())
            return {str(name): str(value) for name, value in (
                payload.get("EnvironmentVariables") or {}).items()}
        environment: dict[str, str] = {}
        for line in path.read_text().splitlines():
            if not line.startswith("Environment="):
                continue
            for assignment in shlex.split(line.removeprefix("Environment=")):
                name, separator, value = assignment.partition("=")
                if separator:
                    environment[name] = value.replace("%%", "%")
        return environment
    except (OSError, ValueError, AttributeError, plistlib.InvalidFileException):
        return None


def _previous_ssh_auth_sock(path: Path) -> str:
    return str((_definition_environment(path) or {}).get("SSH_AUTH_SOCK") or "").strip()


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
    _standing("write", "write", share=share, definitions=(path, runtime_path))
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


# ---- who may touch the services ---------------------------------------------
#
# The units have fixed names (clarp.service, clarp-runtime.service) and every
# process of the user shares one service manager. A disposable Host, a test
# installation or a command run with another environment would therefore stop,
# restart or rewrite the LIVE Host's services (issue 322).
#
# So every entry point of this module that touches a unit, a unit file or the
# service manager first obtains a standing from _standing(), and _run() (the
# only place a service-manager command is executed) takes nothing else. Each
# standing is proved by facts the caller cannot set by accident:
#
#   host          restart(): the process runs inside clarp.service (its kernel
#                 cgroup on Linux, launchd's XPC_SERVICE_NAME on macOS) and the
#                 installed definition names its share directory and database.
#   admin         install.sh and clarp-admin (restart(admin=True), install,
#                 restore, stop, remove): a definition exists where the process
#                 looks, each names its share directory, and the service
#                 manager loaded the units from those very files or does not
#                 know them yet (a first installation).
#   member        launch_detached() and the status questions: the installed
#                 definition names the process's share directory and database.
#   installation  wait_until_ready(): the installed definition names the
#                 process's share directory.
#   write         write_definition(): the share directory written is the
#                 process's own, and no definition of another installation is
#                 replaced.
#
# A refusal is logged and raised (ServiceActionRefused) before anything runs
# or is written. Only the admin path, once its own definitions match, asks the
# manager one read-only question (show / print); every other refusal is
# decided from files and the kernel, without a command.

class _Standing:
    """Proof that _standing() let this caller act. _run takes nothing else."""
    __slots__ = ("kind", "action")

    def __init__(self, kind: str, action: str):
        self.kind, self.action = kind, action

    def __repr__(self) -> str:
        return f"<standing {self.kind} for {self.action}>"


# The admin path's read-only question about the units; _run lets it ask
# nothing else.
_ASKING = _Standing("question", "ask")
_QUESTIONS = (("systemctl", "--user", "show"), ("launchctl", "print"))


def _run(standing: _Standing, *args: str, check: bool = True,
         runner: Runner = subprocess.run):
    """Run one service-manager command for a caller that has standing."""
    if not isinstance(standing, _Standing):
        raise TypeError("a service-manager command needs a standing from _standing()")
    if standing is _ASKING and not any(args[:len(q)] == q for q in _QUESTIONS):
        raise TypeError("only the standing question runs before standing is decided")
    return runner(args, check=check, text=True, capture_output=True)


def refuse(action: str, reason: str, detail: str) -> ServiceActionRefused:
    """Log one refusal and return it for the caller to raise or to answer with."""
    refusal = ServiceActionRefused(action, reason, detail)
    line = f"action={action} reason={reason} {detail}"
    try:
        from .log import log
        log("serviceActionRefused", line)
    except Exception as exc:  # noqa: BLE001 - the refusal stands without its log sink
        print(f"serviceActionRefused {line} (log failed: {type(exc).__name__})",
              file=sys.stderr, flush=True)
    return refusal


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.realpath(str(left)) == os.path.realpath(str(right))


def _own_cgroup() -> str:
    """This process's systemd cgroup path from the kernel, "" when unknown."""
    try:
        text = Path("/proc/self/cgroup").read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if (hierarchy == "0" and not controllers) or controllers == "name=systemd":
            return path.strip()
    return ""


def runs_in_unit(unit: str) -> bool:
    """Whether the service manager started this process as part of ``unit``."""
    if platform_kind() == "macos":
        return os.environ.get("XPC_SERVICE_NAME", "") == dict(_LAUNCHD_LABELS)[unit]
    parts = _own_cgroup().split("/")
    manager = f"user@{os.getuid()}.service"
    return manager in parts and unit in parts[parts.index(manager) + 1:]


def _account_home() -> Path:
    """The home the user's service manager works in, whatever HOME says here."""
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path.home()


def _installed_definition(unit: str) -> Path:
    """The definition the service manager most plausibly runs ``unit`` from.

    For a caller that only asks whether it belongs to the installation: where
    this process would look, else the account's default location, so a process
    of the installation with a private XDG_CONFIG_HOME still finds it. The
    admin path and write_definition use definition_path(): they manage files
    exactly where they look."""
    runtime = unit == RUNTIME_UNIT
    here = runtime_definition_path() if runtime else definition_path()
    if here.is_file():
        return here
    home = _account_home()
    default = (home / "Library/LaunchAgents" / here.name if platform_kind() == "macos"
               else home / ".config/systemd/user" / here.name)
    return default if default.is_file() else here


def _directories_refusal(
    action: str, definition: Path, *, database: bool,
) -> ServiceActionRefused | None:
    """The installed definition must name the share directory this process
    works on and, for a process that keeps state (``database``), its database."""
    environment = _definition_environment(definition)
    if environment is None:
        return refuse(action, "no_definition",
                      f"no readable service definition at {definition}")
    named_share = environment.get("CLARP_SHARE_DIR", "")
    share = os.environ.get("CLARP_SHARE_DIR") or str(xdg.data_dir())
    if not named_share or not _same_path(share, named_share):
        return refuse(
            action, "foreign_directories",
            f"{definition} serves the share directory {named_share or '(none)'}, "
            f"this process works on {share}")
    if not database:
        # install.sh and clarp-admin install and restart what lives in their
        # share directory and open no database of their own: a CLAUDE_PWA_DB
        # inherited from their caller says nothing about what they act on.
        return None
    # lib.db's rule for where a Host keeps its state.
    own_database = os.environ.get("CLAUDE_PWA_DB") or str(xdg.data_dir() / "state.sqlite")
    named_database = environment.get("CLAUDE_PWA_DB") or str(
        Path(named_share) / "state.sqlite")
    if not _same_path(own_database, named_database):
        return refuse(
            action, "foreign_directories",
            f"{definition} serves the database {named_database}, "
            f"this process works on {own_database}")
    return None


def _write_refusal(
    action: str, share: Path | str, definitions: tuple[Path, ...],
) -> ServiceActionRefused | None:
    """Definitions are written for this process's own share directory, and
    never over another installation's."""
    own = os.environ.get("CLARP_SHARE_DIR") or str(xdg.data_dir())
    if not _same_path(share, own):
        return refuse(
            action, "foreign_directories",
            f"definitions for the share directory {share} were asked for, "
            f"this process works on {own}")
    for definition in definitions:
        named = (_definition_environment(definition) or {}).get("CLARP_SHARE_DIR", "")
        if named and not _same_path(named, share):
            return refuse(
                action, "foreign_directories",
                f"{definition} serves the share directory {named}; "
                f"it is not replaced by one for {share}")
    return None


def _loaded_definitions(runner: Runner) -> dict[str, str] | None:
    """Unit -> file the service manager loaded it from ("" when not loaded).
    None when the manager could not be asked."""
    loaded: dict[str, str] = {}
    try:
        if platform_kind() == "macos":
            for unit, label in _LAUNCHD_LABELS:
                result = _run(_ASKING, "launchctl", "print", f"{launchd_domain()}/{label}",
                              check=False, runner=runner)
                # `print` fails for a job that is not loaded. Its text is not a
                # stable format: a loaded job that names no path counts as not
                # loaded rather than refusing every macOS update.
                paths = [] if result.returncode != 0 else [
                    line.split("=", 1)[1].strip()
                    for line in (result.stdout or "").splitlines()
                    if line.strip().startswith("path = ")]
                loaded[unit] = paths[0] if paths else ""
            return loaded
        result = _run(_ASKING, "systemctl", "--user", "show", "-p", "Id", "-p", "LoadState",
                      "-p", "FragmentPath", SERVER_UNIT, RUNTIME_UNIT,
                      check=False, runner=runner)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    for block in (result.stdout or "").split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        unit, state = fields.get("Id", ""), fields.get("LoadState", "")
        if not unit or not state:
            continue
        fragment = fields.get("FragmentPath", "")
        # A unit the manager knows without a file of ours (transient, masked)
        # is someone else's: report a path no definition of ours can equal.
        loaded[unit] = fragment or ("" if state == "not-found" else f"({state})")
    return loaded if {SERVER_UNIT, RUNTIME_UNIT} <= loaded.keys() else None


def _admin_refusal(action: str, runner: Runner) -> ServiceActionRefused | None:
    """Why the admin path may not take ``action``, or None when it may."""
    definitions = ((SERVER_UNIT, definition_path()), (RUNTIME_UNIT, runtime_definition_path()))
    present = [definition for _, definition in definitions if definition.is_file()]
    if not present:
        # Nothing says whose services these are. A caller that cleans up after
        # itself acts while its definitions still exist.
        return refuse(action, "no_definition",
                      f"no service definition at {definitions[0][1]}")
    for definition in present:
        refusal = _directories_refusal(action, definition, database=False)
        if refusal is not None:
            return refusal
    loaded = _loaded_definitions(runner)
    if loaded is None:
        return refuse(action, "manager_unavailable",
                      "the service manager did not say which definitions it loaded")
    for unit, definition in definitions:
        if loaded[unit] and not _same_path(loaded[unit], definition):
            return refuse(
                action, "foreign_definition",
                f"the service manager runs {unit} from {loaded[unit]}, "
                f"this process manages {definition}")
    return None


def _standing(
    kind: str, action: str, runner: Runner = subprocess.run, *,
    share: Path | str | None = None, definitions: tuple[Path, ...] = (),
) -> _Standing:
    """Decide whether this process may take ``action``; raise ServiceActionRefused
    (already logged) when it may not. The one gate of this module."""
    if kind == "host":
        refusal = None if runs_in_unit(SERVER_UNIT) else refuse(
            action, "not_in_unit", f"this process does not run inside {SERVER_UNIT}")
        refusal = refusal or _directories_refusal(
            action, _installed_definition(SERVER_UNIT), database=True)
    elif kind == "member":
        refusal = _directories_refusal(action, _installed_definition(SERVER_UNIT), database=True)
    elif kind == "installation":
        refusal = _directories_refusal(action, _installed_definition(SERVER_UNIT), database=False)
    elif kind == "write":
        refusal = _write_refusal(action, share or "", definitions)
    elif kind == "admin":
        refusal = _admin_refusal(action, runner)
    else:
        raise ValueError(f"unknown standing: {kind}")
    if refusal is not None:
        raise refusal
    return _Standing(kind, action)


def host_refusal(action: str) -> ServiceActionRefused | None:
    """Why this Host may not take ``action`` on its own service, or None when it may.

    Reads this process's own facts and the installed definition only: no
    service-manager command runs, so a route can ask before it answers."""
    try:
        _standing("host", action)
    except ServiceActionRefused as refusal:
        return refusal
    return None


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
    standing = _standing("admin", "install", runner)
    path = definition_path()
    runtime_path = runtime_definition_path()
    runtime_supported = runtime_path.is_file()
    if platform_kind() == "macos":
        domain = launchd_domain()
        if not runtime_supported:
            _run(standing, "launchctl", "bootout", domain, str(runtime_path),
                 check=False, runner=runner)
            _run(standing, "launchctl", "bootout", domain, str(path),
                 check=False, runner=runner)
            _run(standing, "launchctl", "bootstrap", domain, str(path), runner=runner)
            _run(standing, "launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}",
                 runner=runner)
            return
        runtime_target = f"{domain}/{RUNTIME_LAUNCHD_LABEL}"
        running = _run(standing, 
            "launchctl", "print", runtime_target, check=False, runner=runner)
        if running.returncode != 0:
            _require_legacy_idle()
        # On the one-time migration from the monolith, the old server still
        # owns provider children. Never start a second process owner beside it.
        _run(standing, "launchctl", "bootout", domain, str(path), check=False, runner=runner)
        if running.returncode != 0:
            _run(standing, "launchctl", "bootstrap", domain,
                 str(runtime_definition_path()), runner=runner)
        _run(standing, "launchctl", "bootstrap", domain, str(path), runner=runner)
        _run(standing, "launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}",
             runner=runner)
        return
    _run(standing, "systemctl", "--user", "daemon-reload", runner=runner)
    if not runtime_supported:
        _run(standing, "systemctl", "--user", "disable", "--now",
             "clarp-runtime.service", check=False, runner=runner)
        _run(standing, "systemctl", "--user", "enable", "clarp.service", runner=runner)
        _run(standing, "systemctl", "--user", "restart", "clarp.service", runner=runner)
        return
    runtime_running = _run(standing, 
        "systemctl", "--user", "is-active", "--quiet",
        "clarp-runtime.service", check=False, runner=runner)
    if runtime_running.returncode != 0:
        _require_legacy_idle()
        _run(standing, "systemctl", "--user", "stop", "clarp.service",
             check=False, runner=runner)
    _run(standing, "systemctl", "--user", "enable", "--now", "clarp-runtime.service",
         runner=runner)
    _run(standing, "systemctl", "--user", "enable", "clarp.service", runner=runner)
    _run(standing, "systemctl", "--user", "restart", "clarp.service", runner=runner)


def restart(
    *, runner: Runner = subprocess.run, check: bool = True, admin: bool = False,
) -> None:
    """Restart the Host. A Host restarts only the unit it runs in; clarp-admin
    passes ``admin=True``. Raises ServiceActionRefused, whatever ``check`` is."""
    standing = _standing("admin" if admin else "host", "restart", runner)
    if platform_kind() == "macos":
        domain = launchd_domain()
        result = _run(standing, 
            "launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}",
            check=False, runner=runner)
        if check and result.returncode != 0:
            _run(standing, "launchctl", "bootstrap", domain, str(definition_path()),
                 runner=runner)
        return
    _run(standing, "systemctl", "--user", "daemon-reload", check=False, runner=runner)
    _run(standing, "systemctl", "--user", "restart", "clarp.service", check=check,
         runner=runner)


def restore_after_failed_install(
    *, had_previous: bool, had_runtime_previous: bool = True,
    runner: Runner = subprocess.run,
) -> None:
    """Make the restored service definition authoritative after rollback.

    After a failed FIRST installation (no previous definition) call this while
    the new definitions still exist, then delete them: with no definition left
    nothing says these services are the caller's, and the call is refused."""
    standing = _standing("admin", "restore", runner)
    if platform_kind() == "macos":
        domain = launchd_domain()
        _run(standing, "launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}",
             check=False, runner=runner)
        if had_previous and definition_path().is_file():
            _run(standing, "launchctl", "bootstrap", domain, str(definition_path()),
                 runner=runner)
            _run(standing, "launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}",
                 runner=runner)
        if not had_runtime_previous:
            _run(standing, "launchctl", "bootout", domain,
                 str(runtime_definition_path()), check=False, runner=runner)
        return
    _run(standing, "systemctl", "--user", "daemon-reload", check=False, runner=runner)
    if had_previous:
        # A release that crash-looped hit systemd's start limit; a plain
        # restart is then refused and the Host stays down (OPS-N2).
        _run(standing, "systemctl", "--user", "reset-failed", "clarp.service",
             check=False, runner=runner)
        _run(standing, "systemctl", "--user", "restart", "clarp.service",
             check=False, runner=runner)
    else:
        _run(standing, "systemctl", "--user", "disable", "--now", "clarp.service",
             check=False, runner=runner)
    if not had_runtime_previous:
        _run(standing, "systemctl", "--user", "disable", "--now",
             "clarp-runtime.service", check=False, runner=runner)


def stop_and_disable(*, runner: Runner = subprocess.run) -> None:
    standing = _standing("admin", "stop", runner)
    if platform_kind() == "macos":
        _run(standing, "launchctl", "bootout", launchd_domain(), str(definition_path()),
             check=False, runner=runner)
        _run(standing, "launchctl", "bootout", launchd_domain(),
             str(runtime_definition_path()), check=False, runner=runner)
        return
    _run(standing, "systemctl", "--user", "disable", "--now", "clarp.service",
         check=False, runner=runner)
    _run(standing, "systemctl", "--user", "disable", "--now", "clarp-runtime.service",
         check=False, runner=runner)
    _run(standing, "systemctl", "--user", "daemon-reload", check=False, runner=runner)


def remove_definitions(*, runner: Runner = subprocess.run) -> None:
    """Delete both service definitions and make the service manager forget them.

    Standing is decided while the definitions still exist; with none left
    there is nothing to remove and nothing runs."""
    paths = (definition_path(), runtime_definition_path())
    if not any(path.is_file() for path in paths):
        return
    standing = _standing("admin", "remove", runner)
    for path in paths:
        path.unlink(missing_ok=True)
    if platform_kind() == "linux":
        _run(standing, "systemctl", "--user", "daemon-reload", check=False, runner=runner)


def is_active(*, runner: Runner = subprocess.run) -> bool:
    """Whether this installation's Host service runs. False, with the refusal
    logged, for a process that is not of the installation: it has no service,
    and the units it would ask about are another Host's."""
    try:
        standing = _standing("member", "status")
    except ServiceActionRefused:
        return False
    if platform_kind() == "macos":
        result = _run(standing, "launchctl", "print",
                      f"{launchd_domain()}/{LAUNCHD_LABEL}", check=False,
                      runner=runner)
    else:
        result = _run(standing, "systemctl", "--user", "is-active", "--quiet",
                      "clarp.service", check=False, runner=runner)
    return result.returncode == 0


def is_runtime_active(*, runner: Runner = subprocess.run) -> bool:
    """Whether this installation's runtime service runs. False, with the refusal
    logged, for a process that is not of the installation: it has no service,
    and the units it would ask about are another Host's."""
    try:
        standing = _standing("member", "runtime-status")
    except ServiceActionRefused:
        return False
    if platform_kind() == "macos":
        result = _run(standing, 
            "launchctl", "print", f"{launchd_domain()}/{RUNTIME_LAUNCHD_LABEL}",
            check=False, runner=runner)
    else:
        result = _run(standing, 
            "systemctl", "--user", "is-active", "--quiet",
            "clarp-runtime.service", check=False, runner=runner)
    return result.returncode == 0


def wait_until_ready(
    config_path: Path, *, expected_release_id: str, timeout: float = 20.0,
) -> None:
    """Wait for the newly activated server to answer its authenticated status."""
    _standing("installation", "await")
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
    properties: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Launch work independently of the server service on Linux or macOS.

    `properties` are extra systemd unit properties (e.g. ``Restart=on-failure``);
    the fallback without systemd has no supervisor and ignores them."""
    if (platform_kind() == "linux"
            and os.environ.get("CLARP_DEPLOYMENT_MODE") != "container"):
        # A transient unit runs in the user's service manager with the
        # manager's environment and outlives its launcher: only a process of
        # the installation starts one. Refused work is reported as not launched.
        try:
            standing = _standing("member", "launch")
        except ServiceActionRefused as refusal:
            return False, str(refusal)
        args = ["systemd-run", "--user", "--collect", f"--unit={unit}"]
        if delay_seconds:
            # OnActiveSec is rebased by daemon-reload on some systemd versions.
            # Freeze the wall-clock deadline when scheduling, independent of TZ.
            due = math.ceil(time.time() + int(delay_seconds))
            deadline = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(due))
            args.extend([f"--on-calendar={deadline}", "--timer-property=AccuracySec=1s"])
        else:
            args.append("--property=Type=exec")
        args.extend(f"--property={p}" for p in properties)
        if environment:
            for name, value in sorted(environment.items()):
                args.append(f"--setenv={name}={value}")
        result = _run(standing, *args, *command, check=False, runner=runner)
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
