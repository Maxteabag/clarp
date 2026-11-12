"""Only the Host and runtime services upgrade a database.

elitebook, 2026-11-11 23:27: a debug script was run from a development
checkout, outside pytest, in an agent's shell. Every agent turn carries
``CLAUDE_PWA_DB`` naming the live database. The script only ran a simulation
that logs; its first log line went ``log() -> eventlog.emit ->
diagnostics_settings -> settings_store.get -> db.conn()``, and ``db.conn()``
upgraded the live database, because the checkout was one schema step ahead of
every release installed on that machine (issue 262).

These tests replay that from a real child process (no fixture reaches it),
against throwaway paths, and pin the rule: a process upgrades a database only
when its own entry point said it is a service.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import sqlite3
import subprocess
import sys
import textwrap
import time

import pytest

from lib import db, db_schema, diagnostics_settings, eventlog, schema_guard
from lib.log import log, log_exception
from tests.unit.test_db_migrations import _shape_as_v61

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
CODE = db_schema._SCHEMA_VERSION


def _snapshot(path: pathlib.Path):
    """The file's bytes and time, and everything beside it."""
    return (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns,
            sorted(entry.name for entry in path.parent.iterdir()))


def _database_at(path: pathlib.Path, version: int) -> pathlib.Path:
    """A database of this code's shape whose stored schema is ``version``,
    closed and with no write-ahead log left beside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    db._migrate(con)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("INSERT INTO settings (key, value, updated_at) VALUES ('kept', 'yes', 1)")
    con.execute(f"PRAGMA user_version = {version}")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    assert schema_guard.stored_version(path) == version
    return path


def _paths(tmp_path):
    home = tmp_path / "home"
    return {
        "HOME": str(home),
        "CLAUDE_PWA_DB": str(home / "share/state.sqlite"),
        "CLAUDE_PWA_LOG_DIR": str(home / "logs"),
        "CLAUDE_PWA_CONFIG": str(home / "config/config.toml"),
        "CLARP_CONFIG_DIR": str(home / "config"),
        "CLARP_SHARE_DIR": str(home / "share"),
        "CLARP_CACHE_DIR": str(home / "cache"),
        "CLARP_TELEMETRY_DB": str(home / "share/telemetry.sqlite"),
        "XDG_DATA_HOME": str(home / "xdg-data"), "XDG_CACHE_HOME": str(home / "xdg-cache"),
        "XDG_CONFIG_HOME": str(home / "xdg-config"), "XDG_STATE_HOME": str(home / "xdg-state"),
    }


def _own_port(config: pathlib.Path) -> None:
    """A service started by a test listens on a port of its own, never the
    one a Host on this machine uses."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f'[server]\nbind_addr = "127.0.0.1"\nport = {port}\n')


def _run(tmp_path, code: str, *, cwd=SERVER) -> subprocess.CompletedProcess:
    """A standalone python, as an agent's shell would start it: the paths in
    the environment, nothing of pytest's."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CLAUDE_PWA_", "CLARP_", "XDG_", "PYTEST"))}
    env.update(_paths(tmp_path))
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=120)


# --- the real sequence -----------------------------------------------------------------

THE_DEBUG_SCRIPT = """
    import sys
    sys.path.insert(0, ".")
    from lib.account_failover import AccountFailover      # Host code from the checkout
    from lib.log import log
    log("claudeAccountRecovered", "turns=4")              # what its simulation logged
    print("done")
