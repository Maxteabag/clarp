"""Consistent backup and restart-applied restore for one Clarp data root.

Staging and applying a restore are guarded by lib.data_root_locks: a restore is staged only inside
a running lock-aware Host, bound to the archive's exact bytes, and applied only at that Host's next
start with exclusive ownership of the data root."""
from __future__ import annotations

import os
import pathlib
import shutil
import sqlite3
import tarfile
import tempfile
import time

from .deployment import LAYOUT

PENDING_RESTORE = "restore.pending.tar.gz"


# The preserved directory holding the pending archive, its staging record and the data-root locks.
# An archive may never carry anything there: a restore must not replace or shadow those files.
PRESERVED = pathlib.PurePosixPath("clarp/backups")


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe backup member: {member.name}")
        normalized = pathlib.PurePosixPath(*[part for part in path.parts if part not in {"", "."}])
        if normalized == PRESERVED or PRESERVED in normalized.parents:
            raise ValueError(f"backup member inside clarp/backups: {member.name}")
        if member.issym() or member.islnk():
            target = pathlib.PurePosixPath(member.linkname)
            parts: list[str] = []
            for part in pathlib.PurePosixPath(member.name).parent.joinpath(target).parts:
                if part in {"", "."}: continue
                if part == "..":
                    if not parts:
                        raise ValueError(f"unsafe backup link: {member.name}")
                    parts.pop()
                else:
                    parts.append(part)
            if target.is_absolute():
                raise ValueError(f"unsafe backup link: {member.name}")
    return members


def create(destination: pathlib.Path | None = None) -> pathlib.Path:
    root = LAYOUT.data_root.resolve()
    backups = root / "clarp/backups"
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = destination or backups / time.strftime("clarp-%Y%m%d-%H%M%S.tar.gz")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    database = LAYOUT.state_database.resolve()

    with tempfile.TemporaryDirectory(dir=backups, prefix=".backup-") as temporary:
        staged_db = pathlib.Path(temporary) / "state.sqlite"
        if database.exists():
            with sqlite3.connect(database) as source, sqlite3.connect(staged_db) as target:
                source.backup(target)
        with tarfile.open(destination, "w:gz") as archive:
            for path in sorted(root.rglob("*")):
                if path == destination or backups in path.parents or path == backups:
                    continue
                if path == database or path.name in {
                    database.name + "-wal", database.name + "-shm",
                }:
                    continue
                # CLI skill links are generated from immutable Clarp skills or
                # personal sources already included elsewhere in /data.
                if path.is_symlink():
                    relative = path.relative_to(root)
                    if (len(relative.parts) >= 3
                            and relative.parts[0] in {"claude", "codex"}
                            and relative.parts[1] == "skills"):
                        continue
                    if (len(relative.parts) >= 2
                            and relative.parts[0] in {"claude", "codex"}
                            and relative.parts[1] in {"tmp", "cache"}):
                        continue
                    try:
                        path.resolve().relative_to(root)
                    except ValueError as exc:
                        raise ValueError(
                            f"refusing to back up external symlink: {relative}") from exc
                archive.add(path, arcname=path.relative_to(root), recursive=False)
            if staged_db.exists():
                archive.add(staged_db, arcname="clarp/state.sqlite")
    destination.chmod(0o600)
    verify(destination)
    return destination


def verify(path: pathlib.Path) -> dict[str, int | bool]:
    path = path.resolve()
    with tempfile.TemporaryDirectory(prefix="clarp-backup-verify-") as temporary:
        with tarfile.open(path, "r:gz") as archive:
            members = _safe_members(archive)
            database_member = next(
                (member for member in members if member.name == "clarp/state.sqlite"), None)
            if database_member is None:
                raise ValueError("backup has no clarp/state.sqlite")
            # Exercise the same safe extraction used by restore. A backup is
            # not valid if any non-database member would fail at startup.
            archive.extractall(temporary, members=members, filter="data")
        database = pathlib.Path(temporary) / "clarp/state.sqlite"
        with sqlite3.connect(database) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise ValueError(f"backup database integrity check failed: {result}")
    return {"ok": True, "members": len(members), "bytes": path.stat().st_size}


def stage_restore(path: pathlib.Path) -> pathlib.Path:
    """Stage a restore for the Host's next start. Only inside a running lock-aware Host (through
    `docker compose exec`), whose live stack holds the data-root owner lock. Order: any stale
    staging record is removed, the archive is put in place, then the record binding the archive's
    exact bytes is written."""
    from . import data_root_locks
    verify(path)
    directory = data_root_locks.ensure_backups_dir()
    if data_root_locks.held_elsewhere(directory / data_root_locks.OWNER_LOCK) is not True:
        raise data_root_locks.RestoreRefused(
            data_root_locks.EXIT_REFUSED,
            "restore can only be staged inside the running Host (docker compose exec)")
    data_root_locks.remove_sidecar()
    pending = directory / PENDING_RESTORE
    temporary = pending.with_suffix(".next")
    shutil.copy2(path, temporary)
    temporary.chmod(0o600)
    temporary.replace(pending)
    data_root_locks.write_sidecar(pending, {"host_owner_held": True})
    return pending


def apply_pending_restore() -> bool:
    """Apply a staged restore, only with exclusive data-root ownership held by the entrypoint
    (lib.data_root_locks.apply_staged_restore); False when nothing is staged."""
    from . import data_root_locks
    return data_root_locks.apply_staged_restore()


def replace_data_root(pending: pathlib.Path) -> None:
    """Replace the data root with the archive's snapshot rather than merging it. The archive, its
    staging record and the locks in clarp/backups are preserved; the caller removes them in order."""
    root = LAYOUT.data_root.resolve()
    with tempfile.TemporaryDirectory(dir=root, prefix=".restore-") as temporary:
        staged = pathlib.Path(temporary)
        with tarfile.open(pending, "r:gz") as archive:
            archive.extractall(staged, members=_safe_members(archive), filter="data")
        # Generated CLI symlinks are repaired by the entrypoint.
        for existing in list(root.iterdir()):
            if existing == staged or existing.name == "clarp":
                continue
            if existing.is_dir() and not existing.is_symlink():
                shutil.rmtree(existing)
            else:
                existing.unlink()

        clarp_root = root / "clarp"
        clarp_root.mkdir(parents=True, exist_ok=True)
        backups = clarp_root / "backups"
        for existing in list(clarp_root.iterdir()):
            if existing == backups:
                continue
            if existing.is_dir() and not existing.is_symlink():
                shutil.rmtree(existing)
            else:
                existing.unlink()

        staged_clarp = staged / "clarp"
        if staged_clarp.is_dir():
            for child in staged_clarp.iterdir():
                shutil.move(str(child), str(clarp_root / child.name))
        for source in list(staged.iterdir()):
            if source.name != "clarp":
                shutil.move(str(source), str(root / source.name))
