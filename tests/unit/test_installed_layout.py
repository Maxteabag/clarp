"""The real entry points in an installed layout, and what follows a failed install.

From the review of PR 276. Nothing started the real ``server.py`` the way a
unit does: through ``<share>/server.py``, which is a link into
``<share>/current``, which is a link into ``<share>/releases/<id>``. The
line that decides whether a Host is this installation's own takes the
release's real directory; given the path the unit names, every installed
Host would be "not an installed release" and upgrade nothing.

And one sequence kept a Host down: a new release upgrades the database,
fails its health check, the installer puts the previous release back, and
that release meets a database above its schema and refuses to start.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

from lib import db_schema, schema_guard
from tests.unit.test_clarp_admin import admin
from tests.unit.test_only_services_migrate import _database_at, _own_port, _snapshot

ROOT = pathlib.Path(__file__).resolve().parents[2]
CODE = db_schema._SCHEMA_VERSION
RELEASE = "abc1234-20261112150000-4242"


def _release_of_this_code(releases: pathlib.Path, name: str = RELEASE) -> pathlib.Path:
    """This checkout's server, as install.sh lays a release out."""
    release = releases / name
    release.mkdir(parents=True)
    shutil.copy2(ROOT / "server/server.py", release / "server.py")
    shutil.copy2(ROOT / "server/runtime.py", release / "runtime.py")
    shutil.copytree(ROOT / "server/lib", release / "lib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (release / "INSTALL_OK").write_text("")
    return release


def _installed(root: pathlib.Path, *, database_version: int, releases=None):
    """An installation as ours are: ``releases/<id>``, ``current`` pointing at
    it, and ``share/server.py``, ``runtime.py`` and ``lib`` through ``current``."""
    home = root / "home"
    share = home / ".local/share/clarp"
    share.mkdir(parents=True)
    releases = releases or share / "releases"
    release = _release_of_this_code(releases)
    (share / "current").symlink_to(release, target_is_directory=True)
    for item in ("server.py", "runtime.py", "lib"):
        (share / item).symlink_to(share / "current" / item)
    database = _database_at(share / "state.sqlite", database_version)
    return home, share, release, database


def _environment(home: pathlib.Path, share: pathlib.Path, database: pathlib.Path) -> dict:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CLAUDE_PWA_", "CLARP_", "XDG_", "PYTEST"))}
    config = home / "config"
    _own_port(config / "config.toml")
    env.update(
        HOME=str(home), CLAUDE_PWA_DB=str(database), CLAUDE_PWA_LOG_DIR=str(home / "logs"),
        CLAUDE_PWA_CONFIG=str(config / "config.toml"), CLARP_CONFIG_DIR=str(config),
        CLARP_SHARE_DIR=str(share), CLARP_CACHE_DIR=str(home / "cache"),
        CLARP_TELEMETRY_DB=str(share / "telemetry.sqlite"),
        XDG_DATA_HOME=str(home / "xdg-data"), XDG_CACHE_HOME=str(home / "xdg-cache"),
        XDG_CONFIG_HOME=str(home / "xdg-config"), XDG_STATE_HOME=str(home / "xdg-state"))
    return env


def _start(entry: pathlib.Path, env: dict, until, *, limit: float = 150.0):
    """Start a service as its unit does and stop it once ``until()`` holds.
    Returns (exit code or None when it was still running, its stderr, and
    whether ``until()`` came to hold)."""
    errors = pathlib.Path(env["HOME"]) / f"stderr-{entry.name}-{time.monotonic_ns()}.txt"
    with errors.open("w") as stderr:
        service = subprocess.Popen([sys.executable, str(entry)], cwd=env["HOME"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=stderr)
        try:
            deadline = time.monotonic() + limit
            while service.poll() is None and time.monotonic() < deadline and not until():
                time.sleep(0.1)
        finally:
            code = service.poll()
            if code is None:
                service.terminate()
                try:
                    service.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    service.kill()
                    service.wait(timeout=30)
    return code, errors.read_text(), bool(until())


# --- recognised as the installation's own ----------------------------------------------

@pytest.mark.parametrize("entry", ["server.py", "current/runtime.py"])
def test_the_installed_services_are_recognised_through_their_links_and_upgrade(tmp_path, entry):
    """The Host is started as ``share/server.py`` and the runtime as
    ``share/current/runtime.py``, as the units start them, on a database one
    schema step behind."""
    home, share, _release, database = _installed(tmp_path, database_version=CODE - 1)

    code, said, upgraded = _start(share / entry, _environment(home, share, database),
                                  lambda: schema_guard.stored_version(database) == CODE)

    assert "is not an installed release" not in said, said[-1500:]
    assert upgraded and code != schema_guard.EXIT_REFUSED, said[-1500:]


@pytest.mark.parametrize("entry", ["server.py", "runtime.py"])
def test_a_release_of_another_installation_refuses_at_once_and_is_not_restarted(tmp_path, entry):
    """Its code is not under this installation's releases, and the database
    is below its schema. It says so and exits 78, which neither unit
    restarts; it used to wait ten seconds, fail with a traceback and exit 1,
    every twelve seconds."""
    home, share, _release, database = _installed(tmp_path, database_version=CODE - 1)
    stranger = _release_of_this_code(tmp_path / "another-installation/releases")
    before = _snapshot(database)
    began = time.monotonic()

    code, said, _ = _start(stranger / entry, _environment(home, share, database),
                           lambda: False, limit=60)

    assert code == schema_guard.EXIT_REFUSED == 78, said[-1500:]
    assert time.monotonic() - began < 9                   # not after the ten-second wait
    assert "is not an installed release of" in said
    assert f"is at schema {CODE - 1}, below this code's schema {CODE}" in said
    assert "Traceback" not in said
    assert _snapshot(database) == before


def test_a_release_of_another_installation_runs_on_a_database_at_its_schema(tmp_path):
    """It is told it is not this installation's and gets no grant, and that
    is all: with nothing to upgrade there is nothing to refuse."""
    home, share, _release, database = _installed(tmp_path, database_version=CODE)
    stranger = _release_of_this_code(tmp_path / "another-installation/releases")

    code, said, opened = _start(stranger / "server.py", _environment(home, share, database),
                                lambda: (share / "state.sqlite-shm").exists())

    assert opened and code != schema_guard.EXIT_REFUSED, f"it exited {code}: {said[-1500:]}"
    assert "is not an installed release of" in said and "below this code's schema" not in said
    assert schema_guard.stored_version(database) == CODE


def test_both_units_leave_a_refusing_service_down():
    for unit in ("clarp.service", "clarp-runtime.service"):
        text = (ROOT / "systemd" / unit).read_text()
        assert f"RestartPreventExitStatus={schema_guard.EXIT_REFUSED}\n" in text, unit
    host = (ROOT / "server/server.py").read_text()
    assert "raise SystemExit(_schema_guard.EXIT_REFUSED)" in host and "SystemExit(1)" not in host[
        host.index('\nif __name__ == "__main__":'):]
    assert "return schema_guard.EXIT_REFUSED" in (ROOT / "server/runtime.py").read_text()


def test_whose_release_it_is_goes_by_the_real_paths(tmp_path, monkeypatch):
    """Links on the way do not make an installation's own release a stranger,
    and do not make a stranger its own."""
    share = tmp_path / "home/share"
    share.mkdir(parents=True)
    elsewhere = tmp_path / "disk2/releases"
    release = elsewhere / RELEASE
    release.mkdir(parents=True)
    (share / "releases").symlink_to(elsewhere, target_is_directory=True)   # releases on another disk
    (share / "current").symlink_to(share / "releases" / RELEASE, target_is_directory=True)
    database = share / "state.sqlite"
    database.write_bytes(b"")
    checkout = tmp_path / "checkout/server"
    checkout.mkdir(parents=True)

    assert not schema_guard.foreign_to_installation(release, database, share)
    assert not schema_guard.foreign_to_installation(share / "current", database, share)
    assert schema_guard.foreign_to_installation(checkout, database, share)

    # The same database named through a link to the share directory is still
    # this installation's: a checkout on it is a stranger.
    alias = tmp_path / "alias"
    alias.symlink_to(share, target_is_directory=True)
    assert schema_guard.foreign_to_installation(checkout, alias / "state.sqlite", share)
    linked = tmp_path / "db-link.sqlite"
    linked.symlink_to(database)
    assert schema_guard.foreign_to_installation(checkout, linked, share)

    # A path that cannot be read is not shown to be the installation's own.
    real = pathlib.Path.resolve

    def unreadable(self, *args, **kwargs):
        if self == release:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "resolve", unreadable)
    assert schema_guard.foreign_to_installation(release, database, share)


# --- the stored version through a link ---------------------------------------------------

def test_a_database_that_is_a_link_is_read_where_its_log_is(tmp_path):
    """SQLite keeps the write-ahead log beside the real file. Looked for
    beside the link it was not found, and the header's older version was
    reported until a checkpoint (measured by the reviewer: 46 seconds)."""
    real = _database_at(tmp_path / "disk2/state.sqlite", CODE - 1)
    link = tmp_path / "share/state.sqlite"
    link.parent.mkdir()
    link.symlink_to(real)
    writer = sqlite3.connect(str(real))
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute(f"PRAGMA user_version = {CODE}")       # the Host's upgrade, still in the log
    writer.commit()
    assert (tmp_path / "disk2/state.sqlite-wal").stat().st_size > 0
    assert not (tmp_path / "share/state.sqlite-wal").exists()
    held = _snapshot(real)

    assert schema_guard.stored_version(link) == CODE
    assert _snapshot(real) == held                        # the file and what is beside it
    writer.close()


# --- after a failed install with a schema step --------------------------------------------

def _two_releases(tmp_path):
    """The installed release (this code) and a newer one that will fail."""
    home, share, old, database = _installed(tmp_path, database_version=CODE)
    new = share / "releases/def5678-20261113090000-77"
    (new / "lib").mkdir(parents=True)
    (new / "server.py").write_text("# a newer Host that does not come up")
    (new / "lib/db_schema.py").write_text(f"_SCHEMA_VERSION = {CODE + 1}\n")
    (new / "INSTALL_OK").write_text("")
    return home, share, old, new, database


def _the_new_host_upgrades(database):
    con = sqlite3.connect(str(database))
    con.execute(f"PRAGMA user_version = {CODE + 1}")
    con.commit()
    con.close()


def _the_restored_host_starts(home, share, database):
    """The real Host of the release that was put back: it has started when it
    has got past its check and opened the database."""
    def opened():
        return (share / "state.sqlite-shm").exists()

    code, said, started = _start(share / "server.py", _environment(home, share, database), opened)
    assert started and code != schema_guard.EXIT_REFUSED, (
        f"the restored Host exited {code}: {said[-1500:]}")
    assert "because a rollback was asked for" in said and "Refusing" not in said
    assert schema_guard.stored_version(database) == CODE + 1


def test_the_installer_puts_the_previous_release_back_and_it_starts(tmp_path):
    """install.sh's own restore, run as the installer runs it: the new
    release was activated, its Host upgraded the database, the health check
    failed. The release put back is this code, one schema step below the
    database now."""
    home, share, old, new, database = _two_releases(tmp_path)
    (share / "current").unlink()
    (share / "current").symlink_to(new, target_is_directory=True)      # activated
    _the_new_host_upgrades(database)
    # The only step of the restore that touches the service manager.
    (new / "lib/service_manager.py").write_text(
        "def restore_after_failed_install(**kwargs):\n"
        f"    open({str(tmp_path / 'units-restored')!r}, 'w').write(repr(sorted(kwargs)))\n")
    script = (ROOT / "install.sh").read_text()
    restore = script[script.index("rollback_failed_activation() {"):
                     script.index("trap rollback_failed_activation EXIT")]
    harness = tmp_path / "restore.sh"
    harness.write_text(f"""#!/usr/bin/env bash
REPO_DIR={str(ROOT)!r} SHARE={str(share)!r} RELEASE={str(new)!r} PYTHON={sys.executable!r}
PREVIOUS_CURRENT={str(old)!r} PREVIOUS_TOOLCHAIN="" TOOLCHAIN_MODE=existing ACTIVATED=1
SERVICE_FILE={str(tmp_path / 'clarp.service')!r} RUNTIME_SERVICE_FILE={str(tmp_path / 'clarp-runtime.service')!r}
UNIT_BACKUP={str(tmp_path / 'unit.bak')!r} RUNTIME_UNIT_BACKUP={str(tmp_path / 'runtime-unit.bak')!r}
UNIT_HAD_PREVIOUS=0 RUNTIME_UNIT_HAD_PREVIOUS=0
restore_external() {{ :; }}
{restore}
(exit 7)
rollback_failed_activation
""")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CLAUDE_PWA_", "CLARP_", "XDG_"))}

    ran = subprocess.run(["bash", str(harness)], env=env, capture_output=True, text=True,
                         timeout=120)

    assert ran.returncode == 7 and "restoring previous release" in ran.stderr
    assert (share / "current").resolve() == old.resolve()
    assert (tmp_path / "units-restored").exists()                      # the services were restarted
    assert json.loads((share / schema_guard.ROLLBACK_MARKER).read_text()) == {
        "code": CODE, "database": CODE + 1}
    assert "Older code does not know the tables and columns" in ran.stderr
    _the_restored_host_starts(home, share, database)


def test_a_failed_activation_puts_the_previous_release_back_and_it_starts(tmp_path, monkeypatch):
    """The same through ``clarp-admin`` (rollback, the setup wizard): the
    activation's own restore records the override for the release it puts
    back."""
    home, share, old, new, database = _two_releases(tmp_path)
    for release in (old, new):
        (release / "SERVICE_PYTHON").write_text(sys.executable + "\n")
        (release / "SERVICE_PATH").write_text("/usr/bin\n")
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setattr(admin, "HOME", home)
    monkeypatch.setattr(admin, "cmd_skills", lambda args: 0)
    monkeypatch.setattr(admin.service_manager, "write_definition", lambda **kwargs: None)
    monkeypatch.setattr(admin.service_manager, "definition_path",
                        lambda _home: tmp_path / "clarp.service")
    restored = []
    monkeypatch.setattr(admin.service_manager, "restore_after_failed_install",
                        lambda **kwargs: restored.append(kwargs))

    def the_new_host_upgrades_then_fails_its_health_check():
        _the_new_host_upgrades(database)
        raise RuntimeError("the Host did not answer within 20 s")

    monkeypatch.setattr(admin.service_manager, "install_and_restart",
                        the_new_host_upgrades_then_fails_its_health_check)

    with pytest.raises(RuntimeError, match="did not answer"):
        admin.activate_release(new)

    assert (share / "current").resolve() == old.resolve() and restored
    assert schema_guard.rollback_allowed(share, code=CODE, stored=CODE + 1)
    _the_restored_host_starts(home, share, database)


def test_the_command_a_refusing_host_names_gets_it_back(tmp_path, monkeypatch, capsys):
    """Without the override (a release put back by hand, say), the Host
    refuses and names a command. That command, run as written, lets this
    release start."""
    home, share, old, new, database = _two_releases(tmp_path)
    _the_new_host_upgrades(database)

    code, said, _ = _start(share / "server.py", _environment(home, share, database),
                           lambda: False, limit=60)
    assert code == 78
    named = re.search(r"clarp-admin rollback (\S+) --allow-older-schema", said)
    assert named and named.group(1) == old.name == RELEASE

    monkeypatch.setattr(admin, "SHARE", share)
    activated = []
    monkeypatch.setattr(admin, "activate_release",
                        lambda release, **options: activated.append((release.name, options)))
    # Named without the override it is "the current release", as it always was.
    with pytest.raises(SystemExit, match="no previous matching release"):
        admin.cmd_rollback(argparse.Namespace(version=named.group(1)))
    admin.cmd_rollback(argparse.Namespace(version=named.group(1), allow_older_schema=True))
    assert activated == [(old.name, {"allow_older_schema": True})]
    # And a bare rollback still means the newest other release.
    admin.cmd_rollback(argparse.Namespace(version=None, allow_older_schema=True))
    assert activated[-1][0] == new.name

    # What that activation records is what the Host asks for.
    schema_guard.check_source(old, database, share=share, allow=True)
    capsys.readouterr()
    _the_restored_host_starts(home, share, database)
