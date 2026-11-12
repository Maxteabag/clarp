"""Older code is not put on a newer database, and a bare update is not a downgrade.

2026-11-11 (issue 262): ``clarp-admin update`` with no ``--ref`` resolved the
latest release tag, two months behind the installed main, and installed it. A
Host built for schema 80 started on a schema 122 database, skipped migration
because the stored version was higher, served requests, pruned and failed its
transcript imports on a UNIQUE constraint for about a minute.

Everything here runs against throwaway repositories, releases and databases.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest

from lib import db_schema, schema_guard
from tests.unit.test_clarp_admin import admin
from tests.unit.test_only_services_migrate import _database_at, _own_port, _paths, _snapshot

ROOT = pathlib.Path(__file__).resolve().parents[2]
CODE = db_schema._SCHEMA_VERSION
OLD, NEW = 80, 122           # the two schema versions of that day


def _git(repo, *args):
    done = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
         "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        cwd=repo, check=True, text=True, capture_output=True)
    return done.stdout.strip()


def _commit(repo, name, *, schema=None, date):
    """One commit of a (very small) Clarp source tree."""
    if schema is not None:
        (repo / "server/lib").mkdir(parents=True, exist_ok=True)
        (repo / "server/lib/db_schema.py").write_text(f"_SCHEMA_VERSION = {schema}  # {name}\n")
    (repo / "install.sh").write_text(
        "#!/bin/sh\n"
        'echo "$(git rev-parse HEAD) ${CLARP_ALLOW_OLDER_SCHEMA:-none}" >> "$INSTALL_LOG"\n')
    (repo / "install.sh").chmod(0o755)
    (repo / "note").write_text(name)
    _git(repo, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", name],
        cwd=repo, check=True, env=env)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def install(tmp_path, monkeypatch):
    """An installation and the repository it updates from: a release tag from
    September (schema 80) and a main two months ahead of it (schema 122)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    tagged = _commit(origin, "the September release", schema=OLD, date="2026-09-10T12:00:00")
    _git(origin, "tag", "v0.9.0")
    _commit(origin, "work in October", schema=101, date="2026-10-10T12:00:00")
    head = _commit(origin, "work in November", schema=NEW, date="2026-11-10T12:00:00")
    source = tmp_path / "source"
    _git(tmp_path, "clone", "-q", str(origin), str(source))

    share = tmp_path / "share"
    (share / "current").mkdir(parents=True)
    state = tmp_path / "install.json"
    state.write_text(json.dumps({"source_repo": str(source), "channel": "stable"}))
    log = tmp_path / "install.log"
    monkeypatch.setenv("INSTALL_LOG", str(log))
    monkeypatch.delenv(schema_guard.ROLLBACK_ENV, raising=False)
    monkeypatch.setattr(admin, "SHARE", share)
    monkeypatch.setattr(admin, "INSTALL_STATE", state)
    monkeypatch.setattr(admin, "cmd_skills", lambda args: 0)

    def installed(commit, *, built="20261110130000"):
        (share / "current/DEPLOYED_VERSION").write_text(commit[:7] + "\n")
        (share / "current/DEPLOYED_RELEASE_ID").write_text(f"{commit[:7]}-{built}-4242\n")

    def update(ref="", **options):
        return admin.cmd_update(argparse.Namespace(ref=ref, **options))

    def ran():
        return [line.split() for line in log.read_text().splitlines()] if log.exists() else []

    return argparse.Namespace(
        origin=origin, source=source, share=share, tagged=tagged, head=head,
        installed=installed, update=update, ran=ran, database=share / "state.sqlite")


# --- a bare update ---------------------------------------------------------------------

def test_a_bare_update_does_not_install_a_tag_older_than_what_is_installed(install, capsys):
    """The real sequence: main is installed, the stable channel's newest tag
    is two months behind it."""
    install.installed(install.head)

    with pytest.raises(SystemExit) as refused:
        install.update()

    said = capsys.readouterr().out
    assert "no --ref given; the stable channel resolves to v0.9.0" in said
    assert "2026-09-10, the September release" in said
    message = str(refused.value)
    assert "not installing v0.9.0" in message and "2 commits behind what is installed" in message
    assert "2026-11-10, work in November" in message
    assert "clarp-admin update --ref v0.9.0" in message
    assert "clarp-admin update --ref origin/main" in message
    assert install.ran() == []                       # nothing was installed


