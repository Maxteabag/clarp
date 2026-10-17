"""Host generation: this boot plus this instance's token (C2 design r4 §12, §14).

Two primitives, deliberately separate:

- ensure_instance_generation() is the bootstrap and the only creator. An explicit
  caller runs it outside any SQLite transaction. It makes the backups directory and
  mints the token once; it never repairs or rotates an existing token.
- rotate_instance_generation() replaces the token. Only the supported exclusive restore
  calls it, before its first destructive step; any failure raises and the restore stops.
- read_host_generation() only reads. It never creates, links, writes, syncs, unlinks
  or logs, so it is safe under the SQLite write lock. It returns the failure reason
  for the caller to log after the transaction ends.

The directory and the token are opened without following a symlink in their last
path component and are checked by fstat on the open descriptor: type, owner (the
Host's own uid, §14) and mode. Every failure is unknown, never a guess.
"""
from __future__ import annotations

import os
import pathlib
import secrets
import stat
import uuid
from dataclasses import dataclass

from .deployment import LAYOUT
from .log import log, log_exception

TOKEN_NAME = "instance.generation"
MAX_TOKEN_BYTES = 64
BOOT_ID_PATH = pathlib.Path("/proc/sys/kernel/random/boot_id")

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK keeps a FIFO planted at the token name from blocking the open.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class _Invalid(Exception):
    """A path exists but is not what the generation needs; never repaired."""


class RotationRefused(Exception):
    """The token could not be rotated; the restore that asked must not proceed."""


@dataclass(frozen=True)
class GenerationRead:
    """(boot_id, token) when both are valid, else None with the reason why."""
    generation: tuple[str, str] | None
    reason: str | None = None


def default_backups_dir() -> pathlib.Path:
    """Imported at module load, so a read under the write lock opens no module files."""
    return LAYOUT.data_root / "clarp" / "backups"


def default_boot_id_path() -> pathlib.Path:
    return BOOT_ID_PATH


def _canonical_uuid(data: bytes) -> str:
    """The text of a canonical lowercase UUID, optionally followed by one newline."""
    if len(data) > MAX_TOKEN_BYTES:
        raise _Invalid("oversize")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise _Invalid("not ascii") from None
    if text.endswith("\n"):
        text = text[:-1]
    try:
        canonical = str(uuid.UUID(text))
    except ValueError:
        raise _Invalid("not a uuid") from None
    if canonical != text:
        raise _Invalid("not canonical")
    return text


