"""Read-only, agent-rooted filesystem browsing for native clients."""
from __future__ import annotations

import errno
import os
import pathlib
import stat
from dataclasses import asdict, dataclass

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 1000


class AgentFileError(ValueError):
    status = 400


class AgentFileNotFound(AgentFileError):
    status = 404


class AgentFileForbidden(AgentFileError):
    status = 403


class AgentFileTooLarge(AgentFileError):
    status = 413


class AgentFileUnsupported(AgentFileError):
    status = 415


@dataclass(frozen=True)
class FileEntry:
    name: str
    path: str
    is_directory: bool
    size: int
    modified_at: float


def _parts(relative: str) -> tuple[str, ...]:
    # Preserve valid filename whitespace exactly. Reject traversal syntax
    # instead of normalizing it into a different workspace path.
    if "\0" in relative:
        raise AgentFileForbidden("invalid workspace-relative path")
    path = pathlib.PurePosixPath(relative or "")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        if relative:
            raise AgentFileForbidden("invalid workspace-relative path")
        return ()
    return path.parts


def _open_beneath(root: pathlib.Path | str, relative: str,
                  *, directory: bool,
                  confinement_root: pathlib.Path | None = None
                  ) -> tuple[pathlib.Path, int]:
    try:
        resolved_root = pathlib.Path(root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AgentFileNotFound("agent working directory is unavailable") from exc
    if not resolved_root.is_dir():
        raise AgentFileNotFound("agent working directory is unavailable")
    parts = _parts(relative)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    fd = -1
    try:
        fd = os.open(resolved_root, flags | directory_flag | nofollow)
        if confinement_root is not None:
            # Container deployments are Linux. Verify the path attached to the
            # opened descriptor, not the pathname checked before open, so a
            # concurrent rename/symlink swap cannot cross the workspace boundary.
            opened_root = pathlib.Path(os.readlink(f"/proc/self/fd/{fd}"))
            confinement = confinement_root.resolve(strict=True)
            try:
                opened_root.relative_to(confinement)
            except ValueError as exc:
                raise AgentFileForbidden(
                    "agent workspace is outside the configured workspace root") from exc
        for index, part in enumerate(parts):
            is_last = index == len(parts) - 1
            child_flags = flags | nofollow
            if not is_last or directory:
                child_flags |= directory_flag
            else:
                child_flags |= getattr(os, "O_NONBLOCK", 0)
            next_fd = os.open(part, child_flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return resolved_root, fd
    except (OSError, ValueError) as exc:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if isinstance(exc, OSError) and exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            raise AgentFileNotFound("path not found") from exc
        raise AgentFileForbidden("path cannot be opened safely") from exc


def list_directory(root: pathlib.Path | str, relative: str = "", *,
                   confinement_root: pathlib.Path | None = None) -> dict:
    resolved_root, fd = _open_beneath(
        root, relative, directory=True, confinement_root=confinement_root)
    try:
        rows: list[FileEntry] = []
        truncated = False
        for index, child in enumerate(os.scandir(fd)):
            if index >= MAX_DIRECTORY_ENTRIES:
                truncated = True
                break
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            is_directory = stat.S_ISDIR(info.st_mode)
            if not is_directory and not stat.S_ISREG(info.st_mode):
                continue
            rows.append(FileEntry(
                name=child.name,
                path=str(pathlib.PurePosixPath(relative) / child.name),
                is_directory=is_directory,
                size=0 if is_directory else info.st_size,
                modified_at=info.st_mtime,
            ))
        rows.sort(key=lambda item: (not item.is_directory, item.name.casefold()))
        return {
            "root_name": resolved_root.name or str(resolved_root),
            "root_path": str(resolved_root),
            "path": relative,
            "entries": [asdict(row) for row in rows],
            "truncated": truncated,
        }
    finally:
        os.close(fd)


def read_text_file(root: pathlib.Path | str, relative: str, *,
                   confinement_root: pathlib.Path | None = None) -> dict:
    _, fd = _open_beneath(
        root, relative, directory=False, confinement_root=confinement_root)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AgentFileError("path is not a file")
        if info.st_size > MAX_FILE_BYTES:
            raise AgentFileTooLarge("file is larger than 2 MB")
        chunks: list[bytes] = []
        remaining = MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_FILE_BYTES:
            raise AgentFileTooLarge("file is larger than 2 MB")
    finally:
        os.close(fd)
    if b"\0" in raw:
        raise AgentFileUnsupported("binary files cannot be previewed")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentFileUnsupported("file is not UTF-8 text") from exc
    return {
        "path": relative,
        "name": pathlib.PurePosixPath(relative).name,
        "content": content,
        "size": len(raw),
        "modified_at": info.st_mtime,
    }