def test_told_to_a_bare_updates_older_tag_is_installed(install):
    install.installed(install.head)

    assert install.update(ref="v0.9.0") == 0

    assert install.ran() == [[install.tagged, "none"]]


def test_a_bare_update_to_something_newer_says_what_it_is_and_installs_it(install, capsys):
    install.installed(install.tagged)
    _git(install.origin, "tag", "v1.0.0")

    assert install.update() == 0

    said = capsys.readouterr().out
    assert "no --ref given; the stable channel resolves to v1.0.0" in said
    assert "work in November" in said
    assert install.ran() == [[install.head, "none"]]


def test_a_bare_update_to_what_is_installed_goes_ahead_and_says_so(install, capsys):
    install.installed(install.tagged)

    assert install.update() == 0

    assert "that is the installed version; installing it again" in capsys.readouterr().out
    assert install.ran() == [[install.tagged, "none"]]


def test_an_installed_commit_the_source_does_not_have_is_compared_by_its_build_time(install):
    """A release built from a local commit. A tag from before that build is
    not shown to be newer, so it is not installed without being asked for."""
    install.installed("0123abc" + "0" * 33, built="20261110130000")
    with pytest.raises(SystemExit) as refused:
        install.update()
    assert "committed before the installed release (0123abc) was built" in str(refused.value)
    assert install.ran() == []

    install.installed("0123abc" + "0" * 33, built="20260801090000")     # built before the tag
    assert install.update() == 0
    assert install.ran() == [[install.tagged, "none"]]


def test_a_tag_on_another_line_of_history_is_judged_by_its_date(install, capsys):
    """The tag is not an ancestor of what is installed (a release branch):
    commits on both sides. Made before the installed commit, it is still the
    older one, and a bare update does not install it."""
    _git(install.origin, "checkout", "-q", "-b", "release", install.tagged)
    _commit(install.origin, "a fix on the release branch", date="2026-09-20T12:00:00")
    _git(install.origin, "tag", "v0.9.1")
    side = _git(install.origin, "rev-parse", "HEAD")
    _git(install.origin, "checkout", "-q", "main")
    install.installed(install.head)

    with pytest.raises(SystemExit) as refused:
        install.update()
    assert "not installing v0.9.1" in str(refused.value)
    assert "2 commits behind what is installed" in str(refused.value)
    assert install.ran() == []

    # The same shape with the newer date on the tag's side is an update.
    _git(install.origin, "checkout", "-q", "release")
    _commit(install.origin, "a later fix on the release branch", date="2026-11-20T12:00:00")
    _git(install.origin, "tag", "v0.9.2")
    newer = _git(install.origin, "rev-parse", "HEAD")
    _git(install.origin, "checkout", "-q", "main")
    assert install.update() == 0
    assert install.ran() == [[newer, "none"]] and side != newer


def test_a_first_install_with_nothing_recorded_updates_as_before(install):
    assert install.update() == 0
    assert install.ran() == [[install.tagged, "none"]]


# --- an older source over a newer database ---------------------------------------------

def test_update_refuses_a_source_below_the_databases_schema(install):
    _database_at(install.database, NEW)
    before = _snapshot(install.database)
    install.installed(install.head)

    with pytest.raises(SystemExit) as refused:
        install.update(ref="v0.9.0")

    message = str(refused.value)
    assert f"v0.9.0 is at schema {OLD}, below the database's schema {NEW}" in message
    assert str(install.database) in message
    assert "clarp-admin update --ref v0.9.0 --allow-older-schema" in message
    assert install.ran() == [] and _snapshot(install.database) == before
    assert not (install.share / schema_guard.ROLLBACK_MARKER).exists()


