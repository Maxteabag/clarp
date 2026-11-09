"""First actual retained-file observation SOURCE ONLY, UNEXECUTED.

No supplied digest/version, producer/writer-closure or index acceptance. The
qualified parent must establish immutable closed inputs before measurement use.
"""
import hashlib
import os
from pathlib import Path
import socket
import stat

from .coverage_copy_read import _root, _retain, CHUNK, MAX_INPUT_BYTES

_FIELDS = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_uid', 'st_mode')


def observe_file(path, *, byte_limit, path_bytes_limit, retain_bytes):
    """One bounded read/hash owner; ordinary success only after known close.

    retain_bytes=False never constructs a whole-file bytearray/bytes payload.
    Returns an actual observation plus optional ORIGINAL bytes, not provenance.
    """
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no local observation/control authority')
    if (type(byte_limit) is not int or not 0 < byte_limit <= MAX_INPUT_BYTES
            or type(path_bytes_limit) is not int or path_bytes_limit <= 0
            or type(retain_bytes) is not bool or type(path) is not str or not path
            or '\x00' in path or '\\' in path or len(path.encode('utf-8')) > path_bytes_limit):
        raise ValueError('exact bounded operation/path required')
    target = Path(path)
    if not target.is_absolute() or str(target) != path or '..' in target.parts:
        raise ValueError('canonical absolute observed file required')
    root = _root(target.parent)
    root_info = root.stat()
    root_identity = root_info.st_dev, root_info.st_ino
    if any(os.path.lexists(path + suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('observed file sidecar unresolved')
    before = target.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != os.geteuid() or not 0 < before.st_size <= byte_limit):
        raise ValueError('observed file kind/owner/size outside pre-allocation bound')
    # Observe count FIRST. Neither a MAX-sized snapshot nor a claimed receipt
    # precedes this caller's smaller limit. All aggregate bounds remain outer.
    raw = bytearray() if retain_bytes else None
    digest, count, fd, primary = hashlib.sha256(), 0, None, None
    try:
        fd = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(fd)
        if any(getattr(opened, k) != getattr(before, k) for k in _FIELDS):
            raise ValueError('observed descriptor differs from named epoch')
        while True:
            chunk = os.read(fd, min(CHUNK, before.st_size - count + 1))
            if not chunk:
                break
            count += len(chunk)
            if count > before.st_size:
                raise ValueError('observed file grew during bounded read')
            digest.update(chunk)
            if retain_bytes:
                raw.extend(chunk)
        after_fd = os.fstat(fd)
        if count != before.st_size or any(getattr(after_fd, k) != getattr(before, k) for k in _FIELDS):
            raise ValueError('observed file short read or descriptor epoch drift')
    except BaseException as error:
        primary = error
    finally:
        if fd is not None:
            try:
                os.close(fd)  # one attempt; uncertain close is not retried
            except BaseException as cleanup:
                primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary  # no optional post-path/hash/decode/next input
    after_root = _root(root).stat()
    after = target.lstat()
    if ((after_root.st_dev, after_root.st_ino) != root_identity
            or any(getattr(after, k) != getattr(before, k) for k in _FIELDS)
            or any(os.path.lexists(path + suffix) for suffix in ('-wal', '-shm', '-journal'))):
        raise ValueError('observed root/file/sidecar epoch changed')
    observation = {'path': path, 'dev': after.st_dev, 'ino': after.st_ino,
                   'bytes': count, 'mtime_ns': after.st_mtime_ns, 'ctime_ns': after.st_ctime_ns,
                   'sha256': digest.hexdigest()}
    return {'observation': observation, 'raw': bytes(raw) if retain_bytes else None,
            'process_source_or_closed_writer_authenticated': False}
