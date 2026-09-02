"""Shared pytest fixtures.

The big one: every test gets a fresh, isolated SQLite DB so production
state at ~/.local/share/clarp/state.sqlite is never touched and tests
don't see one another's agents.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import pytest


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
                "CLAUDE_PWA_HEARTBEAT_ACTIVE_HOURS"):
        monkeypatch.delenv(var, raising=False)
    try:
        from lib import config as _config
        _config._CACHED = None
    except ImportError:
        pass
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
