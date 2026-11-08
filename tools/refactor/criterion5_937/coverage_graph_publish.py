"""937 single graph-artifact owner SOURCE ONLY, UNEXECUTED and unwired.

Conditional filesystem/wire custody only. Actual complete graph semantics,
source/config/role/raw input authentication, immutable input object ownership,
namespace/FD-return containment and final report-index ownership are external.
No report, manifest commit, cleanup deletion, retry or semantic acceptance.
"""
import hashlib
import os
import socket
import stat

from .coverage_copy_read import (_root, _snapshot, _retain, _same_capture,
                                 CHUNK, MAX_INPUT_BYTES)
from .coverage_graph_wire import encode_graph, decode_graph, _check, _envelope

NAME = 'attribution.json'


def _root_identity(root):
    value = root.stat()
    return value.st_dev, value.st_ino


def _opened_matches(info, expected):
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and info.st_uid == os.geteuid()
            and (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            == tuple(expected[k] for k in ('dev', 'ino', 'bytes', 'mtime_ns', 'ctime_ns')))


def verify_graph(*, root, observation, expected_graph, limits):
    """Independent no-follow reopen after known writer cleanup succeeded.

    Returns actual typed bytes/observations only, NOT graph semantic acceptance.
    Exclusive immutable root/input custody must span all pathname operations.
    """
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no graph control execution authority')
    _check(expected_graph, limits)
    _envelope(expected_graph)
    root = _root(root)
    identity = _root_identity(root)
    path = root / NAME
    if (type(observation) is not dict or observation.get('path') != str(path)
            or set(observation) != {'path', 'dev', 'ino', 'bytes', 'mtime_ns', 'ctime_ns', 'sha256'}):
        raise ValueError('exact graph artifact observation required')
    expected = {k: v for k, v in observation.items() if k != 'path'}
    before = _snapshot(path)
    if not _same_capture(before, expected):
        raise ValueError('graph artifact changed since publication')
    # No caller ceiling may silently expand the shared artifact refusal limit.
    if type(limits) is not dict or type(limits.get('wire_bytes')) is not int:
        raise ValueError('explicit graph byte limit required')
    if not 0 < before['bytes'] <= min(limits['wire_bytes'], MAX_INPUT_BYTES):
        raise ValueError('graph artifact exceeds qualified caller/refusal boundary')
    fd, primary, raw = None, None, bytearray()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        if not _opened_matches(os.fstat(fd), before):
            raise ValueError('graph read descriptor differs from observed file')
        while True:
            chunk = os.read(fd, min(CHUNK, before['bytes'] - len(raw) + 1))
            if not chunk:
                break
            if len(raw) + len(chunk) > before['bytes']:
                raise ValueError('graph grew during read')
            raw.extend(chunk)
        if len(raw) != before['bytes'] or not _opened_matches(os.fstat(fd), before):
            raise ValueError('graph short read or changed descriptor')
    except BaseException as error:
        primary = error
    finally:
        if fd is not None:
            try:
                os.close(fd)  # one attempt; no numeric fallback or retry
            except BaseException as cleanup:
                primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary  # no optional decode/hash/index/next phase on error/control
    if (_root_identity(_root(root)) != identity
            or not _same_capture(_snapshot(path), before)):
        raise ValueError('graph root/file epoch changed during verification')
    raw = bytes(raw)
    if hashlib.sha256(raw).hexdigest() != before['sha256']:
        raise ValueError('actual reopened graph bytes differ from observation')
    decoded = decode_graph(raw, limits=limits)
    if not _same_capture(decoded, expected_graph):
        raise ValueError('persisted full typed graph differs from original input')
    return {'observation': {'path': str(path), **before}, 'decoded_graph': decoded,
            'qualification': 'ACTUAL_GRAPH_WIRE_BYTES_ONLY_NOT_SEMANTICS_OR_PRODUCER_ACCEPTANCE',
            'actual_graph_source_producer_or_report_accepted': False}


def publish_graph(*, root, graph, limits):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; source only until exact admission')
    root = _root(root)
    if any(root.iterdir()):
        raise ValueError('fresh EMPTY private graph root required')
    identity = _root_identity(root)
    raw = encode_graph(graph, limits=limits)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError('graph exceeds shared artifact refusal ceiling; no cap increase')
    directory_fd, file_fd, primary = None, None, None
    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(directory_fd)
        if ((info.st_dev, info.st_ino) != identity or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError('graph directory descriptor changed')
        file_fd = os.open(NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                          | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError('owned exclusive graph file required')
        written = 0
        while written < len(raw):
            chunk = raw[written:written + CHUNK]
            if os.write(file_fd, chunk) != len(chunk):
                raise OSError('short graph write; preserve partial/no retry')
            written += len(chunk)
        os.fsync(file_fd)
        os.fsync(directory_fd)
    except BaseException as error:
        primary = error
    finally:
        # Attempt every KNOWN descriptor independently once; unknown ownership
        # after open-return/assignment or close error needs outer containment.
        for fd in (file_fd, directory_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as cleanup:
                    primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary  # partials remain; no verification or usable index
    if _root_identity(_root(root)) != identity:
        raise ValueError('graph root changed during publication')
    actual = _snapshot(root / NAME)
    if actual['bytes'] != len(raw) or actual['sha256'] != hashlib.sha256(raw).hexdigest():
        raise ValueError('closed graph bytes differ from encoded full input')
    return verify_graph(root=root, observation={'path': str(root / NAME), **actual},
                        expected_graph=graph, limits=limits)