def test_a_deliberate_rollback_says_what_it_risks_and_is_recorded_for_that_pair(install, capsys):
    _database_at(install.database, NEW)
    install.installed(install.head)

    assert install.update(ref="v0.9.0", allow_older_schema=True) == 0

    warned = capsys.readouterr().err
    assert f"installing code at schema {OLD} over a database at schema {NEW}" in warned
    assert "Older code does not know the tables and columns" in warned
    assert "Restore a database backup that matches this release" in warned
    # The source's own installer is told the same pair, and so is the Host.
    assert install.ran() == [[install.tagged, f"{NEW}:{OLD}"]]
    assert json.loads((install.share / schema_guard.ROLLBACK_MARKER).read_text()) == {
        "code": OLD, "database": NEW}
    assert schema_guard.rollback_allowed(install.share, code=OLD, stored=NEW)
    assert not schema_guard.rollback_allowed(install.share, code=OLD, stored=NEW + 1)
    assert not schema_guard.rollback_allowed(install.share, code=OLD - 1, stored=NEW)


def test_a_source_at_or_above_the_database_installs_without_being_asked(install):
    _database_at(install.database, OLD)
    assert install.update(ref="origin/main") == 0
    assert install.ran() == [[install.head, "none"]]


def _release(share, name, schema):
    release = share / "releases" / name
    (release / "lib").mkdir(parents=True)
    (release / "server.py").write_text("# server")
    (release / "lib/db_schema.py").write_text(f"_SCHEMA_VERSION = {schema}\n")
    (release / "INSTALL_OK").write_text("")
    return release


def test_rollback_refuses_a_release_below_the_databases_schema(tmp_path, monkeypatch):
    share = tmp_path / "share"
    old, new = _release(share, "old", OLD), _release(share, "new", NEW)
    (share / "current").symlink_to(new, target_is_directory=True)
    database = _database_at(share / "state.sqlite", NEW)
    monkeypatch.setattr(admin, "SHARE", share)
    activated = []
    real = admin.activate_release

    def activate(release, **options):
        # The guard is activate_release's first step; stop after it.
        monkeypatch.setattr(admin, "service_manager", None)
        try:
            real(release, **options)
        except AttributeError:
            activated.append(release.name)

    monkeypatch.setattr(admin, "activate_release", activate)

    with pytest.raises(SystemExit) as refused:
        admin.cmd_rollback(argparse.Namespace(version="old"))
    assert f"Release old is at schema {OLD}, below the database's schema {NEW}" in str(refused.value)
    assert "clarp-admin rollback --allow-older-schema" in str(refused.value)
    assert activated == [] and (share / "current").resolve() == new.resolve()
    assert database.exists() and not (share / schema_guard.ROLLBACK_MARKER).exists()

    admin.cmd_rollback(argparse.Namespace(version="old", allow_older_schema=True))
    assert activated == ["old"]
    assert schema_guard.rollback_allowed(share, code=OLD, stored=NEW)


def test_the_real_installer_refuses_an_older_source_before_it_changes_anything(tmp_path):
    """install.sh of this checkout, in a throwaway home whose database is one
    schema step ahead of it."""
    home = tmp_path / "home"
    share = home / ".local/share/clarp"
    database = _database_at(share / "state.sqlite", CODE + 1)
    before = _snapshot(database)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CLAUDE_PWA_", "CLARP_", "XDG_"))}
    env.update({"HOME": str(home), "CLARP_PLATFORM_OVERRIDE": "linux", "CLARP_SKIP_ENV": "1",
                "CLARP_SKIP_HEALTHCHECK": "1", "PYTHON": sys.executable})

    ran = subprocess.run([str(ROOT / "install.sh")], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)

    assert ran.returncode == 1
    assert (f"ERROR: This source is at schema {CODE}, below the database's schema {CODE + 1}"
            in ran.stderr)
    assert str(database) in ran.stderr and "--allow-older-schema" in ran.stderr
    assert "creating directories" not in ran.stdout
    assert _snapshot(database) == before
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*") if path.is_file()) == [
        ".local/share/clarp/state.sqlite"]


@pytest.mark.parametrize("value, allowed", [
    ("{stored}:{code}", True), ("", False), ("1", False), ("{code}:{stored}", False),
    ("{stored}:0", False)], ids=["this-pair", "unset", "a-bare-yes", "swapped", "another-code"])
