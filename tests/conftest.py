"""Shared pytest fixtures.

The big one: every test gets a fresh, isolated SQLite DB so production
state at ~/.local/share/clarp/state.sqlite is never touched and tests
don't see one another's agents.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading

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


# ---- real provider CLIs, the network and the real HOME are never reached from a test ----
#
# A unit test once ran a real Claude Code turn through a handler that built its
# own dispatcher on the real backend registry. The guard (tests/
# backend_cli_guard_core.py) is installed ONCE for the whole session, before
# any lib import, and never undone: every exec primitive and the socket entry
# points refuse a provider CLI (by name, through a wrapper, shell or runner, by
# its real installed path, a copy of it, or under the Clarp toolchains) and any
# non-loopback address, and RECORD the attempt. Inside a test the test fails
# at teardown, even when the application swallowed the refusal; outside any
# test (a retry timer after the last test, a session fixture) the session
# fails. Child Python processes install the same guard through a
# ``sitecustomize`` on the session's PYTHONPATH; name shims first on PATH
# record any other child that resolves a provider CLI by name. HOME, the
# custom TTS/STT adapter directories and the toolchain mode point at empty
# session directories. A deterministic fake runs only after the test
# registered it with ``fake_backend_cli(path)``.

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR))
try:
    import backend_cli_guard_core as _guard_core  # noqa: E402  (stdlib only; shared with child processes)
finally:
    del sys.path[0]

BACKEND_CLIS = _guard_core.BACKEND_CLIS
BackendLaunchBlocked = _guard_core.BackendLaunchBlocked
BACKEND_CLI_GUARD = _guard_core.GUARD
AMBIENT_CLI_PATHS = BACKEND_CLI_GUARD.ambient
_TOOLCHAINS = BACKEND_CLI_GUARD.toolchains
_REPO = pathlib.Path(__file__).resolve().parents[1]
_GITCONFIG = ("[user]\n\tname = Clarp Test\n\temail = test@clarp.invalid\n"
              "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")


def _write_backend_cli_shims(shims: pathlib.Path) -> None:
    """Executables named after every provider CLI, first on PATH: a child
    process that resolves one by name records the attempt and exits 127."""
    shims.mkdir(exist_ok=True)
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


def _prepend(var: str, entry: str) -> None:
    current = os.environ.get(var, "")
    if entry not in current.split(os.pathsep):
        os.environ[var] = entry + (os.pathsep + current if current else "")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "probes_provider_clis: the test exercises provider CLI resolution itself, "
                   "so the model-catalog warm-up stub stays off")
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    shared = os.environ.get("CLARP_TEST_GUARD_DIR")
    home, site = None, None
    if worker and shared and os.path.isdir(shared):
        # An xdist worker reuses the controller's session directories as they
        # are: another worker may already be executing the shims.
        root = pathlib.Path(shared)
        home, site = root / "home", root / "guard-site"
    else:
        root = pathlib.Path(tempfile.mkdtemp(prefix="clarp-test-guard-", dir=_TEMP_ROOT))
        os.environ["CLARP_TEST_GUARD_DIR"] = str(root)
        # What the guard recognises as the real installation (read before HOME moves).
        os.environ.setdefault("CLARP_TEST_REAL_HOME", str(pathlib.Path(os.path.expanduser("~")).resolve()))
        os.environ.setdefault("CLARP_TEST_REAL_PATH", os.environ.get("PATH", ""))
        home = root / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(_GITCONFIG)
        for name in ("tts-adapters.d", "stt-adapters.d"):
            (root / name).mkdir()
        _write_backend_cli_shims(root / "backend-cli-shims")
        site = root / "guard-site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(_guard_core.SITECUSTOMIZE.format(
            tests_dir=str(_TESTS_DIR), fallback_log=str(root / "outside-children.log")))
    session_log = root / f"outside-{worker or 'main'}-{os.getpid()}.log"
    os.environ.update({
        "HOME": str(home),
        "CLARP_TTS_ADAPTERS_DIR": str(root / "tts-adapters.d"),
        "CLARP_STT_ADAPTERS_DIR": str(root / "stt-adapters.d"),
        "CLARP_TOOLCHAIN_MODE": "none",
        "CLARP_TEST_BACKEND_CLI_LOG": str(session_log),
    })
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "GNUPGHOME"):
        os.environ.pop(var, None)
    _prepend("PATH", str(root / "backend-cli-shims"))
    _prepend("PYTHONPATH", str(site))
    _guard_core.install(child=False)
    BACKEND_CLI_GUARD.log = str(session_log)
    config._clarp_guard = {"root": root, "session_log": str(session_log), "worker": worker}


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Fail the session for an attempt no test owned (after the session fixtures
    were torn down, which run before this hook)."""
    state = getattr(session.config, "_clarp_guard", None)
    if state is None:
        return
    for timer in list(BACKEND_CLI_GUARD.timers):
        timer.cancel()
    outside = list(BACKEND_CLI_GUARD.outside) + list(BACKEND_CLI_GUARD.attempts)
    if state["worker"]:
        if outside:                                  # the controller reports it
            with open(state["session_log"], "a") as handle:
                handle.write("".join(entry + "\n" for entry in outside))
        return
    for log in sorted(pathlib.Path(state["root"]).glob("outside-*.log")):
        outside += [line for line in log.read_text().splitlines() if line]
    state["outside"] = outside
    if outside:
        sys.stderr.write("\nreal provider CLI launch or network access attempted outside any test "
                         "(blocked):\n  " + "\n  ".join(outside) + "\n")
        if session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED   # never mask an interrupt or internal error


