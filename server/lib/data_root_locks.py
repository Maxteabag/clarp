"""Exclusive restore ownership of one container data root.

A staged restore (instance_backup) swaps the WHOLE data root, so it may only be applied while no
other process can be using it. Two kernel flock(2) locks on the shared volume decide that:

* ``host-owner.lock``: held EXCLUSIVE for the life of the one Host stack (the entrypoint shell, the
  runtime and the server). A second Host start on the same root refuses (exit 75).
* ``data-root-users.lock``: held SHARED by every entrypoint-launched process, and EXCLUSIVE only
  while a restore is applied. A restore refuses while any other user holds it (exit 76).

Both files live in ``<data_root>/clarp/backups``, which backups exclude and restores preserve, so
neither can be archived, restored or replaced by an archive member.

The entrypoint SHELL opens both files (``exec 8>> … 9>>``) and owns their open file descriptions.
Python never opens a lock fd of its own: it only validates and checks the INHERITED fds 8/9, and
only after their identity matches the expected file (a regular, uid-owned file whose fstat device
and inode equal an lstat of the expected path in a real, uid-owned, 0700 directory). An env claim
or a bare fd number is never trusted.

The guarantee is the conjunction of these locks on supported storage (the named local-driver
volume), the archive-bound staging sidecar, archive member checks, and the documented stopped-stack
transition. No single check is the guarantee: the held-lock self-probe shows mutual exclusion for
this lock instance only, and the SQLite dead-man-switch check is defense in depth. Processes that
never pass the entrypoint (``docker exec`` into another container, host-side or bind-mount access,
``--entrypoint`` overrides, other images on the volume, ``docker compose run`` against a live node)
are unsupported restore conditions; nothing here can detect them.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import time

from .deployment import LAYOUT

OWNER_FD, USERS_FD = 8, 9
OWNER_LOCK, USERS_LOCK = "host-owner.lock", "data-root-users.lock"
PENDING = "restore.pending.tar.gz"
SIDECAR = "restore.pending.owner"
MARKER = "restore.applying"

EXIT_OWNED = 75            # the data root is owned by a live Host
EXIT_IN_USE = 76           # restore refused: the data root is in use
EXIT_LOCKING = 77          # locking unsupported or unverifiable: restore refused
EXIT_REFUSED = 78          # restore refused: archive or staging provenance

# SQLite 3.53.4 os_unix.c:4602-4603: UNIX_SHM_DMS = (22 + SQLITE_SHM_NLOCK) * 4 + SQLITE_SHM_NLOCK,
# with SQLITE_SHM_NLOCK = 8 (sqlite.h.in). Every normal-mode WAL connection that has read through
# the WAL index holds a SHARED fcntl lock on this byte of the -shm file.
SHM_DMS_OFFSET = 128

LOCAL_FILESYSTEMS = frozenset({"ext4", "xfs", "btrfs", "zfs"})
REMOTE_PREFIXES = ("nfs", "cifs", "smb", "fuse", "9p", "virtiofs", "ceph", "glusterfs")
FILESYSTEM_OVERRIDE_ENV = "CLARP_RESTORE_ALLOW_FILESYSTEM"


class RestoreRefused(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def backups_dir() -> pathlib.Path:
    return LAYOUT.data_root / "clarp" / "backups"


def _fsync_dir(path: pathlib.Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_backups_dir() -> pathlib.Path:
    """Create the lock directory if absent (0700) and check it is a real, uid-owned directory that
    no group or other user can write. Never repairs: a wrong directory is refused."""
    root = LAYOUT.data_root
    if not root.is_dir():
        raise RestoreRefused(EXIT_LOCKING, f"data root is missing: {root}")
    directory = backups_dir()
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        raise RestoreRefused(EXIT_LOCKING, f"{directory} is not a real directory")
    if info.st_uid != os.getuid():
        raise RestoreRefused(EXIT_LOCKING, f"{directory} is not owned by uid {os.getuid()}")
    if info.st_mode & 0o022:
        raise RestoreRefused(EXIT_LOCKING, f"{directory} is group- or world-writable")
    return directory


def inherited_lock_fd(fd: int, name: str) -> bool:
    """True only if ``fd`` is open on exactly ``backups_dir()/name``: a regular file owned by this
    uid whose device and inode equal an lstat of that (non-symlink) path."""
    try:
        opened = os.fstat(fd)
        expected = os.lstat(backups_dir() / name)
    except OSError:
        return False
    return (stat.S_ISREG(opened.st_mode) and opened.st_uid == os.getuid()
            and not stat.S_ISLNK(expected.st_mode)
            and (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino))


def seal_inherited_lock_fds() -> list[int]:
    """At runtime/server startup: keep holding the inherited lock fds but stop passing them on, so
    no fork+exec child (agent terminals: terminal_ws pty.fork + execvp) receives a copy that could
    unlock or convert the shared open file description. Only fds whose identity matches the lock
    files are touched; anything else (native mode, unrelated fds) is left alone."""
    sealed = []
    for fd, name in ((OWNER_FD, OWNER_LOCK), (USERS_FD, USERS_LOCK)):
        if inherited_lock_fd(fd, name):
            os.set_inheritable(fd, False)
            sealed.append(fd)
    return sealed


def held_elsewhere(path: pathlib.Path) -> bool | None:
    """A separate short-lived process opens ``path`` fresh and tries a non-blocking exclusive
    flock (util-linux flock(1)). True: it could not (the lock is held by an open file description
    other than its own). False: it acquired it, then released it on exit. None: flock failed."""
    try:
        result = subprocess.run(["flock", "-x", "-n", "-E", "75", str(path), "true"],
                                capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 75:
        return True
    if result.returncode == 0:
        return False
    return None


def require_held(fd: int, name: str) -> None:
    """The entrypoint shell says it holds the lock on ``fd``: verify the identity and that a fresh
    open elsewhere cannot acquire it (OFD semantics on this filesystem). Otherwise refuse 77."""
    if not inherited_lock_fd(fd, name):
        raise RestoreRefused(EXIT_LOCKING, f"fd {fd} is not the inherited {name}")
    if held_elsewhere(backups_dir() / name) is not True:
        raise RestoreRefused(EXIT_LOCKING, f"{name} is not effectively held (locking unsupported "
                                           "or emulated on this filesystem)")


# The dead-man-switch probe runs in its own interpreter (-I: isolated, no site, no repo import),
# so the probing process never opens SQLite and holds no POSIX lock that a close could drop.
# F_GETLK is non-mutating: it reports a conflicting lock without taking one.
_DMS_PROBE = r"""
import fcntl, os, struct, sys
if not sys.platform.startswith("linux") or struct.calcsize("P") != 8:
    sys.exit(3)
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
except FileNotFoundError:
    sys.exit(0)