def test_the_installers_override_has_to_name_both_versions(tmp_path, value, allowed):
    database = _database_at(tmp_path / "share/state.sqlite", CODE + 1)
    env = {**os.environ, schema_guard.ROLLBACK_ENV: value.format(stored=CODE + 1, code=CODE)}

    ran = subprocess.run(
        [sys.executable, str(ROOT / "server/lib/schema_guard.py"), "--source", str(ROOT),
         "--database", str(database), "--share", str(tmp_path / "share")],
        env=env, capture_output=True, text=True, timeout=60)

    assert (ran.returncode == 0) is allowed, ran.stderr
    assert (tmp_path / "share" / schema_guard.ROLLBACK_MARKER).exists() is allowed
    assert ("WARNING" if allowed else "ERROR") in ran.stderr and "Older code" in ran.stderr


# --- the Host at start-up --------------------------------------------------------------

def test_the_host_refuses_to_start_on_a_database_above_its_schema(tmp_path):
    database = _database_at(tmp_path / "state.sqlite", CODE + 1)

    with pytest.raises(schema_guard.OlderCode) as refused:
        schema_guard.check_host_start(database, tmp_path, code=CODE)
    assert (f"This Clarp Host is at schema {CODE}, below the database's schema {CODE + 1}"
            in str(refused.value))
    assert "clarp-admin rollback <this release> --allow-older-schema" in str(refused.value)
    with pytest.raises(schema_guard.OlderCode) as named:
        schema_guard.check_host_start(database, tmp_path, code=CODE, release="abc1234-2026")
    assert "clarp-admin rollback abc1234-2026 --allow-older-schema" in str(named.value)

    schema_guard.check_host_start(database, tmp_path, code=CODE + 1)      # its own schema
    schema_guard.check_host_start(tmp_path / "none.sqlite", tmp_path, code=CODE)   # a first start
    schema_guard.allow_rollback(tmp_path, code=CODE, stored=CODE + 1)
    schema_guard.check_host_start(database, tmp_path, code=CODE)          # asked for


def test_the_real_host_exits_before_it_opens_a_database_above_its_schema(tmp_path):
    """server.py itself, started as the service is, in a throwaway home."""
    paths = _paths(tmp_path)
    database = _database_at(pathlib.Path(paths["CLAUDE_PWA_DB"]), CODE + 1)
    before = _snapshot(database)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CLAUDE_PWA_", "CLARP_", "XDG_", "PYTEST"))}
    env.update(paths)
    _own_port(pathlib.Path(paths["CLAUDE_PWA_CONFIG"]))

    ran = subprocess.run([sys.executable, str(ROOT / "server/server.py")], cwd=ROOT / "server",
                         env=env, capture_output=True, text=True, timeout=180)

    assert ran.returncode == 78, ran.stderr[-1500:]
    assert (f"clarp: This Clarp Host is at schema {CODE}, below the database's schema "
            f"{CODE + 1}") in ran.stderr
    assert "listening on" not in ran.stdout
    assert _snapshot(database) == before


def test_the_stored_version_is_read_without_writing_also_beside_a_write_ahead_log(tmp_path):
    database = _database_at(tmp_path / "state.sqlite", 77)
    writer = sqlite3.connect(str(database))
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA user_version = 78")       # in the log, not yet in the file
    writer.commit()
    assert (tmp_path / "state.sqlite-wal").stat().st_size > 0
    held = _snapshot(database)

    assert schema_guard.stored_version(database) == 78
    assert _snapshot(database) == held
    writer.close()

    assert schema_guard.stored_version(tmp_path / "missing.sqlite") == 0
    (tmp_path / "empty.sqlite").write_bytes(b"")
    assert schema_guard.stored_version(tmp_path / "empty.sqlite") == 0
    (tmp_path / "junk.sqlite").write_bytes(b"not a database" * 20)
    with pytest.raises(sqlite3.DatabaseError):
        schema_guard.stored_version(tmp_path / "junk.sqlite")
    assert schema_guard.code_version(ROOT) == CODE
    assert schema_guard.code_version(tmp_path) is None