def pytest_unconfigure(config):
    """Remove the session's guard directories after a clean session; keep them
    as evidence when anything was attempted outside a test."""
    state = getattr(config, "_clarp_guard", None)
    if state is None or state["worker"] or state.get("outside") or "outside" not in state:
        return

    def report(function, path, error):
        error = error[1] if isinstance(error, tuple) else error
        sys.stderr.write(f"guard session dir cleanup: {function.__name__} {path}: {error!r}\n")
    if sys.version_info >= (3, 12):
        shutil.rmtree(state["root"], onexc=report)
    else:
        shutil.rmtree(state["root"], onerror=report)


def _track_retry_timers() -> None:
    """The dispatcher's default retry timer never outlives the test that armed it."""
    turn_dispatch = sys.modules.get("lib.turn_dispatch")
    if turn_dispatch is None or getattr(turn_dispatch._default_retry_scheduler, "__clarp_guard__", False):
        return

    def tracked(delay, fn):
        timer = None

        def run():
            BACKEND_CLI_GUARD.timers.discard(timer)
            fn()
        timer = threading.Timer(delay, run)
        timer.daemon = True
        BACKEND_CLI_GUARD.timers.add(timer)
        timer.start()
    tracked.__clarp_guard__ = True
    tracked.__doc__ = turn_dispatch._default_retry_scheduler.__doc__
    turn_dispatch._default_retry_scheduler = tracked


@pytest.fixture(autouse=True)
def _guard_backend_clis(request, tmp_path):
    """Attribute every refused launch to this test; fail it at teardown if any
    was attempted, even when the application swallowed the refusal."""
    state = request.config._clarp_guard
    log = tmp_path.parent / f"{tmp_path.name}-backend-cli-guard.log"
    mp = pytest.MonkeyPatch()
    mp.setenv("CLARP_TEST_BACKEND_CLI_LOG", str(log))
    BACKEND_CLI_GUARD.begin(request.node.nodeid, str(log))
    _track_retry_timers()
    try:
        yield BACKEND_CLI_GUARD
    finally:
        for timer in list(BACKEND_CLI_GUARD.timers):
            timer.cancel()
        BACKEND_CLI_GUARD.timers.clear()
        mp.undo()
        attempts = BACKEND_CLI_GUARD.end(state["session_log"])
    if attempts:
        pytest.fail("real provider CLI launch or network access attempted (blocked):\n  "
                    + "\n  ".join(attempts), pytrace=False)


@pytest.fixture
def backend_cli_guard(_guard_backend_clis):
    """The active guard: ``take()`` acknowledges refusals a test provoked on purpose."""
    return _guard_backend_clis


@pytest.fixture
def fake_backend_cli(_guard_backend_clis, tmp_path):
    """Register a deterministic fake CLI this test may run. Only a file inside
    this test's tmp_path or the repo's tests/ directory, never one the user's
    PATH or the Clarp toolchains resolve, and never a copy of an installed CLI
    or a script that names one (checked again each time it is launched)."""
    roots = (os.path.realpath(tmp_path), os.path.realpath(_REPO / "tests"))
    guard = _guard_backend_clis

    def register(path):
        path = pathlib.Path(path)
        real = os.path.realpath(path)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"not an executable file: {path}")
        if not any(_guard_core._under(real, root) for root in roots):
            raise ValueError(f"not a disposable fake (outside tmp_path and tests/): {path}")
        if real in guard.ambient or _guard_core._under(real, guard.toolchains) or guard.is_ambient_copy(real):
            raise ValueError(f"an installed provider CLI (or a copy of one), not a fake: {path}")
        ambient = shutil.which(path.name, path=_guard_core._real_path())
        if ambient and os.path.realpath(ambient) == real:
            raise ValueError(f"resolvable on the user's PATH: {path}")
        guard.allow(real)
        return path

    def register_dir(path):
        """Every executable file a test writes under ``path`` (inside its
        tmp_path or tests/), each still checked when it is launched."""
        real = os.path.realpath(path)
        if not any(_guard_core._under(real, root) for root in roots):
            raise ValueError(f"not a disposable fake directory (outside tmp_path and tests/): {path}")
        if _guard_core._under(real, guard.toolchains) or any(_guard_core._under(cli, real) for cli in guard.ambient):
            raise ValueError(f"holds an installed provider CLI: {path}")
        guard.allow_dir(real)
        return pathlib.Path(path)
    register.dir = register_dir
    return register


@pytest.fixture(autouse=True)
def _model_catalog_never_probes_installed_clis(request, monkeypatch):
    """The server's cache warm-up builds the model catalog in the background by
    running each provider CLI it can resolve (``codex debug models``, ``agy
    models``). Tests resolve none, except the ones that test that resolution."""
    if request.node.get_closest_marker("probes_provider_clis"):
        return
    from lib import provider_capabilities
    monkeypatch.setattr(provider_capabilities, "_resolve_executable", lambda provider_id: None)


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
