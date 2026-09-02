"""Consistent backup and restart-applied restore for one Clarp data root."""
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


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe backup member: {member.name}")
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
    backups.mkdir(parents=True, exist_ok=True)
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
    verify(path)
    pending = LAYOUT.data_root / "clarp/backups" / PENDING_RESTORE
    pending.parent.mkdir(parents=True, exist_ok=True)
    temporary = pending.with_suffix(".next")
    shutil.copy2(path, temporary)
    temporary.chmod(0o600)
    temporary.replace(pending)
    return pending


def apply_pending_restore() -> bool:
    """Apply an explicitly staged restore before the server opens SQLite."""
    root = LAYOUT.data_root.resolve()
    pending = root / "clarp/backups" / PENDING_RESTORE
    if not pending.is_file():
        return False
    verify(pending)
    with tempfile.TemporaryDirectory(dir=root, prefix=".restore-") as temporary:
        staged = pathlib.Path(temporary)
        with tarfile.open(pending, "r:gz") as archive:
            archive.extractall(staged, members=_safe_members(archive), filter="data")
        # Replace the snapshot rather than merging it. Preserve only the
        # backup directory containing the pending archive until application is
        # complete; generated CLI symlinks are repaired by the entrypoint.
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
    pending.unlink(missing_ok=True)
    return True