"""


def test_a_checkout_one_step_ahead_that_logs_once_leaves_the_database_untouched(tmp_path):
    database = _database_at(pathlib.Path(_paths(tmp_path)["CLAUDE_PWA_DB"]), CODE - 1)
    before = _snapshot(database)

    ran = _run(tmp_path, THE_DEBUG_SCRIPT)

    assert ran.returncode == 0 and ran.stdout.strip() == "done", ran.stderr
    assert "claudeAccountRecovered turns=4" in ran.stderr      # the line is still logged
    assert _snapshot(database) == before
    assert schema_guard.stored_version(database) == CODE - 1
    # And nothing else appeared on the paths the environment names.
    home = tmp_path / "home"
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == [
        "share", "share/state.sqlite"]


def test_a_script_that_opens_an_older_database_is_refused_and_says_both_versions(tmp_path):
    database = _database_at(pathlib.Path(_paths(tmp_path)["CLAUDE_PWA_DB"]), CODE - 1)
    before = _snapshot(database)

    ran = _run(tmp_path, """
        import sys
        sys.path.insert(0, ".")
        from lib import db
        db._UPGRADE_WAIT_SEC = 0.3
        db.conn()
        print("opened")
    """)

    assert ran.returncode != 0 and "opened" not in ran.stdout
    assert "DatabaseOlderThanCode" in ran.stderr
    assert f"is at schema {CODE - 1}" in ran.stderr and f"is at schema {CODE}" in ran.stderr
    assert str(database) in ran.stderr
    assert "Only the Clarp Host and runtime services upgrade a database" in ran.stderr
    assert _snapshot(database) == before


@pytest.mark.parametrize("role", ["host", "runtime"])
def test_a_service_start_upgrades_the_database(tmp_path, role):
    """A database a much older release left, opened by a process whose entry
    point said it is a service."""
    database = pathlib.Path(_paths(tmp_path)["CLAUDE_PWA_DB"])
    database.parent.mkdir(parents=True)
    con = sqlite3.connect(str(database))
    db._migrate(con)
    _shape_as_v61(con)
    con.commit()
    con.close()
    assert schema_guard.stored_version(database) == 61

    ran = _run(tmp_path, f"""
        import sys
        sys.path.insert(0, ".")
        from lib import db
        db.run_as_service("{role}")
        print(db.conn().execute("PRAGMA user_version").fetchone()[0])
    """)

    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == str(CODE)
    assert schema_guard.stored_version(database) == CODE


@pytest.mark.parametrize("entry", ["server.py", "runtime.py"])
def test_the_real_service_entry_points_upgrade_the_database_when_they_start(tmp_path, entry):
    """The Host and the runtime themselves, started as their units start
    them, in a throwaway home with a database one schema step behind. They
    are stopped as soon as the database is at their schema."""
    import time

    paths = _paths(tmp_path)
    database = _database_at(pathlib.Path(paths["CLAUDE_PWA_DB"]), CODE - 1)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CLAUDE_PWA_", "CLARP_", "XDG_", "PYTEST"))}
    env.update(paths)
    _own_port(pathlib.Path(paths["CLAUDE_PWA_CONFIG"]))
    errors = tmp_path / "stderr.txt"

    with errors.open("w") as stderr:
        service = subprocess.Popen(
            [sys.executable, str(SERVER / entry)], cwd=SERVER, env=env,
            stdout=subprocess.DEVNULL, stderr=stderr)
        try:
            deadline = time.monotonic() + 150
            while (schema_guard.stored_version(database) != CODE and service.poll() is None
                   and time.monotonic() < deadline):
                time.sleep(0.1)
        finally:
            service.terminate()
            try:
                service.wait(timeout=30)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=30)

    assert schema_guard.stored_version(database) == CODE, errors.read_text()[-2000:]
    assert "is not an installed release" not in errors.read_text()


def test_the_two_services_say_what_they_are_in_their_own_entry_points():
    """The grant is a call a service's ``main`` makes, and only those make it."""
    runtime = (SERVER / "runtime.py").read_text()
    assert 'def main() -> int:\n    # This process is the runtime service' in runtime
    assert 'if not db.run_as_service("runtime", code_root=ROOT, share=LAYOUT.share):' in runtime
    host = (SERVER / "server.py").read_text()
    main = host[host.index('\nif __name__ == "__main__":'):]
    assert '_code_root = pathlib.Path(__file__).resolve().parent' in main
    assert '_state_db.run_as_service("host", code_root=_code_root, share=_layout.share)' in main
    assert "run_as_service" not in host[:host.index('\nif __name__ == "__main__":')]
    callers = sorted(
        path.relative_to(ROOT).as_posix()
        for folder in ("server", "bin", "scripts", "plugin")
        for path in (ROOT / folder).rglob("*.py")
        if "run_as_service(" in path.read_text() and path.name != "db.py")
    assert callers == ["server/runtime.py", "server/server.py"]


def _installation(share: pathlib.Path, version: int) -> pathlib.Path:
    """An installed Clarp: a release, the ``current`` link and its database."""
    release = share / "releases/abc1234-20261110130000-4242"
    release.mkdir(parents=True)
    (release / "server.py").write_text("# the installed Host")
    (share / "current").symlink_to(release, target_is_directory=True)
    _database_at(share / "state.sqlite", version)
    return release


