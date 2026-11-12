"""Which code may meet which database.

Two rules, kept apart from ``db.py`` because the installer and the admin CLI
need them for a source tree and a database they must not open the ordinary
way (opening is what upgrades):

* Code never runs as the Host on a database NEWER than itself unless a
  rollback was asked for, in so many words, for exactly this pair of
  versions (issue 262: a two-month-old source, schema 80, served for a minute
  on a schema 122 database).
* Only a service upgrades a database OLDER than its code (``db.conn``). What
  this module adds to that is how to read the stored version without writing
  a byte.

Nothing here imports Host code or opens a database for writing, so it can be
run from a source tree that is not installed:

    python server/lib/schema_guard.py --source <tree> --database <state.sqlite>
"""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import struct
import sys
import urllib.parse

_VERSION_LINE = re.compile(r"^_SCHEMA_VERSION\s*=\s*(\d+)", re.M)
# The override for a deliberate rollback, written by the installer and read
# by the Host when it starts. It names both versions, so it allows that one
# pair and nothing else: left behind, it does not let any other old code in.
ROLLBACK_MARKER = "schema-rollback.json"
# How the admin CLI hands the same override to a source tree's installer.
ROLLBACK_ENV = "CLARP_ALLOW_OLDER_SCHEMA"

# What a service exits with when it refuses the database it was given
# (EX_CONFIG): starting it again changes nothing, and the units do not.
EXIT_REFUSED = 78

RISK = (
    "Older code does not know the tables and columns the newer release added: "
    "it can fail on them, and rows it writes may be wrong for the newer "
    "release when you go back up. Restore a database backup that matches "
    "this release if you mean to stay on it."
)


class OlderCode(RuntimeError):
    """The code is at a lower schema than the database it would run on."""


class NotThisInstallation(RuntimeError):
    """A service that is not one of the installation's releases was started
    on the installation's database, which is below its schema."""


def code_version(root: pathlib.Path | str) -> int | None:
    """The schema version a source tree or an installed release is built for,
    read from its text. None when the tree does not say (not a Clarp tree)."""
    root = pathlib.Path(root)
    for relative in ("server/lib/db_schema.py", "lib/db_schema.py",
                     "server/lib/db.py", "lib/db.py"):
        try:
            found = _VERSION_LINE.search((root / relative).read_text())
        except OSError:
            continue
        if found:
            return int(found.group(1))
    return None


def stored_version(path: pathlib.Path | str) -> int:
    """The database file's ``user_version``; 0 for a file that is missing or
    empty. The file and its ``-wal`` are never written.

    With no write-ahead log beside the file its header is the whole truth
    (bytes 60 to 63) and nothing at all is touched or created. With one, the
    newest header may still be in the log, so the file is read through
    SQLite, read-only. That leaves the file and the ``-wal`` as they are; the
    ``-shm`` index beside them can change, and is created if it was missing.

    The path is resolved first: SQLite keeps the ``-wal`` beside the real
    file, not beside a symlink to it, and a version read from the header
    alone is stale until the next checkpoint.
    """
    path = pathlib.Path(path).resolve()
    try:
        if path.stat().st_size == 0:
            return 0
    except OSError:
        return 0
    wal = path.with_name(path.name + "-wal")
    if not wal.exists() or wal.stat().st_size == 0:
        with path.open("rb") as handle:
            header = handle.read(100)
        if len(header) < 100 or not header.startswith(b"SQLite format 3\x00"):
            raise sqlite3.DatabaseError(f"{path} is not an SQLite database")
        return struct.unpack(">I", header[60:64])[0]
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def rollback_allowed(share: pathlib.Path | str, *, code: int, stored: int) -> bool:
    """Whether a rollback to exactly this code on exactly this database was
    asked for (see ``allow_rollback``)."""
    try:
        marker = json.loads((pathlib.Path(share) / ROLLBACK_MARKER).read_text())
    except (OSError, ValueError):
        return False
    return (isinstance(marker, dict) and marker.get("code") == code
            and marker.get("database") == stored)


def allow_rollback(share: pathlib.Path | str, *, code: int, stored: int) -> None:
    share = pathlib.Path(share)
    share.mkdir(parents=True, exist_ok=True)
    (share / ROLLBACK_MARKER).write_text(
        json.dumps({"code": code, "database": stored}) + "\n")


