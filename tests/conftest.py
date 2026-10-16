"""Shared pytest fixtures.

The big one: every test gets a fresh, isolated SQLite DB so production
state at ~/.local/share/clarp/state.sqlite is never touched and tests
don't see one another's agents.
"""
from __future__ import annotations

import inspect
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import pytest


def default_temp_root(environ, *, var_tmp_exists: bool) -> str | None:
    """Where pytest's per-run temp folders go when nobody chose.

    /tmp is a small RAM-backed tmpfs with a per-user quota; pytest keeps its
    last three run folders, each up to several gigabytes, which filled it and
    broke installs and other agents' shells. An explicit TMPDIR or
    PYTEST_DEBUG_TEMPROOT always wins.
    """
    if environ.get("TMPDIR") or environ.get("PYTEST_DEBUG_TEMPROOT") or not var_tmp_exists:
        return None
    return "/var/tmp"


_TEMP_ROOT = default_temp_root(os.environ, var_tmp_exists=os.path.isdir("/var/tmp"))
if _TEMP_ROOT:
    # Read lazily by pytest's tmp_path factory, after initial conftests load.
    os.environ["PYTEST_DEBUG_TEMPROOT"] = _TEMP_ROOT


# Match the installed layout: server/lib is imported as the top-level `lib`
# package. Centralizing this keeps test collection independent of file order.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))


@pytest.fixture(scope="session")
def _host_service_command_shims(tmp_path_factory):
    """Create service-manager shims outside every test's own tmp_path."""
    if os.name == "nt":
        return None
    command_dir = tmp_path_factory.mktemp("host-command-shims")
    systemctl = command_dir / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "case \" $* \" in\n"
        "  *\" is-active \"*|*\" is-enabled \"*) exit 0 ;;\n"
        "  *) echo 'test blocked live systemctl mutation' >&2; exit 97 ;;\n"
        "esac\n"
    )
    systemctl.chmod(0o755)
    launchctl = command_dir / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "echo 'test blocked live launchctl mutation' >&2\n"
        "exit 97\n"
    )
    launchctl.chmod(0o755)
    return command_dir


