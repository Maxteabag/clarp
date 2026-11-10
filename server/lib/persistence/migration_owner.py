"""Explicit bootstrap/release migration authority; ordinary clients never call it.

Run from the immutable candidate server tree with
``python -m lib.persistence.migration_owner`` before activation. The companion
serializes participating owners only: pre-R clients and the historical long
migration transaction remain named PT-02 residuals. No migration-body retry.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import sys

from . import schema_readiness as readiness


class MigrationOwnerError(RuntimeError):
    """Bounded, content-free owner diagnostic, independent of the business log."""


def _identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1):
        raise MigrationOwnerError("migration_owner_identity")
    return info.st_dev, info.st_ino


@contextmanager
def _custody(path: Path):
    # Canonicalize the parent without following a final DB/lock symlink. The
    # configured parent keeps its existing meaning; no second path/env contract.
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.resolve(strict=True)
    if parent.stat().st_uid != os.geteuid():
        raise MigrationOwnerError("migration_owner_identity")
    path = parent / path.name
    companion = path.with_name(path.name + ".migration-owner.lock")
    descriptor = os.open(companion, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        held = os.fstat(descriptor)
        if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.geteuid()
                or held.st_nlink != 1 or stat.S_IMODE(held.st_mode) != 0o600):
            raise MigrationOwnerError("migration_owner_identity")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationOwnerError("migration_owner_busy") from exc
        expected = held.st_dev, held.st_ino

        def revalidate():
            if _identity(companion) != expected:
                raise MigrationOwnerError("migration_owner_identity")

        revalidate()
        yield path, revalidate
        revalidate()
    finally:
        # Keep the lock inode: unlinking allows another process to acquire a
        # replacement while an earlier participant still holds the original.
        os.close(descriptor)


def _open_existing(path: Path, mode: str) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True,
                           isolation_level=None)


def run(path: Path | None = None) -> None:
    """Prepare this release's schema under actual companion/DB custody.

    No caller-supplied boolean or environment flag conveys ownership. The raw
    connection is local to this synchronous run and never enters db's cache.
    """
    selected = readiness.configured_path() if path is None else path
    with _custody(selected) as (canonical, revalidate):
        try:
            identity = _identity(canonical)
        except FileNotFoundError:
            # Creation belongs to the held owner, never the check-only layer.
            fd = os.open(canonical, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600)
            os.close(fd)
            identity = _identity(canonical)
        connection = _open_existing(canonical, "ro")
        try:
            if _identity(canonical) != identity:
                raise MigrationOwnerError("migration_owner_identity")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == readiness.TARGET_VERSION:
                readiness.check_connection(connection, canonical, identity)
                return  # No WAL/init PRAGMAs on an already current database.
            if version != 0 and not readiness.UPGRADE_FLOOR <= version < readiness.TARGET_VERSION:
                raise readiness.SchemaUnsupported(version=version)
        finally:
            connection.close()
        revalidate()
        if _identity(canonical) != identity:
            raise MigrationOwnerError("migration_owner_identity")
        # Import the unchanged historical implementation only after custody and
        # classification. Helpers receive this exact raw connection transitively.
        from .. import db, db_migrations

        connection = _open_existing(canonical, "rw")
        try:
            if _identity(canonical) != identity:
                raise MigrationOwnerError("migration_owner_identity")
            connection.row_factory = sqlite3.Row
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == readiness.TARGET_VERSION:
                readiness.check_connection(connection, canonical, identity)
                return
            if version != 0 and not readiness.UPGRADE_FLOOR <= version < readiness.TARGET_VERSION:
                raise readiness.SchemaUnsupported(version=version)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {db.SQLITE_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA cache_size = -8192")
            connection.execute("PRAGMA mmap_size = 1073741824")
            revalidate()
            db_migrations._migrate(connection)
            if connection.in_transaction:
                raise MigrationOwnerError("migration_owner_uncommitted")
            readiness.check_connection(connection, canonical, identity)
        finally:
            connection.close()


def main() -> int:
    if len(sys.argv) != 1:
        print("migration_owner_invalid_arguments", file=sys.stderr)
        return 2
    try:
        run()
    except readiness.SchemaNotReady as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except MigrationOwnerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Preserve original failures as causes internally; never emit paths,
        # raw SQL or database contents in the bootstrap command's stderr.
        print(f"migration_owner_failed type={type(exc).__name__} "
              f"sqlite_code={getattr(exc, 'sqlite_errorcode', 'none')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