def test_a_host_started_from_a_checkout_is_not_the_installations_service(tmp_path):
    """The same shell, one step further: ``python server/server.py`` from a
    development checkout, with the live paths inherited. It is told that it
    is not the installed Host and upgrades nothing."""
    paths = _paths(tmp_path)
    share = pathlib.Path(paths["CLARP_SHARE_DIR"])
    _installation(share, CODE - 1)
    database = share / "state.sqlite"
    before = _snapshot(database)

    ran = _run(tmp_path, """
        import pathlib, sys
        sys.path.insert(0, ".")
        from lib import db
        from lib.deployment import LAYOUT
        db._UPGRADE_WAIT_SEC = 0.0
        granted = db.run_as_service("host", code_root=pathlib.Path.cwd(), share=LAYOUT.share)
        try:
            db.conn()
        except db.DatabaseOlderThanCode:
            print("refused", granted, db.is_service())
    """)

    assert ran.stdout.strip() == "refused False False", ran.stderr
    assert "is not an installed release of" in ran.stderr and str(database) in ran.stderr
    assert _snapshot(database) == before


def test_the_installations_own_release_is_its_service_wherever_it_points(tmp_path):
    share = tmp_path / "share"
    release = _installation(share, CODE - 1)
    database = share / "state.sqlite"

    # One of its releases, on its database: the service (also the runtime
    # that still runs the release before the current one).
    assert not schema_guard.foreign_to_installation(release, database, share)
    older = share / "releases/0000000-20261001090000-17"
    older.mkdir()
    assert not schema_guard.foreign_to_installation(older, database, share)
    # A checkout on that database is not.
    assert schema_guard.foreign_to_installation(ROOT / "server", database, share)
    # A checkout on its own database, or with no installation in sight, is
    # nobody's stranger: tests and development Hosts keep their own schema.
    assert not schema_guard.foreign_to_installation(ROOT / "server", tmp_path / "dev.sqlite", share)
    assert not schema_guard.foreign_to_installation(
        ROOT / "server", tmp_path / "x/state.sqlite", tmp_path / "x")


def test_no_environment_variable_grants_it(tmp_path):
    database = _database_at(pathlib.Path(_paths(tmp_path)["CLAUDE_PWA_DB"]), CODE - 1)
    before = _snapshot(database)
    assert not [name for name in ("os.environ", "getenv") if name in (
        SERVER / "lib/db.py").read_text().split("def run_as_service")[1].split("\ndef ")[0]]

    ran = _run(tmp_path, """
        import os, sys
        sys.path.insert(0, ".")
        for name in ("CLARP_SERVICE", "CLARP_RUN_AS_SERVICE", "CLARP_MIGRATE", "INVOCATION_ID"):
            os.environ[name] = "host"
        from lib import db
        db._UPGRADE_WAIT_SEC = 0.0
        try:
            db.conn()
        except db.DatabaseOlderThanCode:
            print("refused", db.is_service())
    """)

    assert ran.stdout.strip() == "refused False", ran.stderr
    assert _snapshot(database) == before


# --- the rule, in process ----------------------------------------------------------------

@pytest.fixture
def not_a_service(monkeypatch):
    """What every process is until its entry point says otherwise."""
    monkeypatch.setattr(db, "_SERVICE", "")
    monkeypatch.setattr(db, "_UPGRADE_WAIT_SEC", 0.0)


def _point_at(path):
    db.reset_for_tests(path)
    eventlog.reset_for_tests(path.parent.with_name(path.parent.name + "-logs"))
    diagnostics_settings.reset_for_tests()


def test_what_is_not_a_service_may_create_a_database_but_never_upgrade_one(
        tmp_path, not_a_service):
    fresh = tmp_path / "fresh/state.sqlite"
    _point_at(fresh)
    assert db.conn().execute("PRAGMA user_version").fetchone()[0] == CODE

    older = _database_at(tmp_path / "older/state.sqlite", CODE - 1)
    before = _snapshot(older)
    _point_at(older)
    with pytest.raises(db.DatabaseOlderThanCode) as refused:
        db.conn()
    assert f"schema {CODE - 1}" in str(refused.value) and f"schema {CODE}" in str(refused.value)
    assert _snapshot(older) == before and not db.opened()

    # A database at this code's schema, or above it, opens as it always did.
    for version in (CODE, CODE + 1):
        path = _database_at(tmp_path / f"v{version}/state.sqlite", version)
        _point_at(path)
        assert db.conn().execute("SELECT value FROM settings WHERE key='kept'").fetchone()[0] == "yes"
        assert schema_guard.stored_version(path) == version