def env_override(value: str | None, *, code: int, stored: int) -> bool:
    """``CLARP_ALLOW_OLDER_SCHEMA=<database>:<code>``. It has to name both
    versions, so a value a shell still carries from an earlier rollback does
    not allow another one."""
    return (value or "").strip() == f"{stored}:{code}"


def older_code_message(*, code: int, stored: int, what: str, database, how: str) -> str:
    return (
        f"{what} is at schema {code}, below the database's schema {stored} "
        f"({database}). Refusing: {RISK}\n{how}")


def check_source(source, database, *, share=None, allow: bool = False,
                 what: str = "This source", how: str = "") -> tuple[int | None, int]:
    """Refuse (``OlderCode``) a source tree or release whose schema is below
    the database's, unless ``allow``; then record the override for the Host
    under ``share``. Returns (code version, stored version)."""
    code, stored = code_version(source), stored_version(database)
    if code is None or stored <= code:
        return code, stored
    if not allow:
        raise OlderCode(older_code_message(
            code=code, stored=stored, what=what, database=database, how=how))
    if share is not None:
        allow_rollback(share, code=code, stored=stored)
    print(f"WARNING: installing code at schema {code} over a database at schema "
          f"{stored} ({database}), as asked. {RISK}", file=sys.stderr)
    return code, stored


def foreign_to_installation(code_root, database, share) -> bool:
    """Whether this code would be serving an installation it is not part of:
    ``share`` holds an installed Clarp (a ``current`` release), ``database``
    is that installation's, and ``code_root`` is not one of its releases. A
    Host started from a development checkout in a shell that inherited the
    live paths is that; it is not the installed service, whatever it runs."""
    share, code_root = pathlib.Path(share), pathlib.Path(code_root)
    if not (share / "current").exists():
        return False
    try:
        if pathlib.Path(database).resolve() != (share / "state.sqlite").resolve():
            return False
        return not code_root.resolve().is_relative_to((share / "releases").resolve())
    except OSError:
        return True


def check_foreign_start(code_root, database, share, *, code: int) -> None:
    """For a service that ``db.run_as_service`` gave no grant (it is not one
    of this installation's releases): refuse (``NotThisInstallation``) now if
    the database is below its schema. Nobody will upgrade it for this
    process, so it could only wait and then fail at its first read; exiting
    1 there, it was restarted every twelve seconds."""
    stored = stored_version(database)
    if 0 < stored < code:
        raise NotThisInstallation(
            f"{code_root} is not an installed release of {share}, and that installation's "
            f"database ({database}) is at schema {stored}, below this code's schema {code}. "
            "Only its own Host and runtime upgrade it. Install this release there, or give "
            "this one a database of its own (CLAUDE_PWA_DB).")


def check_host_start(database, share, *, code: int, release: str = "") -> None:
    """What the Host asks before it opens the database: refuse (``OlderCode``)
    to serve a database above its own schema, unless a rollback to exactly
    this code on exactly this database was asked for. ``release`` is the
    name of the release that asks, for the command the refusal names."""
    stored = stored_version(database)
    if stored <= code:
        return
    if rollback_allowed(share, code=code, stored=stored):
        print(f"WARNING: this Clarp Host is at schema {code} and serves a database at "
              f"schema {stored} ({database}), because a rollback was asked for. {RISK}",
              file=sys.stderr, flush=True)
        return
    raise OlderCode(older_code_message(
        code=code, stored=stored, what="This Clarp Host", database=database,
        how=(f"Install a release at schema {stored} or newer. To run this older "
             f"release on purpose: clarp-admin rollback {release or '<this release>'} "
             "--allow-older-schema.")))


def main(argv: list[str]) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--share")
    parser.add_argument(
        "--restored", action="store_true",
        help="the source is the release an installer puts back after a failed install: "
             "record that it may run on a database the failed release already upgraded")
    args = parser.parse_args(argv)
    code, stored = code_version(args.source), stored_version(args.database)
    allow = args.restored or (code is not None and env_override(
        os.environ.get(ROLLBACK_ENV), code=code, stored=stored))
    try:
        check_source(
            args.source, args.database, share=args.share, allow=allow,
            how=("To roll back on purpose: clarp-admin update --ref <ref> "
                 "--allow-older-schema (or clarp-admin rollback "
                 "--allow-older-schema)."))
    except OlderCode as refused:
        print(f"ERROR: {refused}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