def _read_bounded(fd: int) -> bytes:
    """At most MAX_TOKEN_BYTES + 1 bytes, so oversize content is detectable."""
    chunks = []
    remaining = MAX_TOKEN_BYTES + 1
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _open_dir(path: pathlib.Path, uid: int) -> int:
    """A descriptor on the backups directory: a real directory, ours, not group/world-writable."""
    fd = os.open(path, _DIR_FLAGS)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise _Invalid("backups is not a directory")
        if st.st_uid != uid:
            raise _Invalid(f"backups owner uid {st.st_uid}, expected uid {uid}")
        if st.st_mode & 0o022:
            raise _Invalid("backups is group or world writable")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_token(dir_fd: int, uid: int) -> str:
    """The valid token under dir_fd. FileNotFoundError when absent, _Invalid otherwise."""
    try:
        fd = os.open(TOKEN_NAME, _READ_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise _Invalid(f"token open failed: {type(exc).__name__}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _Invalid("token is not a regular file")
        if st.st_uid != uid:
            raise _Invalid(f"token owner uid {st.st_uid}, expected uid {uid}")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise _Invalid("token mode is not 0600")
        if st.st_size > MAX_TOKEN_BYTES:
            raise _Invalid("oversize")
        return _canonical_uuid(_read_bounded(fd))
    finally:
        os.close(fd)


def _read_boot_id(path: pathlib.Path) -> str:
    fd = os.open(path, _READ_FLAGS)
    try:
        return _canonical_uuid(_read_bounded(fd))
    finally:
        os.close(fd)


def read_host_generation(*, backups_dir: pathlib.Path | None = None,
                         boot_id_path: pathlib.Path | None = None,
                         uid: int | None = None) -> GenerationRead:
    """Read this boot and this instance's token. Opens, fstats, reads and closes only."""
    backups_dir = default_backups_dir() if backups_dir is None else backups_dir
    boot_id_path = default_boot_id_path() if boot_id_path is None else boot_id_path
    uid = os.geteuid() if uid is None else uid
    try:
        boot_id = _read_boot_id(boot_id_path)
    except (OSError, _Invalid) as exc:
        return GenerationRead(None, f"boot id: {_reason(exc)}")
    try:
        dir_fd = _open_dir(backups_dir, uid)
    except (OSError, _Invalid) as exc:
        return GenerationRead(None, f"backups: {_reason(exc)}")
    try:
        token = _read_token(dir_fd, uid)
    except FileNotFoundError:
        return GenerationRead(None, "token absent")
    except (OSError, _Invalid) as exc:
        return GenerationRead(None, f"token: {_reason(exc)}")
    finally:
        os.close(dir_fd)
    return GenerationRead((boot_id, token))


def _reason(exc: BaseException) -> str:
    if isinstance(exc, _Invalid):
        return str(exc)
    return f"{type(exc).__name__} errno={getattr(exc, 'errno', None)}"


def _make_dir(path: pathlib.Path) -> None:
    """Create any missing component 0700 and sync its parent; an existing one is left as is."""
    if os.path.lexists(path):
        return
    _make_dir(path.parent)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return  # another bootstrap made it; _open_dir judges it
    fd = os.open(path, _DIR_FLAGS)
    try:
        os.fchmod(fd, 0o700)  # the umask may have narrowed it
    finally:
        os.close(fd)
    parent = os.open(path.parent, _DIR_FLAGS)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def ensure_instance_generation(*, backups_dir: pathlib.Path | None = None,
                               uid: int | None = None) -> bool:
    """Make the instance token exist and be valid. True when it does; never repairs one.

    The only creator. Call it explicitly, outside any SQLite transaction. It never raises: a
    failure leaves the token unprovable and the Host starts without it (design r4 §12 D).
    """
    dir_fd = None
    tmp = None
    try:
        backups_dir = default_backups_dir() if backups_dir is None else backups_dir
        uid = os.geteuid() if uid is None else uid
        _make_dir(backups_dir)
        dir_fd = _open_dir(backups_dir, uid)
        try:
            _read_token(dir_fd, uid)
            return True
        except FileNotFoundError:
            pass
        tmp = f".{TOKEN_NAME}.{os.getpid()}.{secrets.token_hex(8)}"
        fd = os.open(tmp, _CREATE_FLAGS, 0o600, dir_fd=dir_fd)
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, f"{uuid.uuid4()}\n".encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, TOKEN_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                    follow_symlinks=False)
        except FileExistsError:
            log("hostGenerationBootstrapRaceLost")  # the winner is judged below
        os.unlink(tmp, dir_fd=dir_fd)
        tmp = None
        os.fsync(dir_fd)
        _read_token(dir_fd, uid)
        return True
    except _Invalid as exc:
        log("hostGenerationInvalid", str(exc))
        return False
    except OSError as exc:
        log_exception("hostGenerationBootstrapFailed", exc)
        return False
    except Exception as exc:
        log_exception("hostGenerationBootstrapFailed", exc, "unexpected")
        return False
    finally:
        if tmp is not None and dir_fd is not None:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError as exc:
                log_exception("hostGenerationTempCleanupFailed", exc, tmp)
        if dir_fd is not None:
            os.close(dir_fd)


def rotate_instance_generation(*, backups_dir: pathlib.Path | None = None,
                               uid: int | None = None) -> str:
    """Replace the instance token with a fresh one and return it.

    Only the supported exclusive restore calls this (lib.data_root_locks, with both data-root
    locks held), after its last refusal and before its first destructive step. Temp file,
    fsync, rename over the final name (a symlink there is replaced, never followed), directory
    fsync. Any failure raises RotationRefused; the old token is left in place.
    """
    backups_dir = default_backups_dir() if backups_dir is None else backups_dir
    uid = os.geteuid() if uid is None else uid
    dir_fd = None
    tmp = None
    replaced = False
    try:
        dir_fd = _open_dir(backups_dir, uid)
        token = str(uuid.uuid4())
        tmp = f".{TOKEN_NAME}.{os.getpid()}.{secrets.token_hex(8)}"
        fd = os.open(tmp, _CREATE_FLAGS, 0o600, dir_fd=dir_fd)
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, f"{token}\n".encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, TOKEN_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        tmp = None
        replaced = True
        os.fsync(dir_fd)
        return token
    except (OSError, _Invalid) as exc:
        message = f"instance generation not rotated: {_reason(exc)}"
        if replaced:
            message = (f"instance generation rotation not durable: {_reason(exc)}; the token was "
                       "already replaced")
        if tmp is not None and dir_fd is not None:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError as cleanup:
                message += f"; temporary {tmp} not removed: {_reason(cleanup)}"
        raise RotationRefused(message) from exc
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