@pytest.fixture(autouse=True)
def _isolate_host_service_commands(_host_service_command_shims, monkeypatch):
    """Put non-mutating service-manager shims ahead of host commands.

    This also protects subprocess-based CLI and TUI tests, which run outside
    the in-process monkeypatch below. Installer tests that need richer behavior
    explicitly prepend their own disposable command directory.
    """
    command_dir = _host_service_command_shims
    if command_dir is None:
        return
    monkeypatch.setenv(
        "PATH", f"{command_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    )


@pytest.fixture(autouse=True)
def _forbid_live_service_control(monkeypatch):
    """Fail before a test can mutate the host service manager."""
    from lib import service_manager

    original_run = service_manager._run
    live_runner = subprocess.run

    def guarded_run(*args, check=True, runner=live_runner):
        if runner is live_runner:
            command = " ".join(str(arg) for arg in args)
            raise AssertionError(
                f"test attempted live service-manager command: {command}"
            )
        return original_run(*args, check=check, runner=runner)

    monkeypatch.setattr(service_manager, "_run", guarded_run)


@pytest.fixture(autouse=True)
def _no_real_agy_cli(monkeypatch, tmp_path):
    """Never run, or read the state of, the developer's real Antigravity CLI from
    a test; a test that needs agy installs a fake via CLARP_AGY_BIN and its own
    store via CLAUDE_PWA_AGY_HOME."""
    monkeypatch.setenv("CLARP_AGY_BIN", "clarp-test-no-agy")
    monkeypatch.setenv("CLAUDE_PWA_AGY_HOME", str(tmp_path / "no-agy-home"))


# ---- real provider CLIs are never launched from a test --------------------------
#
# A unit test once ran a real Claude Code turn through a handler that built its
# own dispatcher on the real backend registry. Every exec primitive is
# intercepted for every test: a launch of a provider CLI (by name, through a
# wrapper, by its real installed path or under the Clarp toolchains) is refused
# and RECORDED, and the test fails at teardown even when the application
# swallowed the refusal. A deterministic fake runs only after the test
# registered it with ``fake_backend_cli(path)``. Name shims on PATH record a
# child process that resolves one of these names, too.

BACKEND_CLIS = frozenset({"claude", "clarp", "codex", "agy", "grok", "opencode", "gemini", "tmux"})
_REAL_HOME = pathlib.Path(os.path.expanduser("~")).resolve()
_REAL_PATH = os.environ.get("PATH", "")
_TOOLCHAINS = os.path.realpath(_REAL_HOME / ".local" / "share" / "clarp" / "toolchains")
_REPO = pathlib.Path(__file__).resolve().parents[1]
_WRAPPERS = frozenset({"env", "timeout", "nohup", "setsid", "stdbuf", "nice", "ionice", "sudo",
                       "exec", "command", "unbuffer", "xargs", "script"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish"})
_RUNNERS = frozenset({"node", "npx", "bunx", "bun", "uv", "uvx", "pnpm", "yarn", "deno"})
_POPEN_INIT = subprocess.Popen.__init__
_POPEN_SIGNATURE = inspect.signature(_POPEN_INIT)
_EXEC_NAMES = ("execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe")


class BackendLaunchBlocked(OSError):
    """A test reached a real provider CLI launch."""


def _under(path, root) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _ambient_cli_paths() -> frozenset[str]:
    found = set()
    for name in BACKEND_CLIS:
        hit = shutil.which(name, path=_REAL_PATH)
        if hit:
            found.add(os.path.realpath(hit))
    if os.path.isdir(_TOOLCHAINS):
        for entry in pathlib.Path(_TOOLCHAINS).glob("*/bin/*"):
            found.add(os.path.realpath(entry))
    return frozenset(found)


AMBIENT_CLI_PATHS = _ambient_cli_paths()


class _BackendCliGuard:
    def __init__(self):
        self.reset(None)

    def reset(self, log):
        self.attempts: list[str] = []
        self.allowed: set[str] = set()
        self.allowed_dirs: set[str] = set()
        self.log = log
        self.pid = os.getpid()
        self.ambient = AMBIENT_CLI_PATHS
        self.toolchains = _TOOLCHAINS

    def take(self) -> list[str]:
        """Every refused launch so far (in-process and from child processes),
        cleared: a test that provokes refusals on purpose acknowledges them."""
        taken = list(self.attempts)
        self.attempts.clear()
        if self.log is not None and os.path.exists(self.log):
            taken += [line for line in pathlib.Path(self.log).read_text().splitlines() if line]
            os.unlink(self.log)
        return taken


BACKEND_CLI_GUARD = _BackendCliGuard()
BACKEND_CLI_GUARD.install = lambda mp: install_backend_cli_guard(mp)


def _forbidden_program(program, env_path) -> bool:
    program = str(program)
    name = os.path.basename(program).lower()
    for ext in (".exe", ".cmd", ".bat"):
        if name.endswith(ext):
            name = name[:-len(ext)]
    if os.sep in program:
        target = os.path.realpath(program)
    else:
        hit = shutil.which(program, path=env_path)
        target = os.path.realpath(hit) if hit else None
    if target is not None and target in BACKEND_CLI_GUARD.allowed:
        return False
    if (target is not None and os.path.isfile(target)
            and any(_under(target, root) for root in BACKEND_CLI_GUARD.allowed_dirs)
            and target not in BACKEND_CLI_GUARD.ambient
            and not _under(target, BACKEND_CLI_GUARD.toolchains)):
        return False
    if name in BACKEND_CLIS:
        return True
    return target is not None and (target in BACKEND_CLI_GUARD.ambient
                                   or _under(target, BACKEND_CLI_GUARD.toolchains))


def _forbidden_argv(argv, env_path) -> str:
    """The provider CLI ``argv`` would run (through wrappers, shells and script
    runners), or ""."""
    argv = [str(a) for a in argv]
    i = 0
    while i < len(argv):
        program = argv[i]
        name = os.path.basename(program).lower()
        if _forbidden_program(program, env_path):
            return program
        if name in _SHELLS:
            rest = argv[i + 1:]
            if "-c" in rest and rest.index("-c") + 1 < len(rest):
                return _forbidden_shell(rest[rest.index("-c") + 1], env_path)
            return ""
        if name in _RUNNERS:
            return next((a for a in argv[i + 1:] if not a.startswith("-")
                         and _forbidden_program(a, env_path)), "")
        if name not in _WRAPPERS:
            return ""
        i += 1
        while i < len(argv) and (argv[i].startswith("-") or "=" in argv[i]
                                 or (name == "timeout" and argv[i][:1].isdigit())):
            i += 1
    return ""


def _forbidden_shell(command, env_path) -> str:
    try:
        tokens = shlex.split(str(command))
    except ValueError:
        tokens = str(command).split()
    for token in tokens:
        if _forbidden_program(token, env_path):
            return token
    return ""


def _refuse(what: str) -> None:
    entry = f"{what}"
    if os.getpid() != BACKEND_CLI_GUARD.pid:
        # A forked child (pty.fork + exec): tell the parent through the log.
        if BACKEND_CLI_GUARD.log is not None:
            with open(BACKEND_CLI_GUARD.log, "a") as handle:
                handle.write(entry + "\n")
        os._exit(127)
    BACKEND_CLI_GUARD.attempts.append(entry)
    raise BackendLaunchBlocked(f"test attempted to launch a real provider CLI: {entry}")


def _guarded_popen_init(self, *args, **kwargs):
    bound = _POPEN_SIGNATURE.bind(self, *args, **kwargs)
    call = bound.arguments
    command = call.get("args")
    env = call.get("env")
    env_path = (env or {}).get("PATH", os.environ.get("PATH", "")) if env is not None \
        else os.environ.get("PATH", "")
    if call.get("shell"):
        hit = _forbidden_shell(command if isinstance(command, str) else " ".join(map(str, command)), env_path)
    else:
        argv = [command] if isinstance(command, (str, bytes, os.PathLike)) else list(command or [])
        if call.get("executable"):
            argv = [call["executable"], *argv[1:]]
        hit = _forbidden_argv(argv, env_path)
    if hit:
        _refuse(f"Popen {command!r}")
    return _POPEN_INIT(self, *args, **kwargs)


def _guarded_exec(name, original):
    def guarded(path, *rest):
        argv = rest[0] if name in ("execv", "execve", "execvp", "execvpe") and rest else [path, *rest]
        env_path = os.environ.get("PATH", "")
        if _forbidden_program(path, env_path) or _forbidden_argv(list(argv) or [path], env_path):
            _refuse(f"os.{name} {path!r}")
        return original(path, *rest)
    return guarded


def _guarded_spawn(name, original):
    def guarded(path, argv, *rest, **kwargs):
        if _forbidden_program(path, os.environ.get("PATH", "")) or \
                _forbidden_argv(list(argv) or [path], os.environ.get("PATH", "")):
            _refuse(f"os.{name} {path!r}")
        return original(path, argv, *rest, **kwargs)
    return guarded


def _guarded_system(original):
    def guarded(command):
        if _forbidden_shell(command, os.environ.get("PATH", "")):
            _refuse(f"os.system {command!r}")
        return original(command)
    return guarded


def install_backend_cli_guard(mp: pytest.MonkeyPatch) -> None:
    """Patch every exec primitive through ``mp`` (undone with it)."""
    mp.setattr(subprocess.Popen, "__init__", _guarded_popen_init)
    for name in _EXEC_NAMES:
        if hasattr(os, name):
            mp.setattr(os, name, _guarded_exec(name, getattr(os, name)))
    for name in ("posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            mp.setattr(os, name, _guarded_spawn(name, getattr(os, name)))
    mp.setattr(os, "system", _guarded_system(os.system))


@pytest.fixture(scope="session")
def _backend_cli_shims(tmp_path_factory):
    """Executables named after every provider CLI, first on PATH: a child
    process that resolves one by name records the attempt and exits 127."""
    shims = tmp_path_factory.mktemp("backend-cli-shims")
    for name in BACKEND_CLIS:
        shim = shims / name
        shim.write_text(
            "#!/bin/sh\n"
            'if [ -n "$CLARP_TEST_BACKEND_CLI_LOG" ]; then\n'
            f'  echo "child exec {name} $*" >> "$CLARP_TEST_BACKEND_CLI_LOG"\n'
            "fi\n"
            f"echo 'test blocked a real {name} launch' >&2\n"
            "exit 127\n")
        shim.chmod(0o755)
    return shims


@pytest.fixture(autouse=True)
def _guard_backend_clis(_backend_cli_shims, tmp_path):
    """Intercept every provider CLI launch for this test; fail it at teardown
    if any was attempted, even when the application swallowed the refusal."""
    log = tmp_path.parent / f"{tmp_path.name}-backend-cli-guard.log"
    mp = pytest.MonkeyPatch()
    BACKEND_CLI_GUARD.reset(str(log))
    mp.setenv("CLARP_TEST_BACKEND_CLI_LOG", str(log))
    mp.setenv("PATH", f"{_backend_cli_shims}{os.pathsep}{os.environ.get('PATH', '')}")
    install_backend_cli_guard(mp)
    try:
        yield BACKEND_CLI_GUARD
    finally:
        attempts = BACKEND_CLI_GUARD.take()
        mp.undo()
        BACKEND_CLI_GUARD.reset(None)
    if attempts:
        pytest.fail("real provider CLI launch attempted (blocked):\n  " + "\n  ".join(attempts),
                    pytrace=False)


@pytest.fixture
def backend_cli_guard(_guard_backend_clis):
    """The active guard: ``take()`` acknowledges refusals a test provoked on purpose."""
    return _guard_backend_clis


@pytest.fixture
def fake_backend_cli(_guard_backend_clis, tmp_path):
    """Register a deterministic fake CLI this test may run. Only a file inside
    this test's tmp_path or the repo's tests/ directory, never one the user's
    PATH or the Clarp toolchains resolve."""
    roots = (os.path.realpath(tmp_path), os.path.realpath(_REPO / "tests"))

    def register(path):
        path = pathlib.Path(path)
        real = os.path.realpath(path)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"not an executable file: {path}")
        if not any(_under(real, root) for root in roots):
            raise ValueError(f"not a disposable fake (outside tmp_path and tests/): {path}")
        if real in AMBIENT_CLI_PATHS or _under(real, _TOOLCHAINS):
            raise ValueError(f"an installed provider CLI, not a fake: {path}")
        ambient = shutil.which(path.name, path=_REAL_PATH)
        if ambient and os.path.realpath(ambient) == real:
            raise ValueError(f"resolvable on the user's PATH: {path}")
        BACKEND_CLI_GUARD.allowed.add(real)
        return path

    def register_dir(path):
        """Every executable file a test writes under ``path`` (inside its
        tmp_path or tests/), each still checked when it is launched."""
        real = os.path.realpath(path)
        if not any(_under(real, root) for root in roots):
            raise ValueError(f"not a disposable fake directory (outside tmp_path and tests/): {path}")
        if _under(real, _TOOLCHAINS) or any(_under(cli, real) for cli in AMBIENT_CLI_PATHS):
            raise ValueError(f"holds an installed provider CLI: {path}")
        BACKEND_CLI_GUARD.allowed_dirs.add(real)
        return pathlib.Path(path)
    register.dir = register_dir
    return register


@pytest.fixture(autouse=True)
def _isolate_agy_skills(monkeypatch, tmp_path):
    """Skill links for Antigravity default to ~/.gemini/config/skills, which
    tests do not fake via HOME. Point it at a temp path whose parent does not
    exist, so agy linking stays off unless a test turns it on."""
    target = tmp_path / "no-agy" / "config" / "skills"
    monkeypatch.setenv("CLARP_AGY_SKILLS", str(target))
    for module in ("lib.managed_skills", "lib.personal_skills"):
        loaded = __import__("sys").modules.get(module)
        if loaded is not None:
            monkeypatch.setattr(loaded, "AGY_SKILLS", target)


@pytest.fixture(autouse=True)
def _isolate_xdg(monkeypatch):
    """Clear the XDG base-dir variables for every test.

    Clarp honours $XDG_CONFIG_HOME / $XDG_DATA_HOME / $XDG_CACHE_HOME, and most
    desktops set all three (Hyprland, GNOME, KDE). Without this, a test that
    fakes $HOME still resolves to the developer's real directories, and a
    subprocess test would install into them. The tests that exercise XDG
    behaviour set these explicitly.
    """
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point the DB module at a tmp-path-scoped SQLite file for each test
    and clear the thread-local connection cache.

    Reset the cached connection so the tmp-path-scoped DB always wins.
    """
    db_path = tmp_path / "state.sqlite"
    log_dir = tmp_path.parent / f"{tmp_path.name}-logs"
    monkeypatch.setenv("CLAUDE_PWA_DB", str(db_path))
    monkeypatch.setenv("CLAUDE_PWA_LOG_DIR", str(log_dir))
    monkeypatch.setenv("CLARP_CONFIG_DIR", str(tmp_path / "clarp-config"))
    monkeypatch.setenv("CLARP_SHARE_DIR", str(tmp_path / "clarp-share"))
    monkeypatch.setenv("CLARP_CACHE_DIR", str(tmp_path / "clarp-cache"))
    # Isolate config: tests must never read the developer's real
    # ~/.config/clarp/config.toml (it holds live API keys, which would
    # make the TTS worker hit the network). Point at a non-existent file so
    # load() falls back to safe defaults, and drop any provider keys/overrides
    # leaking in from the environment.
    monkeypatch.setenv("CLAUDE_PWA_CONFIG", str(tmp_path / "no-such-config.toml"))
    for var in ("CARTESIA_API_KEY", "ELEVEN_API_KEY",
                "CLAUDE_PWA_TTS_PROVIDER", "CLAUDE_PWA_DELIVERY",
                "CLAUDE_PWA_CLAUDE_CLI",
                "CLAUDE_PWA_HEARTBEAT_INTERVAL_SEC",
                "CLAUDE_PWA_HEARTBEAT_BACKOFF_CAP_SEC",
                "CLAUDE_PWA_HEARTBEAT_DORMANT_AFTER_NOOPS",
                "CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC",
                "CLAUDE_PWA_HEARTBEAT_ACTIVE_HOURS",
                # Set by the installed Host's units, so an agent running the
                # suite inherits them: they pause heartbeats, bind hooks to
                # the caller's session and point scripts at the live tree.
                "CLARP_HEARTBEATS_DISABLED", "CLAUDE_PWA_SESSION",
                "CLARP_CODE_ROOT",
                # Set inside a Clarp-run Claude turn and its background
                # commands: they would mark every hook in the suite as
                # belonging to the caller's live turn.
                "CLARP_PROVIDER_TURN", "CLARP_BACKGROUND_ORIGIN"):
        monkeypatch.delenv(var, raising=False)
    try:
        from lib import config as _config
        _config.reset_cache()
    except ImportError:
        pass
    for mod_name in ("lib.db",):
        try:
            mod = __import__(mod_name, fromlist=["reset_for_tests"])
            mod.reset_for_tests(db_path)
        except (ImportError, AttributeError):
            pass
    # Every RevisionedCache (pair-conversation list, dashboard previews, ...)
    # registers itself; one call empties them all so no test inherits a
    # projection computed against the previous test's database.
    from lib import revisioned_cache as _revisioned_cache
    _revisioned_cache.reset_all()
    try:
        from lib import telemetry as _telemetry
        _telemetry.reset_for_tests(tmp_path / "telemetry.sqlite")
    except (ImportError, AttributeError):
        pass
    try:
        from lib import heartbeat as _heartbeat
        _heartbeat.reset_for_tests()
    except (ImportError, AttributeError):
        pass
    try:
        from lib import dreaming as _dreaming
        _dreaming.reset_for_tests()
    except (ImportError, AttributeError):
        pass
    try:
        from lib import transcription_models as _transcription_models
        monkeypatch.setattr(
            _transcription_models, "REGISTRY",
            tmp_path / "clarp-share/transcription-models.json")
        monkeypatch.setattr(
            _transcription_models, "MANAGED_MODELS",
            tmp_path / "clarp-share/models")
        with _transcription_models._lock:
            _transcription_models._tasks.clear()
        with _transcription_models._activation_lock:
            _transcription_models._activation_inflight.clear()
            _transcription_models._activation_completed.clear()
            _transcription_models._activation_exhausted.clear()
            _transcription_models._activation_retry_after.clear()
            _transcription_models._activation_attempts.clear()
    except (ImportError, AttributeError):
        pass
    for mod_name in ("lib.eventlog",):
        try:
            mod = __import__(mod_name, fromlist=["reset_for_tests"])
            mod.reset_for_tests(log_dir)
        except (ImportError, AttributeError):
            pass
    try:
        from lib import diagnostics_settings as _diagnostics_settings
        _diagnostics_settings.reset_for_tests()
        with _diagnostics_settings._LOCK:
            _diagnostics_settings._CACHED = _diagnostics_settings.Settings(
                enabled=True,
                categories=frozenset(_diagnostics_settings.CATEGORIES))
            _diagnostics_settings._CACHED_AT = (
                _diagnostics_settings.time.monotonic())
    except (ImportError, AttributeError):
        pass
    yield
    from lib import transcript_import_cache
    assert transcript_import_cache.wait_for_background(5), "transcript importer leaked across tests"
    transcript_import_cache.reset_for_tests()
    _revisioned_cache.reset_all()
    for mod_name in ("lib.db",):
        try:
            mod = __import__(mod_name, fromlist=["reset_for_tests"])
            mod.reset_for_tests(db_path)
        except (ImportError, AttributeError):
            pass
    try:
        from lib import telemetry as _telemetry
        _telemetry.reset_for_tests(tmp_path / "telemetry.sqlite")
    except (ImportError, AttributeError):
        pass
    try:
        from lib import heartbeat as _heartbeat
        _heartbeat.reset_for_tests()
    except (ImportError, AttributeError):
        pass
    try:
        from lib import dreaming as _dreaming
        _dreaming.reset_for_tests()
    except (ImportError, AttributeError):
        pass
    for mod_name in ("lib.eventlog",):
        try:
            mod = __import__(mod_name, fromlist=["reset_for_tests"])
            mod.reset_for_tests(log_dir)
        except (ImportError, AttributeError):
            pass


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch):
    """Fail fast if a test reaches past loopback.

    Provider usage, voice catalogs, and update checks all talk to the internet
    in production. A test that gets there by accident is slow, flaky, and
    leaks the developer's credentials into a fixture; refuse the connection
    instead so the missing stub is obvious.
    """
    import socket

    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in {
            "127.0.0.1", "::1", "localhost", "0.0.0.0", "::",
        } and not host.startswith("127."):
            raise AssertionError(
                f"test attempted a network connection to {host!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def _seed_agents_via_db(rows: dict[str, dict]) -> None:
    """Helper: write `{session: {name, voice_id, cwd}}` into the DB."""
    from lib.agents import create_agent
    for session, info in rows.items():
        create_agent(
            persona=str(info.get("name") or session),
            voice_id=str(info.get("voice_id") or ""),
            cwd=str(info.get("cwd") or "/tmp"),
            session=session,
        )


# Expose the helper for test files to use.
@pytest.fixture
def seed_agents():
    return _seed_agents_via_db