def test_an_older_database_is_not_even_switched_to_write_ahead_logging(tmp_path, not_a_service):
    """Opening is itself a write on a database that does not use a
    write-ahead log yet. The refusal comes before the file is opened."""
    older = tmp_path / "state.sqlite"
    con = sqlite3.connect(str(older))
    db._migrate(con)
    con.execute(f"PRAGMA user_version = {CODE - 1}")
    con.commit()
    con.close()
    assert sqlite3.connect(str(older)).execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    before = _snapshot(older)
    _point_at(older)

    with pytest.raises(db.DatabaseOlderThanCode):
        db.conn()

    assert _snapshot(older) == before
    assert sqlite3.connect(str(older)).execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_the_open_itself_refuses_too_should_the_file_change_under_the_first_look(
        tmp_path, monkeypatch, not_a_service):
    older = _database_at(tmp_path / "state.sqlite", CODE - 1)
    _point_at(older)
    monkeypatch.setattr(db, "_leave_an_older_database_alone", lambda: None)

    with pytest.raises(db.DatabaseOlderThanCode):
        db.conn()

    assert schema_guard.stored_version(older) == CODE - 1 and not db.opened()
    assert sqlite3.connect(str(older)).execute(
        "SELECT value FROM settings WHERE key='kept'").fetchone()[0] == "yes"


def test_it_waits_a_moment_for_a_service_to_upgrade_then_opens(tmp_path, monkeypatch, not_a_service):
    """An install switches the release first and restarts the Host after: the
    new release's hooks can meet the old schema for a few seconds."""
    older = _database_at(tmp_path / "state.sqlite", CODE - 1)
    _point_at(older)
    monkeypatch.setattr(db, "_UPGRADE_WAIT_SEC", 30.0)
    seen = []

    def the_host_upgrades_meanwhile(seconds):
        seen.append(seconds)
        con = sqlite3.connect(str(older))
        con.execute(f"PRAGMA user_version = {CODE}")
        con.commit()
        con.close()

    monkeypatch.setattr(db.time, "sleep", the_host_upgrades_meanwhile)

    assert db.conn().execute("PRAGMA user_version").fetchone()[0] == CODE
    assert len(seen) == 1


def test_the_wait_is_spent_once_in_a_process(tmp_path, monkeypatch, not_a_service):
    """A hook or a tool that opens the database again after it was refused
    is refused at once: measured by the reviewer, a second open waited
    another ten seconds."""
    older = _database_at(tmp_path / "state.sqlite", CODE - 1)
    _point_at(older)
    monkeypatch.setattr(db, "_UPGRADE_WAIT_SEC", 1.0)
    slept, real_sleep = [], time.sleep
    monkeypatch.setattr(db.time, "sleep", lambda seconds: slept.append(seconds) or real_sleep(0.3))

    with pytest.raises(db.DatabaseOlderThanCode):
        db.conn()
    waited = len(slept)
    assert waited >= 2
    with pytest.raises(db.DatabaseOlderThanCode):
        db.conn()

    assert len(slept) == waited                 # no second wait
    # A database that is upgraded later is opened without ado.
    con = sqlite3.connect(str(older))
    con.execute(f"PRAGMA user_version = {CODE}")
    con.commit()
    con.close()
    assert db.conn().execute("PRAGMA user_version").fetchone()[0] == CODE


