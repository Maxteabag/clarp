"""Inert, check-only schema admission for the PT-02 R ownership bridge.

No schema provider, migration module, event log or business store is imported.
The descriptor deliberately admits all existing schema-121 shapes rather than
turning a diagnostic schema manifest into an application admission allowlist.
"""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import stat

from .. import xdg

TARGET_VERSION = 121
UPGRADE_FLOOR = 61


class SchemaNotReady(RuntimeError):
    """The explicit release/bootstrap owner has not prepared this database."""

    code = "schema_not_ready"

    def __init__(self, *, version: int | None = None, sqlite_code: int | None = None):
        self.version = version
        self.sqlite_code = sqlite_code
        super().__init__(f"{self.code} version={version if version is not None else 'unknown'}"
                         f" sqlite_code={sqlite_code if sqlite_code is not None else 'none'}")


class SchemaUnsupported(SchemaNotReady):
    """This reader cannot safely interpret the stored schema version."""

    code = "schema_unsupported"


def configured_path() -> Path:
    """Use the existing state path contract; resolving it performs no I/O writes."""
    return Path(os.environ.get("CLAUDE_PWA_DB", str(xdg.data_dir() / "state.sqlite")))


def require_version(version: int) -> None:
    if version == TARGET_VERSION:
        return
    if version == 0 or UPGRADE_FLOOR <= version < TARGET_VERSION:
        raise SchemaNotReady(version=version)
    raise SchemaUnsupported(version=version)


def file_identity(path: Path) -> tuple[int, int]:
    try:
        info = path.stat()
    except OSError as exc:
        raise SchemaNotReady() from exc
    if not stat.S_ISREG(info.st_mode):
        raise SchemaNotReady()
    return info.st_dev, info.st_ino


def check_connection(connection: sqlite3.Connection, path: Path,
                     identity: tuple[int, int]) -> None:
    """Validate between business units, never invalidate a caller transaction.

    The caller supplies the identity captured when opening its connection.
    Established release/restore quiescence still governs actual DB replacement;
    this check does not turn a stat/read sequence into a replacement lock.
    """
    if connection.in_transaction:
        return
    if file_identity(path) != identity:
        raise SchemaNotReady()
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        raise SchemaNotReady(sqlite_code=getattr(exc, "sqlite_errorcode", None)) from exc
    require_version(version)
    if file_identity(path) != identity:
        raise SchemaNotReady()


def check_existing(path: Path | None = None) -> None:
    """Check compatibility without mkdir, SQLite creation or write PRAGMAs."""
    path = configured_path() if path is None else path
    identity = file_identity(path)
    try:
        connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise SchemaNotReady(sqlite_code=getattr(exc, "sqlite_errorcode", None)) from exc
    try:
        check_connection(connection, path, identity)
    finally:
        connection.close()