except OSError:
    sys.exit(3)
layout = "hhxxxxqqixxxx"
try:
    answer = fcntl.fcntl(fd, fcntl.F_GETLK,
                         struct.pack(layout, fcntl.F_WRLCK, os.SEEK_SET, int(sys.argv[2]), 1, 0))
except OSError:
    sys.exit(3)
sys.exit(0 if struct.unpack(layout, answer)[0] == fcntl.F_UNLCK else 2)
"""


def dms_conflict(shm: pathlib.Path) -> bool | None:
    """True when some process holds the SQLite dead-man-switch byte of ``shm``; False when none
    does (or the -shm file is absent: no WAL user has the database open); None when unverifiable."""
    try:
        result = subprocess.run([sys.executable, "-I", "-c", _DMS_PROBE, str(shm), str(SHM_DMS_OFFSET)],
                                capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: False, 2: True}.get(result.returncode)


def filesystem_type(path: pathlib.Path) -> str:
    """The filesystem type of the mount holding ``path`` (/proc/self/mountinfo), or ""."""
    target = str(path.resolve())
    best, kind = "", ""
    try:
        lines = pathlib.Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        left, _, right = line.partition(" - ")
        fields = left.split()
        if len(fields) < 5 or not right:
            continue
        mount = fields[4].replace("\\040", " ")
        if (target == mount or target.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
            best, kind = mount, right.split()[0]
    return kind


def check_filesystem(path: pathlib.Path) -> str:
    kind = filesystem_type(path)
    if not kind or kind.startswith(REMOTE_PREFIXES):
        raise RestoreRefused(EXIT_LOCKING, f"restore refused on filesystem {kind or 'unknown'!r}")
    if kind not in LOCAL_FILESYSTEMS and os.environ.get(FILESYSTEM_OVERRIDE_ENV) != kind:
        raise RestoreRefused(EXIT_LOCKING, f"restore refused on filesystem {kind!r}; set "
                                           f"{FILESYSTEM_OVERRIDE_ENV}={kind} to accept it")
    return kind


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def remove_sidecar() -> None:
    try:
        (backups_dir() / SIDECAR).unlink()
    except FileNotFoundError:
        return
    _fsync_dir(backups_dir())


def write_sidecar(pending: pathlib.Path, evidence: dict) -> pathlib.Path:
    """Bind the staged archive to its exact bytes, atomically and only after the archive is in
    place (temp file, fsync, rename, directory fsync)."""
    from .server_identity import clarp_version
    record = {"sha256": sha256_file(pending), "size": pending.stat().st_size,
              "version": clarp_version(), "staged_at": int(time.time()), "evidence": evidence}
    target = backups_dir() / SIDECAR
    temporary = target.with_name(target.name + f".{os.getpid()}.next")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps(record, sort_keys=True).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, target)
    _fsync_dir(target.parent)
    return target


def check_sidecar(pending: pathlib.Path) -> dict:
    target = backups_dir() / SIDECAR
    try:
        record = json.loads(target.read_text())
    except (OSError, ValueError) as exc:
        raise RestoreRefused(EXIT_REFUSED, "restore refused: no staging record from a lock-aware "
                                           "Host; stop every container on this volume, start the "
                                           "Host, then stage the restore again") from exc
    if (record.get("size") != pending.stat().st_size
            or record.get("sha256") != sha256_file(pending)):
        raise RestoreRefused(EXIT_REFUSED, "restore refused: the staged archive does not match "
                                           "its staging record")
    return record


def write_marker() -> None:
    marker = backups_dir() / MARKER
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, str(int(time.time())).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(marker.parent)


def recover_marker() -> str:
    """At a start with NO pending archive (called before the stack starts): a marker left without
    its archive means the apply completed after its last delete. Remove a leftover sidecar first,
    then the marker. With the archive still present the caller re-applies instead."""
    directory = backups_dir()
    marker = directory / MARKER
    if not marker.exists():
        return "none"
    if (directory / PENDING).exists():
        return "interrupted"
    remove_sidecar()
    marker.unlink()
    _fsync_dir(directory)
    return "completed"


def _apply_errors() -> tuple[type[BaseException], ...]:
    import sqlite3
    import tarfile
    import zlib
    return (tarfile.TarError, EOFError, zlib.error, sqlite3.DatabaseError, ValueError, OSError)


# The failures `apply` maps to an exit code (78; OSError 77), before and after the marker.
_APPLY_ERRORS = _apply_errors()


def apply_staged_restore() -> bool:
    """The server-CMD apply, entered only after the entrypoint shell took owner EX (fd 8) and users
    EX (fd 9). Every refusal happens before the marker and before any deletion; the pending
    archive and its sidecar are kept."""
    from . import instance_backup
    pending = backups_dir() / PENDING
    if not pending.is_file():
        return False
    require_held(OWNER_FD, OWNER_LOCK)
    require_held(USERS_FD, USERS_LOCK)
    check_sidecar(pending)
    try:
        instance_backup.verify(pending)
    except ValueError as exc:
        raise RestoreRefused(EXIT_REFUSED, f"restore refused: {exc}") from exc
    check_filesystem(LAYOUT.data_root)
    database = LAYOUT.state_database
    conflict = dms_conflict(database.with_name(database.name + "-shm"))
    if conflict is not False:
        raise RestoreRefused(EXIT_IN_USE, "restore refused: the database is open elsewhere"
                                          if conflict else "restore refused: database use "
                                          "could not be verified")
    # A fresh instance generation (lib.host_generation) before the first destructive step: every
    # opening fenced before this restore then mismatches. clarp/backups is never archived or
    # replaced, so the old token cannot come back with the restored data.
    from . import host_generation
    try:
        host_generation.rotate_instance_generation(backups_dir=backups_dir())
    except host_generation.RotationRefused as exc:
        raise RestoreRefused(EXIT_LOCKING, f"restore refused: {exc}; nothing was deleted and the "
                                           "staged archive is kept") from exc
    write_marker()
    try:
        instance_backup.replace_data_root(pending)
        directory = backups_dir()
        pending.unlink()
        _fsync_dir(directory)
        remove_sidecar()
        (directory / MARKER).unlink()
        _fsync_dir(directory)
    except _APPLY_ERRORS as exc:
        # Past the marker the data root is being replaced: never "refused" or "unchanged". The
        # marker (and the archive, unless its removal already succeeded) stay for the next start.
        kept = "the marker and staged archive are kept" if pending.exists() else "the marker is kept"
        raise RestoreRefused(
            EXIT_LOCKING if isinstance(exc, OSError) else EXIT_REFUSED,
            f"restore interrupted, incomplete: the data root may be partially replaced; {kept}; "
            f"start the Host to complete it: {exc}") from exc
    return True


def _main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    try:
        if command == "prepare":
            ensure_backups_dir()
        elif command == "validate-fds":
            if not (inherited_lock_fd(OWNER_FD, OWNER_LOCK) and inherited_lock_fd(USERS_FD, USERS_LOCK)):
                raise RestoreRefused(EXIT_LOCKING, "inherited lock fds do not match the lock files")
        elif command == "probe-held":
            fd, name = {"owner": (OWNER_FD, OWNER_LOCK), "users": (USERS_FD, USERS_LOCK)}[argv[1]]
            require_held(fd, name)
        elif command == "apply":
            try:
                apply_staged_restore()
            except OSError as exc:
                raise RestoreRefused(EXIT_LOCKING, f"restore refused: I/O on the data root: {exc}") from exc
            except _APPLY_ERRORS as exc:
                raise RestoreRefused(EXIT_REFUSED, f"restore refused: unreadable archive: {exc}") from exc
        elif command == "recover-marker":
            state = recover_marker()
            if state == "interrupted":
                raise RestoreRefused(EXIT_REFUSED, "an interrupted restore is pending; only the "
                                                   "Host start may re-apply it")
        elif command == "check-marker":
            if (backups_dir() / MARKER).exists():
                raise RestoreRefused(EXIT_IN_USE, "a restore is in progress or was interrupted; "
                                                  "start the Host to complete it")
        else:
            print(f"usage: python3 -m lib.data_root_locks prepare|validate-fds|probe-held "
                  f"owner|users|apply|recover-marker|check-marker", file=sys.stderr)
            return 2
    except RestoreRefused as refused:
        print(f"clarp: {refused} (exit {refused.code})", file=sys.stderr)
        return refused.code
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