def test_a_tool_started_during_an_install_waits_for_the_host_to_upgrade(tmp_path):
    """The window the wait exists for, in a real process with the wait as it
    ships: the release has been switched, the Host upgrades the database two
    seconds later, and a tool that opened it meanwhile carries on."""
    database = _database_at(pathlib.Path(_paths(tmp_path)["CLAUDE_PWA_DB"]), CODE - 1)

    ran = _run(tmp_path, f"""
        import sqlite3, sys, threading, time
        sys.path.insert(0, ".")
        from lib import db

        def the_host_upgrades():
            time.sleep(2.0)
            con = sqlite3.connect({str(database)!r})
            con.execute("PRAGMA user_version = {CODE}")
            con.commit()
            con.close()

        threading.Thread(target=the_host_upgrades, daemon=True).start()
        began = time.monotonic()
        version = db.conn().execute("PRAGMA user_version").fetchone()[0]
        print(version, round(time.monotonic() - began, 1))
    """)

    assert ran.returncode == 0, ran.stderr[-1500:]
    version, took = ran.stdout.split()
    assert int(version) == CODE and 1.5 <= float(took) < 9.0


def test_a_service_upgrades(tmp_path):
    older = _database_at(tmp_path / "state.sqlite", CODE - 1)
    _point_at(older)
    assert db.is_service()                    # the fixture said so for this test

    assert db.conn().execute("PRAGMA user_version").fetchone()[0] == CODE


def test_only_a_known_service_can_be_named():
    with pytest.raises(ValueError):
        db.run_as_service("script")
    with pytest.raises(ValueError):
        db.run_as_service("")


# --- logging ---------------------------------------------------------------------------

def test_logging_never_opens_the_database(tmp_path, monkeypatch, not_a_service):
    database = _database_at(tmp_path / "state.sqlite", CODE)
    _point_at(database)
    before = _snapshot(database)
    opened = []
    monkeypatch.setattr(db, "_open_connection", lambda: opened.append(1) or pytest.fail(
        "logging opened the database"))

    log("claudeAccountRecovered", "turns=4")
    log_exception("claudeAccountRecoveryFail", RuntimeError("boom"))
    eventlog.emit("server", "claudeAccountWaiting", detail={"turns": 4})
    eventlog.emit_exception("server", "goalWakeLimitKindFail", RuntimeError("boom"))
    assert diagnostics_settings.allows("agents") is False
    assert diagnostics_settings.allows_event(source="server", event="serverStart") is False

    assert opened == [] and not db.opened()
    assert _snapshot(database) == before
    assert list(eventlog.LOG_DIR.iterdir()) == []       # and no event was written


def test_a_process_that_uses_the_database_still_logs_by_its_settings(tmp_path):
    """A hook, an admin command, the services: once the process has opened
    the database for its own work, the opt-in capture settings apply."""
    _point_at(tmp_path / "state.sqlite")
    db.conn()
    diagnostics_settings.update({"enabled": True, "categories": list(diagnostics_settings.CATEGORIES)})
    diagnostics_settings.reset_for_tests()

    assert diagnostics_settings.get().enabled is True


def test_the_log_directory_is_made_by_the_first_event_written(tmp_path, monkeypatch):
    """Not by importing: it does not exist until something is logged to it."""
    _point_at(tmp_path / "state.sqlite")
    db.conn()
    diagnostics_settings.update({"enabled": True, "categories": list(diagnostics_settings.CATEGORIES)})
    logs = tmp_path / "not-yet/logs"
    monkeypatch.setattr(eventlog, "LOG_DIR", logs)
    assert not logs.exists()

    eventlog.emit("server", "serverStart", detail={"port": 1})

    written = [path for path in logs.iterdir() if path.suffix == ".jsonl"]
    assert len(written) == 1 and '"event":"serverStart"' in written[0].read_text()


# --- importing -------------------------------------------------------------------------

def test_importing_host_code_creates_nothing_on_the_paths_it_is_given(tmp_path):
    """Every module under server/, imported in a fresh process whose home and
    every Clarp path point into an empty directory."""
    home = tmp_path / "home"
    home.mkdir()

    ran = _run(tmp_path, """
        import importlib, pkgutil, sys
        sys.path.insert(0, ".")
        import lib
        imported, failed = 0, []
        for module in pkgutil.walk_packages(lib.__path__, "lib."):
            try:
                importlib.import_module(module.name)
                imported += 1
            except Exception as error:      # optional dependencies, platform modules
                failed.append(f"{module.name}: {type(error).__name__}")
        import server
        print(imported, len(failed))
    """)

    assert ran.returncode == 0, ran.stderr[-2000:]
    imported, failed = (int(part) for part in ran.stdout.split()[-2:])
    assert imported > 400 and failed < 25, ran.stdout
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == []
