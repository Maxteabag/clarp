"""Exact retained-artifact byte reader SOURCE ONLY, UNEXECUTED and unwired.

No receipt parser/index producer, provenance authentication or union/report call.
Caller owns immutable physical namespaces and must qualify aggregate resource
limits BEFORE any readers are called. Unknown errors preserve/stop, no retry.
"""
import hashlib
import os
from pathlib import Path
import socket

from .coverage_copy_read import (_root, _retain,
                                 CHUNK, MAX_INPUT_BYTES)
from .coverage_graph_publish import _opened_matches

FIELDS = {'path', 'dev', 'ino', 'bytes', 'mtime_ns', 'ctime_ns', 'sha256'}


def check_observation(observation, *, byte_limit, path_bytes_limit):
    """Pure preflight, suitable for the future ALL-input aggregate preflight.

    Required caller ceilings are refusal boundaries, NOT approved capacities.
    This does not open/authenticate an artifact or bound decoder/heap costs.
    """
    if (type(byte_limit) is not int or not 0 < byte_limit <= MAX_INPUT_BYTES
            or type(path_bytes_limit) is not int or path_bytes_limit <= 0
            or type(observation) is not dict or set(observation) != FIELDS):
        raise ValueError('exact observed artifact and explicit finite limits required')
    text = observation['path']
    if (type(text) is not str or not text or '\x00' in text or '\\' in text
            or len(text.encode('utf-8')) > path_bytes_limit):
        raise ValueError('invalid/oversized retained artifact path')
    path = Path(text)
    if not path.is_absolute() or str(path) != text or '..' in path.parts or not path.name:
        raise ValueError('canonical absolute retained artifact path required')
    if any(type(observation[k]) is not int for k in ('dev', 'ino', 'bytes', 'mtime_ns', 'ctime_ns')):
        raise ValueError('exact integer physical artifact fields required; no bool coercion')
    if (observation['dev'] < 0 or observation['ino'] <= 0
            or not 0 < observation['bytes'] <= byte_limit
            or observation['mtime_ns'] < 0 or observation['ctime_ns'] < 0):
        raise ValueError('retained artifact version/size outside finite domain')
    digest = observation['sha256']
    if (type(digest) is not str or len(digest) != 64
            or any(c not in '0123456789abcdef' for c in digest)):
        raise ValueError('exact retained artifact digest required')
    return path


def _consume_retained(observation, *, byte_limit, path_bytes_limit, retain_bytes):
    if type(retain_bytes) is not bool:
        raise ValueError('fixed retained-byte operation required')
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no local reader/control authority')
    path = check_observation(observation, byte_limit=byte_limit, path_bytes_limit=path_bytes_limit)
    root = _root(path.parent)
    root_info = root.stat()
    root_identity = root_info.st_dev, root_info.st_ino
    expected = {k: v for k, v in observation.items() if k != 'path'}
    if any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('retained artifact has unresolved sidecars')
    if not _opened_matches(path.lstat(), expected):
        raise ValueError('retained artifact differs before bounded read')
    fd, primary, count = None, None, 0
    raw = bytearray() if retain_bytes else None
    digest = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        if not _opened_matches(os.fstat(fd), expected):
            raise ValueError('retained read descriptor changed from observed file')
        while True:
            chunk = os.read(fd, min(CHUNK, expected['bytes'] - count + 1))
            if not chunk:
                break
            if count + len(chunk) > expected['bytes']:
                raise ValueError('retained artifact grew during read')
            count += len(chunk)
            if retain_bytes:
                raw.extend(chunk)
            digest.update(chunk)
        if count != expected['bytes'] or not _opened_matches(os.fstat(fd), expected):
            raise ValueError('retained artifact short read/version mismatch')
    except BaseException as error:
        primary = error
    finally:
        if fd is not None:
            try:
                os.close(fd)  # one attempt; unknown close is not retried
            except BaseException as cleanup:
                primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary  # no post-snapshot/hash/decode/next input on failure/control
    current = _root(root).stat()
    after = path.lstat()
    if ((current.st_dev, current.st_ino) != root_identity
            or not _opened_matches(after, expected)):
        raise ValueError('retained root/file epoch changed during read')
    if any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('sidecar appeared during retained read')
    actual_hash = digest.hexdigest()
    if actual_hash != expected['sha256']:
        raise ValueError('actual retained bytes differ from observed digest')
    if retain_bytes:
        return bytes(raw)  # original bytes, NOT source/producer acceptance
    return {'path': str(path), 'dev': after.st_dev, 'ino': after.st_ino,
            'bytes': count, 'mtime_ns': after.st_mtime_ns, 'ctime_ns': after.st_ctime_ns,
            'sha256': actual_hash}  # actual observation, NOT provenance acceptance


def read_retained_bytes(observation, *, byte_limit, path_bytes_limit):
    return _consume_retained(observation, byte_limit=byte_limit,
                             path_bytes_limit=path_bytes_limit, retain_bytes=True)


def hash_retained_bytes(observation, *, byte_limit, path_bytes_limit):
    """Same exact-version owner; chunks hashed without a whole-file buffer.

    This does not bound kernel page cache/IO/CPU or all memory/whole-package fit.
    No extraction/import/JSON decode or source/producer authentication.
    """
    return _consume_retained(observation, byte_limit=byte_limit,
                             path_bytes_limit=path_bytes_limit, retain_bytes=False)
